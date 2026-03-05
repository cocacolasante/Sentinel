"""
GitHub Webhook Router

Receives GitHub webhook events at POST /api/v1/github/webhook.
Verifies the HMAC-SHA256 signature using GITHUB_WEBHOOK_SECRET.

Handled events:
  pull_request  (opened, synchronize, reopened)
    → triggers review_and_merge_pr Celery task for sentinel/* branches

Setup:
  In your GitHub repo → Settings → Webhooks → Add webhook:
    Payload URL : https://sentinelai.cloud/api/v1/github/webhook
    Content type: application/json
    Secret      : value of GITHUB_WEBHOOK_SECRET in .env
    Events      : Pull requests
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


async def _verify_signature(request: Request, x_hub_signature_256: str | None) -> bytes:
    """Verify GitHub HMAC-SHA256 signature. Raises 401 on mismatch."""
    body = await request.body()
    secret = settings.github_webhook_secret
    if not secret:
        # No secret configured — accept all (dev/testing only)
        logger.warning("GITHUB_WEBHOOK_SECRET not set — skipping signature check")
        return body

    if not x_hub_signature_256:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature header")

    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256  # type: ignore[attr-defined]
    ).hexdigest()

    if not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Signature mismatch")

    return body


@router.post("/github/webhook", status_code=status.HTTP_202_ACCEPTED, tags=["github"])
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
):
    """Receive GitHub webhook events and dispatch Celery tasks."""
    body = await _verify_signature(request, x_hub_signature_256)

    import json
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = x_github_event or "unknown"
    logger.info("GitHub webhook received | event=%s", event)

    if event == "pull_request":
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        pr_number = pr.get("number")
        branch = pr.get("head", {}).get("ref", "")

        if action in ("opened", "synchronize", "reopened") and pr_number:
            # Only auto-review sentinel/* branches — human PRs need human review
            if branch.startswith("sentinel/"):
                from app.worker.pr_tasks import review_and_merge_pr
                review_and_merge_pr.apply_async(args=[pr_number], queue="tasks_workspace")
                logger.info("Dispatched review_and_merge_pr for PR #%d (%s)", pr_number, branch)
                return {"queued": True, "pr_number": pr_number, "branch": branch}
            else:
                logger.info("Skipping non-sentinel PR #%d branch=%s", pr_number, branch)
                return {"queued": False, "reason": "non-sentinel branch"}

    return {"queued": False, "event": event}
