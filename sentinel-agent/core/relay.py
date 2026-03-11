"""
AgentRelay — manages the WebSocket connection to Sentinel Brain.

Handles:
  - Exponential backoff reconnection (1s → 2s → 4s → 60s max)
  - Inbound message dispatch to registered handlers
  - Outbound message queue with HMAC signing
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Coroutine, Any

logger = logging.getLogger(__name__)


class AgentRelay:
    def __init__(self, settings):
        self._settings = settings
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._inbound_handlers: dict[str, Callable] = {}
        self._connect_handlers: list[Callable] = []
        self._ws = None
        self._backoff = 1.0
        self._running = False

    def register_handler(self, msg_type: str, handler: Callable) -> None:
        """Register an async handler for a specific inbound message type.
        Use '_on_connect' for connection establishment callbacks."""
        if msg_type == "_on_connect":
            self._connect_handlers.append(handler)
        else:
            self._inbound_handlers[msg_type] = handler

    async def send(self, msg_type: str, payload: dict) -> None:
        """Enqueue a message for sending (signs it before send)."""
        from core.signing import build_message
        msg = build_message(msg_type, payload, self._settings.agent_token)
        await self._send_queue.put(json.dumps(msg))

    async def connect_and_run(self) -> None:
        """Main loop: connect with exponential backoff, run recv+send loops."""
        import websockets

        self._running = True
        ws_url = f"{self._settings.brain_url}/{self._settings.agent_id}"

        while self._running:
            try:
                logger.info("Connecting to Brain at %s", ws_url)
                async with websockets.connect(
                    ws_url,
                    extra_headers={"X-Agent-Token": self._settings.agent_token},
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._backoff = 1.0  # reset on successful connect
                    logger.info("Connected to Brain")

                    # Fire connect handlers
                    for handler in self._connect_handlers:
                        try:
                            await handler()
                        except Exception as e:
                            logger.warning("Connect handler error: %s", e)

                    await asyncio.gather(
                        self._recv_loop(ws),
                        self._send_loop(ws),
                        return_exceptions=True,
                    )

            except Exception as exc:
                logger.warning("Connection error: %s — retrying in %.0fs", exc, self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 60.0)
            finally:
                self._ws = None

    async def _recv_loop(self, ws) -> None:
        """Receive messages and dispatch to handlers."""
        async for raw in ws:
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")

                # Verify signature on inbound messages
                from core.signing import verify
                if not verify(msg, self._settings.agent_token):
                    logger.warning("Inbound HMAC verification failed for type=%s", msg_type)
                    continue

                handler = self._inbound_handlers.get(msg_type)
                if handler:
                    try:
                        await handler(msg.get("payload", {}))
                    except Exception as e:
                        logger.error("Handler error for %s: %s", msg_type, e)
                else:
                    logger.debug("No handler for message type: %s", msg_type)
            except Exception as exc:
                logger.error("Recv loop error: %s", exc)

    async def _send_loop(self, ws) -> None:
        """Drain the send queue and transmit messages."""
        while True:
            msg_json = await self._send_queue.get()
            try:
                await ws.send(msg_json)
            except Exception as exc:
                logger.error("Send error: %s — requeueing", exc)
                await self._send_queue.put(msg_json)
                raise  # re-raise to trigger reconnect

    def stop(self) -> None:
        self._running = False
