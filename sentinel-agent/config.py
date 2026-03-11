"""
Sentinel Agent configuration — reads from /etc/sentinel-agent/env or environment variables.
"""

from pydantic_settings import BaseSettings


class AgentSettings(BaseSettings):
    # Brain connection
    brain_url: str = "wss://sentinelai.cloud/ws/agent"
    agent_id: str = ""
    agent_token: str = ""

    # Application under management
    app_name: str = "app"
    app_dir: str = "/opt/app"
    app_process_name: str = "python"
    app_health_url: str = ""
    app_log_path: str = ""
    app_restart_cmd: str = ""
    app_test_cmd: str = ""

    # Identity
    sentinel_env: str = "staging"

    # Heartbeat / monitoring
    heartbeat_interval: int = 30
    resource_cpu_threshold: float = 90.0
    resource_mem_threshold: float = 85.0
    resource_disk_threshold: float = 90.0

    model_config = {
        "env_file": "/etc/sentinel-agent/env",
        "case_sensitive": False,
        "extra": "ignore",
    }


settings = AgentSettings()
