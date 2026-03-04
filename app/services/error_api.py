from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any
from app.services.error_logger import error_collector
from app.services.log_monitor import log_monitor

router = APIRouter(prefix="/errors", tags=["errors"])

@router.get("/recent")
async def get_recent_errors(limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent errors from all services."""
    return error_collector.get_recent_errors(limit)

@router.get("/by-service/{service}")
async def get_errors_by_service(service: str) -> List[Dict[str, Any]]:
    """Get errors for a specific service."""
    return error_collector.get_errors_by_service(service)

@router.get("/health")
async def get_service_health() -> Dict[str, Dict[str, Any]]:
    """Get health status of all monitored services."""
    return await log_monitor.get_service_health()

@router.get("/summary")
async def get_error_summary() -> Dict[str, Any]:
    """Get summary of errors by service and type."""
    errors = error_collector.error_buffer
    summary = {}
    
    for error in errors:
        service = error["service"]
        error_type = error["error_type"]
        
        if service not in summary:
            summary[service] = {}
        if error_type not in summary[service]:
            summary[service][error_type] = 0
        
        summary[service][error_type] += 1
    
    return {"total_errors": len(errors), "by_service": summary}

@router.delete("/clear")
async def clear_error_buffer() -> Dict[str, str]:
    """Clear the error buffer."""
    error_collector.error_buffer.clear()
    return {"status": "cleared"}
