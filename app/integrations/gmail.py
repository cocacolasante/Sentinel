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

Multi-account: use get_gmail_client(account_name) to get a client for a
specific account.  Pass account_name=None (or call GmailClient() with no
args) to use the primary / first configured account.
"""

import asyncio
import base64
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
]


def _resolve_account(account_name: str | None) -> dict | None:
    """Return the account config dict for *account_name*, or the primary account."""
    accounts = settings.google_accounts
    if not accounts:
        return None
    if account_name is None:
        return accounts[0]
    name_lower = account_name.lower()
    for acc in accounts:
        if acc["name"].lower() == name_lower:
            return acc
    return accounts[0]  # fallback to primary


def get_gmail_client(account_name: str | None = None) -> "GmailClient":
    """Factory: return a GmailClient for the named account (or primary)."""
    return GmailClient(account_config=_resolve_account(account_name))


class GmailClient:
    def __init__(self, account_config: dict | None = None) -> None:
        # Accept an explicit account dict, or resolve from primary
        self._account = account_config if account_config is not None else _resolve_account(None)
        self._service = None

    @property
    def account_name(self) -> str:
        return (self._account or {}).get("name", "unknown")

    def is_configured(self) -> bool:
        return bool(
            self._account
            and self._account.get("client_id")
            and self._account.get("client_secret")
            and self._account.get("refresh_token")
        )

    def _build_service(self):
        """Build and return a Gmail API service object (cached per instance)."""
        if self._service is not None:
            return self._service
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=self._account["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._account["client_id"],
            client_secret=self._account["client_secret"],
            scopes=_SCOPES,
        )
        creds.refresh(Request())
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    # ── Sync internals ────────────────────────────────────────────────────────

    def _list_emails_sync(self, query: str, max_results: int) -> list[dict]:
        svc = self._build_service()
        result = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        messages = result.get("messages", [])
        emails = []
        for msg in messages:
            detail = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=msg["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                )
                .execute()
            )
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            emails.append(
                {
                    "id": msg["id"],
                    "from": headers.get("From", ""),
                    "to": headers.get("To", ""),
                    "subject": headers.get("Subject", "(no subject)"),
                    "date": headers.get("Date", ""),
                    "snippet": detail.get("snippet", ""),
                }
            )
        return emails

    def _get_email_sync(self, msg_id: str) -> dict:
        svc = self._build_service()
        msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
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
            "id": msg_id,
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "body": body,
        }

    def _send_email_sync(self, to: str, subject: str, body: str) -> dict:
        svc = self._build_service()
        mime_msg = MIMEText(body)
        mime_msg["to"] = to
        mime_msg["subject"] = subject
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"id": sent.get("id"), "thread_id": sent.get("threadId")}

    def _reply_email_sync(self, msg_id: str, body: str) -> dict:
        """Reply in the same thread as msg_id."""
        svc = self._build_service()
        orig = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Message-ID", "References"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in orig.get("payload", {}).get("headers", [])}
        thread_id = orig.get("threadId", "")
        to_addr = headers.get("From", "")
        subject = headers.get("Subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        orig_msg_id = headers.get("Message-ID", "")
        references = headers.get("References", "") + f" {orig_msg_id}".strip()

        mime_msg = MIMEText(body)
        mime_msg["to"] = to_addr
        mime_msg["subject"] = subject
        mime_msg["In-Reply-To"] = orig_msg_id
        mime_msg["References"] = references.strip()
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        sent = svc.users().messages().send(userId="me", body={"raw": raw, "threadId": thread_id}).execute()
        return {"id": sent.get("id"), "thread_id": thread_id, "to": to_addr}

    def _mark_read_sync(self, msg_id: str) -> dict:
        svc = self._build_service()
        svc.users().messages().modify(userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}).execute()
        return {"id": msg_id, "marked_read": True}

    def _list_labels_sync(self) -> list[dict]:
        svc = self._build_service()
        result = svc.users().labels().list(userId="me").execute()
        return [{"id": l["id"], "name": l["name"]} for l in result.get("labels", [])]

    def _create_draft_sync(self, to: str, subject: str, body: str) -> dict:
        svc = self._build_service()
        mime_msg = MIMEText(body)
        mime_msg["to"] = to
        mime_msg["subject"] = subject
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
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
