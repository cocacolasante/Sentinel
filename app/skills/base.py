"""
BaseSkill — abstract base class for all skills in the PAI skill system.

Each skill handles one or more intents from the IntentClassifier. Skills are
self-describing (name, description, trigger_intents) and can report whether
they are currently available (e.g., credentials configured).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class ApprovalCategory(str, Enum):
    """Controls when a skill requires user confirmation based on global approval level.

    Level 1  — confirm STANDARD, CRITICAL, BREAKING  (strictest)
    Level 2  — confirm CRITICAL, BREAKING             (critical writes only)
    Level 3  — confirm only BREAKING                  (breaking changes only)
    """

    NONE = "none"  # read ops — never require confirmation
    STANDARD = "standard"  # normal writes: email, calendar event
    CRITICAL = "critical"  # significant writes: GitHub, smart-home state changes
    BREAKING = "breaking"  # irreversible/destructive — always confirm


@dataclass
class SkillResult:
    """Result returned by a skill's execute() method."""

    context_data: str = ""  # injected into LLM prompt
    pending_action: dict | None = None  # stored for confirmation flow
    skill_name: str = "chat"
    confidence: float = 1.0


class BaseSkill(ABC):
    """Abstract base class for all skills."""

    name: str = "base"
    description: str = ""
    trigger_intents: list[str] = []
    requires_confirmation: bool = False
    approval_category: ApprovalCategory = ApprovalCategory.NONE

    # Names of env vars required for this skill.  Used by the dispatcher to
    # produce a clear "missing: IONOS_TOKEN" message instead of asking the user.
    config_vars: list[str] = []

    def is_available(self) -> bool:
        """Return True if the skill is properly configured and ready."""
        return True

    @abstractmethod
    async def execute(self, params: dict, original_message: str) -> SkillResult:
        """Execute the skill and return a SkillResult."""
        ...
