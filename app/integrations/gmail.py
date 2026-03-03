"""
Gmail Integration

Operations:
  list_emails(query, max_results) — search / list emails
  get_email(msg_id)               — fetch full email body
  send_email(to, subject, body)   — send via Gmail API
  reply_email(msg_id, body)       — reply in-thread
  mark_read(msg_id)               — mark a message as read
  create_draft(to, subject, body) — save draft without sending
  list_labels()                   — list all Gmail labels

Auth: Google OAuth 2.0 with a stored refresh token.
      Run scripts/google_auth.py once to obtain GOOGLE_REFRESH_TOKEN.
"""

import asyncio
import base64
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailClient:
    def __init__(self) -> None:
        self._service = None

    def is_configured(self) -> bool:
        return bool(
            settings.google_client_id
            and settings.google_client_secret
            and settings.google_refresh_token
        )

    def _build_service(self):
        """Build and return a Gmail API service object (cached)."""
        if self._service is not None:
            return self._service
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=settings.google_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            scopes=_SCOPES,
        )
        creds.refresh(Request())
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    # ── Sync internals ────────────────────────────────────────────────────────

    def _list_emails_sync(self, query: str, max_results: int) -> list[dict]:
        svc = self._build_service()
        result = svc.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        messages = result.get("messages", [])
        emails = []
        for msg in messages:
            detail = svc.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            emails.append({
                "id":      msg["id"],
                "from":    headers.get("From", ""),
                "to":      headers.get("To", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "date":    headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
            })
        return emails

    def _get_email_sync(self, msg_id: str) -> dict:
        svc  = self._build_service()
        msg  = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

        body = ""
        parts = payload.get("parts", [payload])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                    break

        return {
            "id":      msg_id,
            "from":    headers.get("From", ""),
            "to":      headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "date":    headers.get("Date", ""),
            "body":    body,
        }

    def _send_email_sync(self, to: str, subject: str, body: str) -> dict:
        svc = self._build_service()
        mime_msg = MIMEText(body)
        mime_msg["to"]      = to
        mime_msg["subject"] = subject
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"id": sent.get("id"), "thread_id": sent.get("threadId")}

    def _reply_email_sync(self, msg_id: str, body: str) -> dict:
        """Reply in the same thread as msg_id."""
        svc = self._build_service()
        # Fetch original to get thread_id and headers
        orig = svc.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Subject", "Message-ID", "References"],
        ).execute()
        headers = {h["name"]: h["value"] for h in orig.get("payload", {}).get("headers", [])}
        thread_id   = orig.get("threadId", "")
        to_addr     = headers.get("From", "")
        subject     = headers.get("Subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        orig_msg_id = headers.get("Message-ID", "")
        references  = headers.get("References", "") + f" {orig_msg_id}".strip()

        mime_msg = MIMEText(body)
        mime_msg["to"]          = to_addr
        mime_msg["subject"]     = subject
        mime_msg["In-Reply-To"] = orig_msg_id
        mime_msg["References"]  = references.strip()
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        sent = svc.users().messages().send(
            userId="me", body={"raw": raw, "threadId": thread_id}
        ).execute()
        return {"id": sent.get("id"), "thread_id": thread_id, "to": to_addr}

    def _mark_read_sync(self, msg_id: str) -> dict:
        svc = self._build_service()
        svc.users().messages().modify(
            userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        return {"id": msg_id, "marked_read": True}

    def _list_labels_sync(self) -> list[dict]:
        svc = self._build_service()
        result = svc.users().labels().list(userId="me").execute()
        return [
            {"id": l["id"], "name": l["name"]}
            for l in result.get("labels", [])
        ]

    def _create_draft_sync(self, to: str, subject: str, body: str) -> dict:
        svc = self._build_service()
        mime_msg = MIMEText(body)
        mime_msg["to"]      = to
        mime_msg["subject"] = subject
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        draft = svc.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
        return {"draft_id": draft.get("id")}

    # ── Public async API ──────────────────────────────────────────────────────

    async def list_emails(self, query: str = "is:unread", max_results: int = 10) -> list[dict]:
        return await asyncio.to_thread(self._list_emails_sync, query, max_results)

    async def get_email(self, msg_id: str) -> dict:
        return await asyncio.to_thread(self._get_email_sync, msg_id)

    async def send_email(self, to: str, subject: str, body: str) -> dict:
        return await asyncio.to_thread(self._send_email_sync, to, subject, body)

    async def reply_email(self, msg_id: str, body: str) -> dict:
        return await asyncio.to_thread(self._reply_email_sync, msg_id, body)

    async def mark_read(self, msg_id: str) -> dict:
        return await asyncio.to_thread(self._mark_read_sync, msg_id)

    async def list_labels(self) -> list[dict]:
        return await asyncio.to_thread(self._list_labels_sync)

    async def create_draft(self, to: str, subject: str, body: str) -> dict:
        return await asyncio.to_thread(self._create_draft_sync, to, subject, body)
