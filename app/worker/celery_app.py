"""
Celery application instance and Beat schedule.

Broker:  Redis DB 1  (app hot memory uses DB 0 — no collision)
Backend: Redis DB 2  (task result storage, 24hr TTL)

Start workers:
  celery -A app.worker.celery_app worker --loglevel=info --concurrency=2 -E -Q evals,celery
  celery -A app.worker.celery_app beat   --loglevel=info
  celery -A app.worker.celery_app flower --port=5555
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

# Use Redis DB 1 (broker) and DB 2 (backend) — app memory uses DB 0
_broker  = f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/1"
_backend = f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/2"

celery_app = Celery(
    "brain",
    broker=_broker,
    backend=_backend,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    # Serialisation
    task_serializer         = "json",
    result_serializer       = "json",
    accept_content          = ["json"],

    # Time
    timezone                = "UTC",
    enable_utc              = True,

    # Visibility — required for Flower and celery-exporter
    worker_send_task_events = True,
    task_send_sent_event    = True,
    task_track_started      = True,

    # Reliability
    task_acks_late          = True,   # ack only after task completes
    result_expires          = 86_400, # keep results 24 hr
    task_reject_on_worker_lost = True,

    # Queues
    task_default_queue      = "celery",
)

celery_app.conf.beat_schedule = {
    # Weekly Sunday 09:00 UTC — agent quality evals + Slack scorecard
    "weekly-agent-evals": {
        "task":    "app.worker.tasks.run_weekly_agent_evals",
        "schedule": crontab(day_of_week="sun", hour=9, minute=0),
        "options": {"queue": "evals"},
    },
    # Nightly 02:00 UTC — integration reliability checks
    "nightly-integration-evals": {
        "task":    "app.worker.tasks.run_nightly_integration_evals",
        "schedule": crontab(hour=2, minute=0),
        "options": {"queue": "evals"},
    },
}
