"""
BatchClient — wraps Anthropic's /v1/messages/batches endpoint for non-urgent tasks.

Use for: log analysis, report generation, bulk summarization, parallel triage.
Provides a 50% cost discount vs synchronous API calls. Results are available
within minutes (typically) but the endpoint is async — poll until complete.

Typical workflow:
    requests = [
        BatchRequest(custom_id="item-1", prompt="Summarize: ...", model=_HAIKU),
        BatchRequest(custom_id="item-2", prompt="Analyze: ...",   model=_HAIKU),
    ]
    batch_id = await client.submit(requests)
    results  = await client.poll_until_done(batch_id)  # ~1–60 min

Intended callers (wired in follow-on PRs):
    - bug_hunter_tasks.py  — per-error Haiku analysis (line 322)
    - sentry_tasks.py      — bulk triage of top-10 errors
    - reddit_skill.py      — post summarization (line 122)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

from app.config import get_settings


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class BatchRequest:
    """A single item to include in a batch submission."""

    custom_id: str
    prompt: str
    model: str = ""           # defaults to settings.model_haiku if empty
    max_tokens: int = 1024
    system: str = ""


@dataclass
class BatchResult:
    """Result for one item returned by the batch endpoint."""

    custom_id: str
    success: bool
    text: str = ""
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


# ── Client ────────────────────────────────────────────────────────────────────


class BatchClient:
    """
    Async wrapper around the Anthropic Messages Batch API.

    One instance is sufficient for the whole application — share via module-level
    singleton ``batch_client`` at the bottom of this file.
    """

    def __init__(self) -> None:
        self._client = None

    @property
    def _anthropic(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
        return self._client

    # ── Public API ────────────────────────────────────────────────────────────

    async def submit(
        self,
        requests: list[BatchRequest],
        default_model: str = "",
    ) -> str:
        """
        Submit a batch of requests.  Returns the batch_id immediately.

        Args:
            requests:      List of BatchRequest items.
            default_model: Model to use when BatchRequest.model is empty.
                           Defaults to settings.model_haiku.
        """
        s = get_settings()
        model_fallback = default_model or s.model_haiku

        batch_requests = []
        for req in requests:
            model = req.model or model_fallback
            body: dict = {
                "model": model,
                "max_tokens": req.max_tokens,
                "messages": [{"role": "user", "content": req.prompt}],
            }
            if req.system:
                body["system"] = req.system

            batch_requests.append({
                "custom_id": req.custom_id,
                "params": body,
            })

        def _submit() -> str:
            response = self._anthropic.messages.batches.create(requests=batch_requests)
            return response.id

        batch_id = await asyncio.to_thread(_submit)
        logger.info("BATCH_SUBMIT | id={} | count={}", batch_id, len(requests))
        return batch_id

    async def poll_until_done(
        self,
        batch_id: str,
        timeout_seconds: int = 3600,
        poll_interval: int = 60,
    ) -> list[BatchResult]:
        """
        Poll the batch until it reaches a terminal state, then return results.

        Args:
            batch_id:        Batch ID returned by submit().
            timeout_seconds: Maximum wait time before raising TimeoutError.
            poll_interval:   Seconds between status checks.
        """
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        attempts = 0

        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(
                    f"Batch {batch_id} did not complete within {timeout_seconds}s"
                )

            def _status():
                return self._anthropic.messages.batches.retrieve(batch_id)

            batch = await asyncio.to_thread(_status)
            attempts += 1
            logger.debug(
                "BATCH_POLL | id={} | status={} | attempt={}",
                batch_id,
                batch.processing_status,
                attempts,
            )

            if batch.processing_status == "ended":
                return await self.get_results(batch_id)

            await asyncio.sleep(poll_interval)

    async def get_results(self, batch_id: str) -> list[BatchResult]:
        """
        Fetch and parse results for a completed batch.

        Returns one BatchResult per submitted request.
        """
        def _fetch():
            return list(self._anthropic.messages.batches.results(batch_id))

        raw_results = await asyncio.to_thread(_fetch)
        results: list[BatchResult] = []

        for item in raw_results:
            custom_id = item.custom_id
            outcome = item.result

            if outcome.type == "succeeded":
                msg = outcome.message
                text = msg.content[0].text if msg.content else ""
                results.append(BatchResult(
                    custom_id=custom_id,
                    success=True,
                    text=text,
                    input_tokens=getattr(msg.usage, "input_tokens", 0),
                    output_tokens=getattr(msg.usage, "output_tokens", 0),
                ))
            else:
                error_msg = getattr(outcome, "error", None)
                results.append(BatchResult(
                    custom_id=custom_id,
                    success=False,
                    error=str(error_msg) if error_msg else f"type={outcome.type}",
                ))

        succeeded = sum(1 for r in results if r.success)
        logger.info(
            "BATCH_RESULTS | id={} | total={} | succeeded={} | failed={}",
            batch_id,
            len(results),
            succeeded,
            len(results) - succeeded,
        )
        return results


# ── Module-level singleton ────────────────────────────────────────────────────

batch_client = BatchClient()
