"""
ServerInventorySkill — managed server registry.

Not user-triggerable (no trigger_intents). Instantiated directly by other
Phase 3 skills that need to enumerate or look up managed servers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.skills.base import BaseSkill, SkillResult


@dataclass
class ServerRecord:
    id: int
    hostname: str
    ip_address: Optional[str]
    ssh_user: str
    ssh_key_path: Optional[str]
    os: Optional[str]
    role: Optional[str]
    owner: Optional[str]
    meshcentral_node_id: Optional[str]
    last_seen: Optional[datetime]
    tags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "hostname": self.hostname,
            "ip_address": self.ip_address,
            "ssh_user": self.ssh_user,
            "ssh_key_path": self.ssh_key_path,
            "os": self.os,
            "role": self.role,
            "owner": self.owner,
            "meshcentral_node_id": self.meshcentral_node_id,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "tags": self.tags,
        }

    @classmethod
    def from_row(cls, row: dict) -> "ServerRecord":
        return cls(
            id=row["id"],
            hostname=row["hostname"],
            ip_address=row.get("ip_address"),
            ssh_user=row.get("ssh_user", "root"),
            ssh_key_path=row.get("ssh_key_path"),
            os=row.get("os"),
            role=row.get("role"),
            owner=row.get("owner"),
            meshcentral_node_id=row.get("meshcentral_node_id"),
            last_seen=row.get("last_seen"),
            tags=row.get("tags") or {},
        )


class ServerInventorySkill(BaseSkill):
    name = "server_inventory"
    description = "Managed server registry — list, get, upsert managed servers"
    trigger_intents: list[str] = []  # internal — no user trigger

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        return SkillResult(context_data="ServerInventorySkill is internal-only")

    async def list(
        self,
        owner: str | None = None,
        role: str | None = None,
        tag: str | None = None,
    ) -> list[ServerRecord]:
        from app.db.postgres import execute
        where_clauses = []
        args: list = []
        idx = 1
        if owner:
            where_clauses.append(f"owner = ${idx}")
            args.append(owner)
            idx += 1
        if role:
            where_clauses.append(f"role = ${idx}")
            args.append(role)
            idx += 1
        if tag:
            where_clauses.append(f"tags ? ${idx}")
            args.append(tag)
            idx += 1
        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        rows = await execute(f"SELECT * FROM managed_servers {where} ORDER BY hostname", *args)
        return [ServerRecord.from_row(dict(r)) for r in (rows or [])]

    async def get(self, hostname: str) -> ServerRecord | None:
        from app.db.postgres import execute
        rows = await execute(
            "SELECT * FROM managed_servers WHERE hostname = $1", hostname
        )
        if rows:
            return ServerRecord.from_row(dict(rows[0]))
        return None

    async def upsert(self, record: ServerRecord) -> None:
        from app.db.postgres import execute
        tags_json = json.dumps(record.tags)
        await execute(
            """
            INSERT INTO managed_servers
                (hostname, ip_address, ssh_user, ssh_key_path, os, role, owner,
                 meshcentral_node_id, tags)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            ON CONFLICT (hostname) DO UPDATE SET
                ip_address          = EXCLUDED.ip_address,
                ssh_user            = EXCLUDED.ssh_user,
                ssh_key_path        = EXCLUDED.ssh_key_path,
                os                  = EXCLUDED.os,
                role                = EXCLUDED.role,
                owner               = EXCLUDED.owner,
                meshcentral_node_id = EXCLUDED.meshcentral_node_id,
                tags                = EXCLUDED.tags
            """,
            record.hostname, record.ip_address, record.ssh_user,
            record.ssh_key_path, record.os, record.role, record.owner,
            record.meshcentral_node_id, tags_json,
        )

    async def mark_seen(self, hostname: str) -> None:
        from app.db.postgres import execute
        await execute(
            "UPDATE managed_servers SET last_seen = NOW() WHERE hostname = $1",
            hostname,
        )
