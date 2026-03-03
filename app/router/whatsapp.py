"""
WhatsApp Incoming Webhook Router

Twilio sends POST requests here when someone messages your WhatsApp number.
The Brain processes the message and replies via Twilio.

Twilio webhook URL to configure:
  https://your-domain.com/api/v1/whatsapp/incoming
  Method: POST
"""

from __future__ import annotations

import logging
from urllib.parse import unquote_plus

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


@router.post("/incoming")
async def whatsapp_incoming(
    request: Request,
    From: str = Form(""),
    Body: str = Form(""),
    MessageSid: str = Form(""),
    NumMedia: str = Form("0"),
    WaId: str = Form(""),
):
    """
    Handle incoming WhatsApp messages from Twilio.
    Strips the whatsapp: prefix, processes through Brain, and replies.
    Returns 200 immediately if WhatsApp is not configured (so Twilio won't retry).
    """
    from app.integrations.whatsapp import WhatsAppClient
    if not WhatsAppClient().is_configured():
        logger.debug("WhatsApp incoming webhook hit but Twilio is not configured — ignoring")
        return PlainTextResponse("", status_code=200)

    from app.brain.dispatcher import Dispatcher

    # Twilio sends From as "whatsapp:+12125551234"
    sender   = From.replace("whatsapp:", "").strip() or WaId
    body     = Body.strip()
    media_ct = int(NumMedia or 0)

    if not body and not media_ct:
        return PlainTextResponse("", status_code=200)

    logger.info("WhatsApp incoming | from={} | sid={} | body={}", sender, MessageSid, body[:80])

    # Use sender phone as session_id so context persists per WhatsApp conversation
    session_id = f"whatsapp-{sender.lstrip('+')}"

    if media_ct > 0:
        body = f"[Media attachment] {body}".strip()

    try:
        dispatcher = Dispatcher()
        result     = await dispatcher.process(body, session_id)
        reply_text = result.reply

        # Send reply via Twilio
        client = WhatsAppClient()
        if client.is_configured():
            await client.send(to=From, body=reply_text[:1600])  # WhatsApp 1600 char limit
        else:
            logger.warning("WhatsApp not configured — reply not sent for incoming message from {}", sender)

    except Exception as exc:
        logger.error("WhatsApp incoming handler error: {}", exc)
        # Don't crash — return 200 so Twilio doesn't retry
        return PlainTextResponse("", status_code=200)

    # Twilio expects a TwiML response OR empty 200 (we already sent reply via API above)
    return PlainTextResponse("", status_code=200)


@router.get("/status")
async def whatsapp_status():
    """Check WhatsApp integration status."""
    from app.integrations.whatsapp import WhatsAppClient
    configured = WhatsAppClient().is_configured()
    return {"configured": configured}
