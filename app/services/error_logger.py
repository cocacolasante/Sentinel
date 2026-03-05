import json
from datetime import datetime
from typing import Dict, Any, Optional
from loguru import logger


class ErrorCollector:
    """Collects errors from all services and creates approval-gated remediation tasks."""

    def __init__(self):
        self.error_buffer: list = []
        self.max_buffer = 100
        self.debounce_window = 3600  # 1 hour — one task per service+error_type per hour
        self.last_error_hash: Dict[str, float] = {}

    async def log_error(
        self,
        service: str,
        error_type: str,
        message: str,
        stacktrace: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Buffer the error and create a remediation task if the debounce window
        has elapsed. Returns True if a task was created, False otherwise.
        """
        # Validate service name — reject 'unknown' or empty services
        if not service or service.lower() == "unknown":
            logger.warning(
                "Rejecting error log with invalid service name: {}",
                service or "<empty>",
            )
            return False

        error_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "service": service,
            "error_type": error_type,
            "message": message,
            "stacktrace": stacktrace,
            "context": context or {},
        }

        self.error_buffer.append(error_record)
        if len(self.error_buffer) > self.max_buffer:
            self.error_buffer.pop(0)

        # Bucket key: service + error_type only (message content excluded from dedup)
        error_hash = f"{service}:{error_type}"
        if self._should_create_task(error_hash):
            await self._create_remediation_task(error_record)
            return True
        return False

    def _should_create_task(self, error_hash: str) -> bool:
        """Debounce: skip if the same bucket fired within the window."""
        now = datetime.utcnow().timestamp()
        if now - self.last_error_hash.get(error_hash, 0) > self.debounce_window:
            self.last_error_hash[error_hash] = now
            return True
        return False

    async def _create_remediation_task(self, error_record: Dict[str, Any]) -> None:
        """Insert a pending, approval-required task into the task board."""
        try:
            task_title = f"[AUTO] Fix {error_record['service']} - {error_record['error_type']}"
            task_description = (
                f"Service: {error_record['service']}\n"
                f"Error Type: {error_record['error_type']}\n"
                f"Message: {error_record['message']}\n"
                f"Time: {error_record['timestamp']}\n"
                f"Context: {json.dumps(error_record['context'], indent=2)}\n"
                f"Stacktrace:\n{error_record['stacktrace'] or 'N/A'}"
            )
            from app.db import postgres

            tags = json.dumps(["auto-generated", "error-handling", error_record["service"]])
            postgres.execute(
                """
                INSERT INTO tasks (title, description, status, priority, priority_num,
                                   approval_level, source, tags)
                VALUES (%s, %s, 'pending', 'high', 4, 2, 'error-collector', %s::jsonb)
                """,
                (task_title, task_description, tags),
            )
            logger.info(
                "Queued approval-required task for {} ({})",
                error_record["service"],
                error_record["error_type"],
            )
        except Exception as e:
            logger.error("Failed to create remediation task: {}", e)

    def get_recent_errors(self, limit: int = 50) -> list:
        return self.error_buffer[-limit:]

    def get_errors_by_service(self, service: str) -> list:
        return [e for e in self.error_buffer if e["service"] == service]

    def export_errors_json(self, filepath: str) -> None:
        with open(filepath, "w") as f:
            json.dump(self.error_buffer, f, indent=2, default=str)


error_collector = ErrorCollector()
