"""
HMAC-SHA256 signing and verification for agent messages.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional


def sign(msg_type: str, payload: dict, secret: str, ts: Optional[float] = None) -> str:
    """
    Sign a message payload. Returns the HMAC hex digest.

    canonical = f"{ts}:{msg_type}:{json.dumps(payload, sort_keys=True, separators=(',',':'))}"
    """
    if ts is None:
        ts = time.time()
    canonical = (
        f"{ts}:{msg_type}:"
        + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def build_message(msg_type: str, payload: dict, secret: str) -> dict:
    """Build a complete signed message dict ready to send over the wire."""
    ts = time.time()
    sig = sign(msg_type, payload, secret, ts=ts)
    return {
        "type": msg_type,
        "payload": payload,
        "ts": ts,
        "sig": sig,
    }


def verify(msg: dict, secret: str, max_drift: int = 60) -> bool:
    """Verify a received message's timestamp freshness and HMAC signature."""
    try:
        ts = float(msg.get("ts", 0))
        if abs(time.time() - ts) > max_drift:
            return False
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        expected = sign(msg_type, payload, secret, ts=ts)
        return hmac.compare_digest(expected, msg.get("sig", ""))
    except Exception:
        return False
