"""SmartHomeSkill — control and query Home Assistant devices."""

from __future__ import annotations

import json
import logging

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)


class SmartHomeSkill(BaseSkill):
    name = "smart_home"
    description = (
        "Control and query smart home devices via Home Assistant. Use when Anthony says 'turn on', "
        "'turn off', 'dim', 'set thermostat', 'lock/unlock', 'what's the temperature', 'announce', "
        "or mentions specific devices like lights, thermostat, locks, or sensors. "
        "NOT for: scheduling automations (use n8n), reading sensor history (use data_intelligence), "
        "or Home Assistant config changes."
    )
    trigger_intents = ["smart_home"]
    approval_category = ApprovalCategory.CRITICAL

    def is_available(self) -> bool:
        from app.integrations.smarthome import SmartHomeClient

        return SmartHomeClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.smarthome import SmartHomeClient

        client = SmartHomeClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[Home Assistant not configured — HOME_ASSISTANT_URL or HOME_ASSISTANT_TOKEN missing]",
                skill_name=self.name,
                is_error=True,
                needs_config=True,
            )

        action = params.get("action", "status")
        entity = params.get("entity", "")

        try:
            if action == "turn_on":
                data = await client.turn_on(entity)
            elif action == "turn_off":
                data = await client.turn_off(entity)
            elif action == "status":
                from app.integrations.home_assistant import HomeAssistantClient

                if entity:
                    data = await HomeAssistantClient().get_entity(entity)
                else:
                    data = await client.get_states()
            elif action == "announce":
                message = params.get("message", original_message)
                target = params.get("target")
                data = await client.announce(message, target)
            else:
                domain = entity.split(".")[0] if "." in entity else "homeassistant"
                value = params.get("value")
                svc_data: dict = {"entity_id": entity}
                if value is not None:
                    svc_data["value"] = value
                data = await client.call_service(domain, action, svc_data)

            return SkillResult(context_data=json.dumps(data, indent=2), skill_name=self.name)
        except Exception as exc:
            logger.exception("SmartHomeSkill error action=%s entity=%s: %s", action, entity, exc)
            return SkillResult(
                context_data=f"[SmartHome error executing '{action}' on '{entity}': {exc}]",
                skill_name=self.name,
                is_error=True,
            )
