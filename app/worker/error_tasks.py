from celery import shared_task
from loguru import logger
from app.services.error_logger import error_collector
from app.db.postgres import get_db
import json
from datetime import datetime, timedelta

@shared_task(bind=True, max_retries=3)
def process_error_and_create_task(self, error_record):
    """Process error record and create investigation task."""
    try:
        db = get_db()
        
        task_title = f"[AUTO] Fix {error_record["service"]} - {error_record["error_type"]}"
        task_description = f"""Service: {error_record["service"]}
Error Type: {error_record["error_type"]}
Message: {error_record["message"]}
Time: {error_record["timestamp"]}
Context: {json.dumps(error_record["context"], indent=2)}
Stacktrace:
{error_record.get("stacktrace") or "N/A"}
"""
        
        insert_query = """
        INSERT INTO tasks (title, description, status, priority, tags, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """
        
        cursor = db.cursor()
        cursor.execute(insert_query, (
            task_title,
            task_description,
            "open",
            "high",
            json.dumps(["auto-generated", "error-handling", error_record["service"]]),
            datetime.utcnow(),
            datetime.utcnow()
        ))
        task_id = cursor.fetchone()[0]
        db.commit()
        cursor.close()
        
        logger.info(f"Created task {task_id} for error in {error_record["service"]}")
        return {"task_id": task_id, "status": "created"}
        
    except Exception as exc:
        logger.error(f"Error processing error record: {exc}")
        raise self.retry(exc=exc, countdown=60)

@shared_task
def collect_live_logs():
    """Periodic task to collect and process live logs from all services."""
    try:
        import asyncio
        from app.services.log_monitor import log_monitor
        
        loop = asyncio.get_event_loop()
        health = loop.run_until_complete(log_monitor.get_service_health())
        
        logger.info(f"Log collection task executed. Health: {health}")
        return health
    except Exception as e:
        logger.error(f"Error in collect_live_logs: {e}")
        raise

@shared_task
def aggregate_error_metrics():
    """Hourly task to aggregate error metrics and create summary reports."""
    try:
        summary = error_collector.error_buffer
        
        errors_by_service = {}
        for error in summary:
            service = error["service"]
            if service not in errors_by_service:
                errors_by_service[service] = []
            errors_by_service[service].append(error)
        
        logger.info(f"Error metrics aggregated: {len(errors_by_service)} services with errors")
        return {"total_errors": len(summary), "services_affected": len(errors_by_service)}
    except Exception as e:
        logger.error(f"Error in aggregate_error_metrics: {e}")
        raise
