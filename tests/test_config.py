"""Unit tests for app.config.Settings."""
import pytest
from app.config import Settings


def test_default_environment():
    s = Settings()
    assert s.environment in ("development", "test")


def test_default_ports():
    s = Settings()
    assert s.postgres_port == 5432
    assert s.redis_port == 6379
    assert s.qdrant_port == 6333


def test_postgres_dsn():
    s = Settings(
        postgres_user="alice",
        postgres_password="secret",
        postgres_host="db.example.com",
        postgres_port=5433,
        postgres_db="mydb",
    )
    assert s.postgres_dsn == "postgresql://alice:secret@db.example.com:5433/mydb"


def test_google_accounts_empty_when_no_creds():
    s = Settings(
        google_client_id="",
        google_client_secret="",
        google_refresh_token="",
    )
    assert s.google_accounts == []


def test_google_accounts_primary_only():
    s = Settings(
        google_client_id="cid",
        google_client_secret="csecret",
        google_refresh_token="rtoken",
        google_account_name="personal",
    )
    accounts = s.google_accounts
    assert len(accounts) == 1
    assert accounts[0]["name"] == "personal"
    assert accounts[0]["client_id"] == "cid"
    assert accounts[0]["calendar_id"] == "primary"


def test_google_accounts_multiple():
    s = Settings(
        google_client_id="cid1",
        google_client_secret="csecret1",
        google_refresh_token="rtoken1",
        google_account_2_client_id="cid2",
        google_account_2_client_secret="csecret2",
        google_account_2_refresh_token="rtoken2",
        google_account_2_name="work",
    )
    accounts = s.google_accounts
    assert len(accounts) == 2
    assert accounts[0]["name"] == "personal"
    assert accounts[1]["name"] == "work"


def test_google_accounts_skips_incomplete():
    """An account with only client_id but no secret/token is not included."""
    s = Settings(
        # Explicitly clear primary credentials so .env values don't bleed in
        google_client_id="",
        google_client_secret="",
        google_refresh_token="",
        # Secondary account is incomplete (no secret/token)
        google_account_2_client_id="cid2",
        google_account_2_client_secret="",
        google_account_2_refresh_token="",
    )
    assert s.google_accounts == []


def test_brain_autonomy_default_false():
    s = Settings()
    assert s.brain_autonomy is False


def test_daily_cost_ceiling_default():
    s = Settings()
    assert s.daily_cost_ceiling_usd == 10.0


def test_rate_limits_defaults():
    s = Settings()
    assert s.rate_limit_per_minute == 20
    assert s.rate_limit_per_hour == 200


def test_budget_alert_thresholds_default():
    s = Settings()
    assert "0.5" in s.budget_alert_thresholds
    assert "1.0" in s.budget_alert_thresholds
