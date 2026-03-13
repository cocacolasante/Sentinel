"""
GoalQueueSkill — Redis sorted-set goal queue.

Redis key layout:
  sorted set:  sentinel:goals:queue  (score = priority float)
  goal data:   sentinel:goals:{goal_id}  (Redis hash)

Trigger intents: goal (add goal), goal_status (list/check goals)
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.skills.base import BaseSkill, SkillResult

_QUEUE_KEY = "sentinel:goals:queue"
_GOAL_TTL = 7 * 86400  # 7 days


@dataclass
class Goal:
    id: str
    title: str
    description: str
    created_by: str           # "user:{slack_user}" | "skill:{skill_name}" | "wake_loop"
    created_at: datetime
    priority: float           # 0.0–10.0
    status: str               # pending | running | completed | failed | cancelled
    deadline: Optional[datetime] = None
    skill_hint: Optional[str] = None
    parent_goal_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "priority": self.priority,
            "status": self.status,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "skill_hint": self.skill_hint,
            "parent_goal_id": self.parent_goal_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Goal":
        return cls(
            id=d["id"],
            title=d["title"],
            description=d.get("description", ""),
            created_by=d.get("created_by", "unknown"),
            created_at=datetime.fromisoformat(d["created_at"]) if isinstance(d.get("created_at"), str) else datetime.now(timezone.utc),
            priority=float(d.get("priority", 5.0)),
            status=d.get("status", "pending"),
            deadline=datetime.fromisoformat(d["deadline"]) if d.get("deadline") else None,
            skill_hint=d.get("skill_hint"),
            parent_goal_id=d.get("parent_goal_id"),
            metadata=d.get("metadata", {}),
        )


def compute_priority(urgency: float, deadline: Optional[datetime] = None) -> float:
    """
    Compute a priority score (0–10).
    If a deadline is set, boost by how soon it is (decays by hours remaining).
    """
    base = max(0.0, min(10.0, urgency))
    if deadline:
        now = datetime.now(timezone.utc)
        hours_remaining = max(0, (deadline - now).total_seconds() / 3600)
        if hours_remaining < 24:
            boost = 2.0 * (1 - hours_remaining / 24)
            base = min(10.0, base + boost)
    return base


_INSTANCE: Optional["GoalQueueSkill"] = None


def get_goal_queue() -> "GoalQueueSkill":
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = GoalQueueSkill()
    return _INSTANCE


class GoalQueueSkill(BaseSkill):
    name = "goal_queue"
    description = "Manage the autonomous goal queue (enqueue, dequeue, list goals)"
    trigger_intents = ["goal", "goal_status"]

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        from app.config import get_settings
        settings = get_settings()

        intent = params.get("_intent", "goal")
        action = params.get("action", "")

        if intent == "goal_status" or action in ("list", "status"):
            limit = int(params.get("limit", 10))
            goals = await self.peek(limit)
            return SkillResult(
                context_data=json.dumps({"goals": [g.to_dict() for g in goals]})
            )

        # Add a new goal
        title = params.get("title", original_message[:200])
        description = params.get("description", "")
        priority_raw = float(params.get("priority", 5.0))
        created_by = params.get("created_by", "user:slack")
        skill_hint = params.get("skill_hint")
        deadline_str = params.get("deadline")
        deadline = datetime.fromisoformat(deadline_str) if deadline_str else None

        # Cap auto-created goals
        if not created_by.startswith("user:"):
            priority_raw = min(priority_raw, settings.sentinel_goal_max_priority_auto)

        priority = compute_priority(priority_raw, deadline)

        goal = Goal(
            id=str(uuid.uuid4()),
            title=title,
            description=description,
            created_by=created_by,
            created_at=datetime.now(timezone.utc),
            priority=priority,
            status="pending",
            deadline=deadline,
            skill_hint=skill_hint,
            metadata=params.get("metadata", {}),
        )

        await self.enqueue(goal)

        # Update metrics
        try:
            from app.observability.prometheus_metrics import GOAL_QUEUE_DEPTH
            depth = await self._queue_depth()
            GOAL_QUEUE_DEPTH.set(depth)
        except Exception:
            pass

        return SkillResult(
            context_data=json.dumps({
                "status": "enqueued",
                "goal_id": goal.id,
                "title": goal.title,
                "priority": goal.priority,
            })
        )

    async def _queue_depth(self) -> int:
        from app.db.redis import get_redis
        redis = await get_redis()
        return await redis.zcard(_QUEUE_KEY)

    async def enqueue(self, goal: Goal) -> None:
        from app.db.redis import get_redis
        redis = await get_redis()
        async with redis.pipeline() as pipe:
            pipe.zadd(_QUEUE_KEY, {goal.id: goal.priority})
            pipe.setex(f"sentinel:goals:{goal.id}", _GOAL_TTL, json.dumps(goal.to_dict()))
            await pipe.execute()

    async def dequeue(self, n: int = 1) -> list[Goal]:
        """Pop the top-N highest priority pending goals."""
        from app.db.redis import get_redis
        redis = await get_redis()

        # Get highest-priority IDs (highest score first)
        ids = await redis.zrevrange(_QUEUE_KEY, 0, n - 1)
        goals = []
        for gid in ids:
            gid_str = gid.decode() if isinstance(gid, bytes) else gid
            raw = await redis.get(f"sentinel:goals:{gid_str}")
            if raw:
                data = json.loads(raw)
                if data.get("status") == "pending":
                    goal = Goal.from_dict(data)
                    goals.append(goal)
                    await self.update_status(gid_str, "running")
                    await redis.zrem(_QUEUE_KEY, gid_str)
        return goals

    async def peek(self, n: int = 10) -> list[Goal]:
        """Return top-N goals without removing them."""
        from app.db.redis import get_redis
        redis = await get_redis()

        ids = await redis.zrevrange(_QUEUE_KEY, 0, n - 1)
        goals = []
        for gid in ids:
            gid_str = gid.decode() if isinstance(gid, bytes) else gid
            raw = await redis.get(f"sentinel:goals:{gid_str}")
            if raw:
                try:
                    goals.append(Goal.from_dict(json.loads(raw)))
                except Exception:
                    pass
        return goals

    async def update_status(self, goal_id: str, status: str) -> None:
        from app.db.redis import get_redis
        redis = await get_redis()
        raw = await redis.get(f"sentinel:goals:{goal_id}")
        if raw:
            data = json.loads(raw)
            data["status"] = status
            await redis.setex(f"sentinel:goals:{goal_id}", _GOAL_TTL, json.dumps(data))

    async def cancel(self, goal_id: str) -> None:
        from app.db.redis import get_redis
        redis = await get_redis()
        await self.update_status(goal_id, "cancelled")
        await redis.zrem(_QUEUE_KEY, goal_id)

    async def get(self, goal_id: str) -> Optional[Goal]:
        from app.db.redis import get_redis
        redis = await get_redis()
        raw = await redis.get(f"sentinel:goals:{goal_id}")
        if raw:
            return Goal.from_dict(json.loads(raw))
        return None
