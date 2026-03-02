"""
BaseSkill — abstract base class for all skills in the PAI skill system.

Each skill handles one or more intents from the IntentClassifier. Skills are
self-describing (name, description, trigger_intents) and can report whether
they are currently available (e.g., credentials configured).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SkillResult:
    """Result returned by a skill's execute() method."""
    context_data:    str        = ""          # injected into LLM prompt
    pending_action:  dict | None = None       # stored for confirmation flow
    skill_name:      str        = "chat"
    confidence:      float      = 1.0


class BaseSkill(ABC):
    """Abstract base class for all skills."""

    name: str = "base"
    description: str = ""
    trigger_intents: list[str] = []
    requires_confirmation: bool = False

    def is_available(self) -> bool:
        """Return True if the skill is properly configured and ready."""
        return True

    @abstractmethod
    async def execute(self, params: dict, original_message: str) -> SkillResult:
        """Execute the skill and return a SkillResult."""
        ...
