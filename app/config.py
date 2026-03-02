from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Brain ─────────────────────────────────────────────────
    secret_key: str = "change-me-in-production"
    environment: str = "development"

    # ── PostgreSQL ─────────────────────────────────────────────
    postgres_user: str = "brain"
    postgres_password: str = "changeme"
    postgres_db: str = "aibrain"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # ── Redis ──────────────────────────────────────────────────
    redis_password: str = "changeme"
    redis_host: str = "redis"
    redis_port: int = 6379

    # ── Qdrant ─────────────────────────────────────────────────
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333

    # ── LLM APIs ───────────────────────────────────────────────
    anthropic_api_key: str = ""
    openai_api_key: str = ""    # Phase 2+
    gemini_api_key: str = ""    # Phase 2+

    # ── Slack ──────────────────────────────────────────────────
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_app_token: str = ""   # xapp- token for Socket Mode

    # ── n8n ────────────────────────────────────────────────────
    n8n_host: str = "localhost"
    n8n_user: str = "admin"
    n8n_password: str = "changeme"
    n8n_webhook_url: str = "http://n8n:5678"

    # ── Domain ─────────────────────────────────────────────────
    domain: str = "localhost"

    # ── Google OAuth (Gmail + Calendar) ────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"

    # ── GitHub ─────────────────────────────────────────────────
    github_token: str = ""
    github_username: str = ""
    github_default_repo: str = ""   # e.g. "anthonycolasante/my-repo"

    # ── Home Assistant ─────────────────────────────────────────
    home_assistant_url: str = ""    # e.g. "http://192.168.1.100:8123"
    home_assistant_token: str = ""
    home_assistant_verify_ssl: bool = True

    # ── Timezone ───────────────────────────────────────────────
    timezone: str = "America/Chicago"

    # ── TELOS ──────────────────────────────────────────────────
    telos_dir: str = "/home/ubuntu/ai-brain/telos"
    telos_cache_ttl_seconds: int = 300

    # ── Observability ───────────────────────────────────────────
    sentry_dsn: str = ""
    log_level: str = "INFO"
    log_dir: str = "/var/log/aibrain"

    # ── Evals ──────────────────────────────────────────────────
    slack_eval_channel: str = "brain-evals"

    # ── Memory ─────────────────────────────────────────────────
    openai_embedding_model: str = "text-embedding-3-small"
    qdrant_collection: str = "brain_memories"
    qdrant_vector_size: int = 1536
    memory_flush_interval_turns: int = 10

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    model_config = {"env_file": ".env", "case_sensitive": False}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
