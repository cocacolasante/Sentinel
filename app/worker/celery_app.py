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
_broker = f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/1"
_backend = f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/2"

celery_app = Celery(
    "brain",
    broker=_broker,
    backend=_backend,
    include=[
        "app.worker.tasks",
        "app.worker.project_tasks",
        "app.worker.sentry_tasks",
        "app.worker.bug_hunter_tasks",
        "app.worker.pr_tasks",
        "app.worker.rmm_tasks",
        "app.worker.reddit_tasks",
        "app.worker.agent_tasks",
        "app.worker.self_heal",
    ],
)

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Time
    timezone="UTC",
    enable_utc=True,
    # Visibility — required for Flower and celery-exporter
    worker_send_task_events=True,
    task_send_sent_event=True,
    task_track_started=True,
    # Reliability
    task_acks_late=True,  # ack only after task completes
    result_expires=86_400,  # keep results 24 hr
    task_reject_on_worker_lost=True,
    # Queues
    task_default_queue="celery",
    # Task routing — workspace tasks go to a dedicated single-concurrency queue
    # to prevent simultaneous writes to /root/sentinel-workspace (merge conflicts).
    # Non-workspace tasks go to tasks_general (concurrency=3).
    task_routes={
        "app.worker.tasks.execute_board_task": {
            # Routing happens dynamically at call-time via .apply_async(queue=...)
            # This entry is a fallback; the skill chooses the queue based on commands.
            "queue": "tasks_general",
        },
        "app.worker.tasks.plan_and_execute_board_task": {
            "queue": "tasks_general",
        },
        "app.worker.tasks.run_shell_and_report_back": {
            "queue": "tasks_general",
        },
    },
)

celery_app.conf.beat_schedule = {
    # Weekly Sunday 09:00 UTC — agent quality evals + Slack scorecard
    "weekly-agent-evals": {
        "task": "app.worker.tasks.run_weekly_agent_evals",
        "schedule": crontab(day_of_week="sun", hour=9, minute=0),
        "options": {"queue": "evals"},
    },
    # Nightly 02:00 UTC — integration reliability checks + Slack health post
    "nightly-integration-evals": {
        "task": "app.worker.tasks.run_nightly_integration_evals",
        "schedule": crontab(hour=2, minute=0),
        "options": {"queue": "evals"},
    },
    # Every 30 min — Brain/Redis/Postgres health check; alerts Slack on failure
    "health-check": {
        "task": "app.worker.tasks.run_health_check",
        "schedule": crontab(minute="*/30"),
        "options": {"queue": "celery"},
    },
    # Every 1 min — scan for pending tasks and dispatch them to workers
    "scan-pending-tasks": {
        "task": "app.worker.tasks.scan_pending_tasks",
        "schedule": crontab(minute="*/1"),
        "options": {"queue": "celery"},
    },
    # Every hour — aggregate error metrics from the in-memory error buffer
    "aggregate-error-metrics": {
        "task": "app.worker.error_tasks.aggregate_error_metrics",
        "schedule": crontab(minute=0),
    },
    # 4x daily at 12am, 6am, 12pm, 6pm UTC — fetch top 10 Sentry errors,
    # create tasks (approval_level=1 = auto-start), investigate + patch + open PR
    "sentry-error-triage": {
        "task": "app.worker.sentry_tasks.ingest_and_triage_top_errors",
        "schedule": crontab(minute=0, hour="0,6,12,18"),
        "options": {"queue": "celery"},
    },
    # Every 6h at :30 — autonomous log scan: cluster errors, LLM root-cause
    # analysis, Slack report, auto-create fix tasks for high-severity bugs
    "autonomous-bug-hunt": {
        "task": "app.worker.bug_hunter_tasks.run_bug_hunt",
        "schedule": crontab(minute=30, hour="*/6"),
        "options": {"queue": "tasks_general"},
        "kwargs": {"hours": 6},
    },
    # Every 15 min — poll for open sentinel/* PRs and trigger review tasks.
    # Acts as a fallback for any PRs that missed the GitHub webhook.
    "poll-sentinel-prs": {
        "task": "app.worker.pr_tasks.poll_open_sentinel_prs",
        "schedule": crontab(minute="*/15"),
        "options": {"queue": "celery"},
    },
    # RMM — every 60s: check device online/offline status, fire Slack alerts on changes
    "rmm-device-poll": {
        "task": "app.worker.rmm_tasks.rmm_poll_device_status",
        "schedule": crontab(minute="*"),
        "options": {"queue": "celery"},
    },
    # RMM — every 5min: full inventory sync (hostname, OS, IP, agent version)
    "rmm-full-sync": {
        "task": "app.worker.rmm_tasks.rmm_full_inventory_sync",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "celery"},
    },
    # RMM — every 2min: threshold breach detection (CPU/mem/disk/offline duration)
    "rmm-incident-check": {
        "task": "app.worker.rmm_tasks.rmm_incident_detection",
        "schedule": crontab(minute="*/2"),
        "options": {"queue": "celery"},
    },
    # Reddit — every hour: check schedules and dispatch due digests
    "reddit-digest-dispatch": {
        "task": "app.worker.reddit_tasks.dispatch_reddit_digests",
        "schedule": crontab(minute=0),
        "options": {"queue": "celery"},
    },
    # Mesh Agent — every 2min: detect offline agents and alert
    "agent-heartbeat-monitor": {
        "task": "app.worker.agent_tasks.check_agent_heartbeats",
        "schedule": crontab(minute="*/2"),
        "options": {"queue": "celery"},
    },
    # Mesh Agent — every 1min: process inbound stream messages
    "agent-stream-consumer": {
        "task": "app.worker.agent_tasks.process_agent_stream",
        "schedule": crontab(minute="*/1"),
        "options": {"queue": "celery"},
    },
    # Mesh Agent — daily 03:00 UTC: purge old heartbeat rows
    "agent-heartbeat-purge": {
        "task": "app.worker.agent_tasks.purge_old_heartbeats",
        "schedule": crontab(hour=3, minute=0),
        "options": {"queue": "celery"},
    },
}
