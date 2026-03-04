import json
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional
from loguru import logger

class ErrorCollector:
    """Collects errors from all services and creates auto-fix tasks."""
    
    def __init__(self):
        self.error_buffer = []
        self.max_buffer = 100
        self.debounce_window = 300
        self.last_error_hash = {}
    
    async def log_error(self, 
                       service: str,
                       error_type: str,
                       message: str,
                       stacktrace: Optional[str] = None,
                       context: Optional[Dict[str, Any]] = None) -> None:
        """Log error and create remediation task if needed."""
        error_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "service": service,
            "error_type": error_type,
            "message": message,
            "stacktrace": stacktrace,
            "context": context or {}
        }
        
        logger.error(f"Service Error [{service}] {error_type}: {message}", extra=error_record)
        
        self.error_buffer.append(error_record)
        if len(self.error_buffer) > self.max_buffer:
            self.error_buffer.pop(0)
        
        error_hash = f"{service}:{error_type}:{message[:50]}"
        if self._should_create_task(error_hash):
            await self._create_remediation_task(error_record)
    
    def _should_create_task(self, error_hash: str) -> bool:
        """Debounce: do not create duplicate tasks within window."""
        now = datetime.utcnow().timestamp()
        last_seen = self.last_error_hash.get(error_hash, 0)
        
        if now - last_seen > self.debounce_window:
            self.last_error_hash[error_hash] = now
            return True
        return False
    
    async def _create_remediation_task(self, error_record: Dict[str, Any]) -> None:
        """Create auto-investigation task for error."""
        try:
            task_title = f"[AUTO] Fix {error_record["service"]} - {error_record["error_type"]}"
            task_description = f"Service: {error_record["service"]}
Error Type: {error_record["error_type"]}
Message: {error_record["message"]}
Time: {error_record["timestamp"]}
Context: {json.dumps(error_record["context"], indent=2)}
Stacktrace:
{error_record["stacktrace"] or "N/A"}"
            
            from app.worker.tasks import create_investigation_task
            await create_investigation_task(
                title=task_title,
                description=task_description,
                priority="high",
                tags=["auto-generated", "error-handling", error_record["service"]]
            )
            logger.info(f"Created remediation task for {error_record["service"]}") 
        except Exception as e:
            logger.error(f"Failed to create remediation task: {e}")
    
    def get_recent_errors(self, limit: int = 50) -> list:
        """Get recent errors from buffer."""
        return self.error_buffer[-limit:]
    
    def get_errors_by_service(self, service: str) -> list:
        """Get errors filtered by service."""
        return [e for e in self.error_buffer if e["service"] == service]
    
    def export_errors_json(self, filepath: str) -> None:
        """Export error buffer to JSON file."""
        with open(filepath, "w") as f:
            json.dump(self.error_buffer, f, indent=2, default=str)

error_collector = ErrorCollector()
