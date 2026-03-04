"""
Redis Hot Memory — stores short-term conversation history per session.

TTL:       4 hours (auto-expires idle sessions)
Max turns: 20 message pairs (user + assistant)

Key format:  session:{session_id}
Value:       JSON-serialised list of Anthropic-format message dicts
             [{"role": "user"|"assistant", "content": str}, ...]
"""

import json
import redis

from app.config import get_settings

settings = get_settings()

HISTORY_TTL = 4 * 60 * 60   # 4 hours in seconds
MAX_TURNS   = 20              # max (user, assistant) pairs kept


class RedisMemory:
    def __init__(self) -> None:
        self._client: redis.Redis | None = None

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                password=settings.redis_password,
                decode_responses=True,
                socket_connect_timeout=5,
            )
        return self._client

    # ── Internal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _key(session_id: str) -> str:
        return f"session:{session_id}"

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_history(self, session_id: str) -> list[dict]:
        """Return the full conversation history for a session (or [] if none)."""
        raw = self.client.get(self._key(session_id))
        if not raw:
            return []
        return json.loads(raw)

    def append_turn(
        self,
        session_id: str,
        user_msg: str,
        assistant_msg: str,
    ) -> None:
        """
        Append one (user, assistant) exchange and refresh the TTL.
        Trims history to MAX_TURNS pairs if it grows too large.
        """
        history = self.get_history(session_id)
        history.append({"role": "user",      "content": user_msg})
        history.append({"role": "assistant", "content": assistant_msg})

        # Keep only the most recent MAX_TURNS pairs
        max_messages = MAX_TURNS * 2
        if len(history) > max_messages:
            history = history[-max_messages:]

        key = self._key(session_id)
        self.client.setex(key, HISTORY_TTL, json.dumps(history))

    def clear_session(self, session_id: str) -> None:
        """Delete a session's history immediately."""
        self.client.delete(self._key(session_id))

    def ttl(self, session_id: str) -> int:
        """Return remaining TTL in seconds (-2 = key doesn't exist)."""
        return self.client.ttl(self._key(session_id))

    def ping(self) -> bool:
        """Health-check the Redis connection."""
        try:
            return self.client.ping()
        except Exception:
            return False

    # ── Pending action (write-op confirmation flow) ────────────────────────────

    def set_pending_action(self, session_id: str, action: dict) -> None:
        """Store a pending write action awaiting user confirmation (5-min TTL)."""
        key = f"pending:{session_id}"
        self.client.setex(key, 300, json.dumps(action))

    def get_pending_action(self, session_id: str) -> dict | None:
        """Return the pending action dict, or None if none exists."""
        raw = self.client.get(f"pending:{session_id}")
        return json.loads(raw) if raw else None

    def clear_pending_action(self, session_id: str) -> None:
        """Delete the pending action for a session."""
        self.client.delete(f"pending:{session_id}")

    # ── Approval level (global) ────────────────────────────────────────────────

    _APPROVAL_KEY = "brain:approval_level"

    def get_approval_level(self) -> int:
        """Return global approval level (1, 2, or 3). Default is 1."""
        val = self.client.get(self._APPROVAL_KEY)
        try:
            return max(1, min(3, int(val))) if val else 1
        except (ValueError, TypeError):
            return 1

    def set_approval_level(self, level: int) -> None:
        """Persist global approval level (1-3, no TTL — permanent until changed)."""
        self.client.set(self._APPROVAL_KEY, str(max(1, min(3, level))))

    # ── Slack context (channel + thread for report-back) ──────────────────────

    # ── Workspace lock (serialises sentinel-workspace access) ─────────────────

    _WORKSPACE_LOCK_KEY = "lock:sentinel_workspace"
    _WORKSPACE_LOCK_TTL = 600   # 10 min max hold time — hard ceiling per task

    def acquire_workspace_lock(self, holder_id: str) -> bool:
        """
        Try to acquire the exclusive sentinel-workspace lock.
        Returns True if the lock was acquired (SET NX succeeded).
        `holder_id` is the Celery task ID — stored so we can identify who holds it.
        """
        return bool(
            self.client.set(
                self._WORKSPACE_LOCK_KEY,
                holder_id,
                nx=True,
                ex=self._WORKSPACE_LOCK_TTL,
            )
        )

    def release_workspace_lock(self, holder_id: str) -> bool:
        """
        Release the lock ONLY if still held by `holder_id`.
        Uses a Lua script for an atomic check-and-delete.
        """
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "  return redis.call('del', KEYS[1]) "
            "else return 0 end"
        )
        return bool(self.client.eval(script, 1, self._WORKSPACE_LOCK_KEY, holder_id))

    def get_workspace_lock_holder(self) -> str | None:
        """Return the holder_id of whoever currently holds the workspace lock, or None."""
        return self.client.get(self._WORKSPACE_LOCK_KEY)

    # ── Slack context (channel + thread for report-back) ──────────────────────

    def set_slack_context(self, session_id: str, channel: str, thread_ts: str) -> None:
        """Store the Slack channel + thread_ts for the current turn (5-min TTL)."""
        key = f"slack_ctx:{session_id}"
        self.client.setex(key, 300, json.dumps({"channel": channel, "thread_ts": thread_ts}))

    def get_slack_context(self, session_id: str) -> dict | None:
        """Return {channel, thread_ts} for the session, or None if not set."""
        raw = self.client.get(f"slack_ctx:{session_id}")
        return json.loads(raw) if raw else None

    # ── Chat follow-up queue (async replies back to Grafana/REST chat) ─────────

    def push_followup(self, session_id: str, message: str) -> None:
        """Queue a follow-up message for a chat session (TTL 10 min)."""
        key = f"chat_followup:{session_id}"
        self.client.rpush(key, message)
        self.client.expire(key, 600)

    def pop_followup(self, session_id: str) -> str | None:
        """Pop and return the oldest queued follow-up, or None if empty."""
        return self.client.lpop(f"chat_followup:{session_id}")
