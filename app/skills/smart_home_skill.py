"""SmartHomeSkill — control and query Home Assistant devices."""

from __future__ import annotations

import json

from app.skills.base import BaseSkill, SkillResult


class SmartHomeSkill(BaseSkill):
    name = "smart_home"
    description = "Control or query smart home devices (lights, thermostat, locks, etc.)"
    trigger_intents = ["smart_home"]

    def is_available(self) -> bool:
        from app.integrations.home_assistant import HomeAssistantClient
        return HomeAssistantClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.home_assistant import HomeAssistantClient
        client = HomeAssistantClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[Home Assistant not configured — HOME_ASSISTANT_URL or HOME_ASSISTANT_TOKEN missing]",
                skill_name=self.name,
            )
        action = params.get("action", "status")
        entity = params.get("entity", "")
        if action == "status":
            data = await client.get_entity(entity) if entity else await client.get_all_states()
        else:
            domain   = entity.split(".")[0] if "." in entity else "homeassistant"
            service  = action
            value    = params.get("value")
            svc_data = {"entity_id": entity}
            if value is not None:
                svc_data["value"] = value
            data = await client.call_service(domain, service, svc_data)
        return SkillResult(context_data=json.dumps(data, indent=2), skill_name=self.name)
