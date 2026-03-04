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
    # Primary account (backward-compatible)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"
    google_account_name: str = "personal"   # display label for the primary account

    # Additional Google accounts (optional, up to 4 more)
    google_account_2_name: str = ""
    google_account_2_client_id: str = ""
    google_account_2_client_secret: str = ""
    google_account_2_refresh_token: str = ""
    google_account_2_calendar_id: str = "primary"

    google_account_3_name: str = ""
    google_account_3_client_id: str = ""
    google_account_3_client_secret: str = ""
    google_account_3_refresh_token: str = ""
    google_account_3_calendar_id: str = "primary"

    google_account_4_name: str = ""
    google_account_4_client_id: str = ""
    google_account_4_client_secret: str = ""
    google_account_4_refresh_token: str = ""
    google_account_4_calendar_id: str = "primary"

    google_account_5_name: str = ""
    google_account_5_client_id: str = ""
    google_account_5_client_secret: str = ""
    google_account_5_refresh_token: str = ""
    google_account_5_calendar_id: str = "primary"

    @property
    def google_accounts(self) -> list[dict]:
        """Return all fully-configured Google accounts as a list of dicts."""
        accounts: list[dict] = []
        if self.google_client_id and self.google_client_secret and self.google_refresh_token:
            accounts.append({
                "name":          self.google_account_name or "personal",
                "client_id":     self.google_client_id,
                "client_secret": self.google_client_secret,
                "refresh_token": self.google_refresh_token,
                "calendar_id":   self.google_calendar_id,
            })
        for i in range(2, 6):
            cid     = getattr(self, f"google_account_{i}_client_id", "")
            csecret = getattr(self, f"google_account_{i}_client_secret", "")
            rtoken  = getattr(self, f"google_account_{i}_refresh_token", "")
            if cid and csecret and rtoken:
                accounts.append({
                    "name":          getattr(self, f"google_account_{i}_name", "") or f"account{i}",
                    "client_id":     cid,
                    "client_secret": csecret,
                    "refresh_token": rtoken,
                    "calendar_id":   getattr(self, f"google_account_{i}_calendar_id", "primary"),
                })
        return accounts

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

    # ── Observability / Sentry ───────────────────────────────────
    sentry_dsn:             str = ""
    sentry_auth_token:      str = ""   # API token for reading/managing issues
    sentry_org:             str = ""   # organization slug
    sentry_project:         str = ""   # default project slug (optional)
    sentry_webhook_secret:  str = ""   # HMAC secret for webhook signature verification
    log_level: str = "INFO"
    log_dir:   str = "/var/log/aibrain"

    # ── Evals ──────────────────────────────────────────────────
    slack_eval_channel: str = "brain-evals"

    # ── Cost & rate limiting ────────────────────────────────────
    # Set DAILY_COST_CEILING_USD=0 to disable the ceiling (not recommended).
    daily_cost_ceiling_usd: float = 10.0
    # Comma-separated fractions at which to send a Slack alert (0.5 = 50%).
    budget_alert_thresholds: str = "0.5,0.8,1.0"
    # Per-model daily token caps (input + output combined). 0 = unlimited.
    sonnet_daily_token_budget: int = 0
    haiku_daily_token_budget:  int = 0
    # Slack channel for budget alerts (separate from eval reports).
    slack_alert_channel: str = "brain-alerts"
    # Slack channel for AI action milestones (every confirmed write action).
    slack_milestone_channel: str = "sentinel-milestones"
    # Per-session request rate limits.
    rate_limit_per_minute: int = 20
    rate_limit_per_hour:   int = 200

    # ── Cross-interface memory ─────────────────────────────────
    # All interfaces write to and read from this shared primary session.
    # This is the "hub" that makes Slack, CLI, and REST share context.
    brain_primary_session: str = "brain"

    # ── Repository (Brain self-modification) ───────────────────
    github_brain_repo_url: str = ""
    repo_workspace:        str = "/workspace/repo"
    repo_ssh_key_path:     str = "/root/.ssh/id_ed25519"
    # Local path of the Brain's own code inside the container.
    # With Docker bind-mounts (/root/sentinel:/root/sentinel-workspace) this is
    # the live code directory on the host — writes here persist across rebuilds.
    repo_local_path:       str = "/root/sentinel-workspace"

    # ── Autonomy mode ───────────────────────────────────────────
    # When True: ALL pending actions (deploys, git push, file writes, shell
    # commands, docker restart, etc.) execute immediately without requiring
    # the user to reply "confirm". Set BRAIN_AUTONOMY=true in .env to enable.
    brain_autonomy: bool = False

    # ── IONOS Cloud ─────────────────────────────────────────────
    ionos_token: str = ""           # Bearer token (preferred)
    ionos_username: str = ""        # Basic auth fallback
    ionos_password: str = ""
    ionos_ssh_private_key: str = "" # PEM-encoded SSH key for server access
    ionos_ssh_public_key: str = ""  # Corresponding public key

    # ── Twilio / WhatsApp ───────────────────────────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""  # e.g. whatsapp:+14155238886

    # ── n8n API key (for workflow management) ──────────────────
    n8n_api_key: str = ""

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

    model_config = {"env_file": ".env", "case_sensitive": False, "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
