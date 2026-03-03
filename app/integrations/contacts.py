"""
Contacts Integration — address book backed by PostgreSQL.

Operations:
  search(query)          — fuzzy name / email search
  get_by_name(name)      — closest name match
  get_by_email(email)    — exact email lookup
  get_all()              — list all contacts
  add(...)               — insert a new contact
  update(id, fields)     — update fields on a contact
  delete(id)             — remove a contact
  resolve_to_email(name) — returns email if name is in contacts, else None
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ContactsClient:
    """Synchronous contact operations wrapped in async helpers."""

    # ── Sync internals ────────────────────────────────────────────────────────

    def _search_sync(self, query: str, limit: int = 10) -> list[dict]:
        from app.db import postgres
        q = f"%{query.lower()}%"
        rows = postgres.fetch(
            """
            SELECT id, name, email, phone, whatsapp, company, github, slack_id, tags, notes
              FROM contacts
             WHERE lower(name)    LIKE %s
                OR lower(email)   LIKE %s
                OR lower(company) LIKE %s
                OR lower(tags)    LIKE %s
             ORDER BY name
             LIMIT %s
            """,
            (q, q, q, q, limit),
        )
        return [dict(r) for r in rows]

    def _get_all_sync(self, limit: int = 100) -> list[dict]:
        from app.db import postgres
        rows = postgres.fetch(
            "SELECT id, name, email, phone, whatsapp, company, github, slack_id, tags, notes "
            "FROM contacts ORDER BY name LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in rows]

    def _get_by_id_sync(self, contact_id: int) -> dict | None:
        from app.db import postgres
        rows = postgres.fetch(
            "SELECT id, name, email, phone, whatsapp, company, github, slack_id, tags, notes "
            "FROM contacts WHERE id = %s",
            (contact_id,),
        )
        return dict(rows[0]) if rows else None

    def _get_by_email_sync(self, email: str) -> dict | None:
        from app.db import postgres
        rows = postgres.fetch(
            "SELECT id, name, email, phone, whatsapp, company, github, slack_id, tags, notes "
            "FROM contacts WHERE lower(email) = lower(%s) LIMIT 1",
            (email,),
        )
        return dict(rows[0]) if rows else None

    def _get_by_name_sync(self, name: str) -> list[dict]:
        """Return contacts whose name contains the query (case-insensitive)."""
        from app.db import postgres
        q = f"%{name.lower()}%"
        rows = postgres.fetch(
            "SELECT id, name, email, phone, whatsapp, company, github, slack_id, tags, notes "
            "FROM contacts WHERE lower(name) LIKE %s ORDER BY name LIMIT 5",
            (q,),
        )
        return [dict(r) for r in rows]

    def _add_sync(
        self,
        name: str,
        email: str = "",
        phone: str = "",
        whatsapp: str = "",
        company: str = "",
        github: str = "",
        slack_id: str = "",
        tags: str = "",
        notes: str = "",
    ) -> dict:
        from app.db import postgres
        rows = postgres.fetch(
            """
            INSERT INTO contacts (name, email, phone, whatsapp, company, github, slack_id, tags, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, name, email, phone, whatsapp, company
            """,
            (name, email or None, phone or None, whatsapp or None,
             company or None, github or None, slack_id or None,
             tags or None, notes or None),
        )
        return dict(rows[0]) if rows else {"error": "insert failed"}

    def _update_sync(self, contact_id: int, fields: dict) -> dict:
        from app.db import postgres
        allowed = {"name", "email", "phone", "whatsapp", "company", "github", "slack_id", "tags", "notes"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return {"error": "no valid fields to update"}
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        values     = list(updates.values()) + [contact_id]
        postgres.execute(
            f"UPDATE contacts SET {set_clause}, updated_at = NOW() WHERE id = %s",
            values,
        )
        return self._get_by_id_sync(contact_id) or {"error": "not found after update"}

    def _delete_sync(self, contact_id: int) -> dict:
        from app.db import postgres
        postgres.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
        return {"deleted": True, "id": contact_id}

    # ── Public async API ──────────────────────────────────────────────────────

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        return await asyncio.to_thread(self._search_sync, query, limit)

    async def get_all(self, limit: int = 100) -> list[dict]:
        return await asyncio.to_thread(self._get_all_sync, limit)

    async def get_by_name(self, name: str) -> list[dict]:
        return await asyncio.to_thread(self._get_by_name_sync, name)

    async def get_by_email(self, email: str) -> dict | None:
        return await asyncio.to_thread(self._get_by_email_sync, email)

    async def add(self, name: str, **kwargs) -> dict:
        return await asyncio.to_thread(self._add_sync, name, **kwargs)

    async def update(self, contact_id: int, fields: dict) -> dict:
        return await asyncio.to_thread(self._update_sync, contact_id, fields)

    async def delete(self, contact_id: int) -> dict:
        return await asyncio.to_thread(self._delete_sync, contact_id)

    async def resolve_to_email(self, name_or_email: str) -> str | None:
        """
        If name_or_email looks like an email address, return it directly.
        Otherwise search contacts by name and return the first email found.
        """
        if "@" in name_or_email:
            return name_or_email
        matches = await self.get_by_name(name_or_email)
        for m in matches:
            if m.get("email"):
                return m["email"]
        return None

    async def resolve_to_phone(self, name_or_phone: str) -> str | None:
        """
        If name_or_phone starts with + or is all digits, return it.
        Otherwise search contacts and return whatsapp or phone.
        """
        stripped = name_or_phone.lstrip("+").replace(" ", "").replace("-", "")
        if stripped.isdigit():
            return name_or_phone
        matches = await self.get_by_name(name_or_phone)
        for m in matches:
            return m.get("whatsapp") or m.get("phone") or None
        return None
