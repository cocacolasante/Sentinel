import json
from celery import shared_task
from loguru import logger
from app.services.error_logger import error_collector


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

        logger.info(
            f"Error metrics aggregated: {len(errors_by_service)} services with errors"
        )
        return {"total_errors": len(summary), "services_affected": len(errors_by_service)}
    except Exception as e:
        logger.error(f"Error in aggregate_error_metrics: {e}")
        raise
