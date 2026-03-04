import asyncio
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any
from loguru import logger
import docker
from app.services.error_logger import error_collector

class LogMonitor:
    """Monitors live logs from all Sentinel services via Docker."""
    
    def __init__(self):
        self.client = docker.from_env()
        self.monitored_containers = [
            "ai-brain",
            "ai-celery-worker",
            "ai-celery-worker-workspace",
            "ai-celery-beat",
            "ai-postgres",
            "ai-redis"
        ]
        self.error_patterns = [
            (r"ERROR|FATAL|CRITICAL", "error"),
            (r"ConnectionError|TimeoutError|DatabaseError", "connection"),
            (r"PermissionError|AccessDenied", "permission"),
            (r"OutOfMemory|MemoryError", "memory"),
            (r"FileNotFound|IOError", "io"),
            (r"ValueError|TypeError|KeyError", "type"),
        ]
        self.last_log_time = {}
    
    async def start_monitoring(self) -> None:
        """Start monitoring all containers."""
        logger.info("Starting live log monitoring for all services")
        tasks = [
            self._monitor_container(container_name)
            for container_name in self.monitored_containers
        ]
        await asyncio.gather(*tasks)
    
    async def _monitor_container(self, container_name: str) -> None:
        """Monitor logs from a single container."""
        try:
            container = self.client.containers.get(container_name)
            logger.info(f"Monitoring {container_name}")
            
            while True:
                try:
                    since_time = self.last_log_time.get(container_name, datetime.utcnow() - timedelta(seconds=10))
                    logs = container.logs(stdout=True, stderr=True, follow=False, timestamps=True)
                    
                    for line in logs.decode("utf-8", errors="ignore").split("\n"):
                        if line.strip():
                            await self._process_log_line(container_name, line)
                    
                    self.last_log_time[container_name] = datetime.utcnow()
                    await asyncio.sleep(5)
                    
                except docker.errors.NotFound:
                    logger.warning(f"Container {container_name} not found, retrying...")
                    await asyncio.sleep(10)
                except Exception as e:
                    logger.error(f"Error reading logs from {container_name}: {e}")
                    await asyncio.sleep(5)
        except docker.errors.NotFound:
            logger.warning(f"Container {container_name} does not exist")
        except Exception as e:
            logger.error(f"Failed to monitor {container_name}: {e}")
    
    async def _process_log_line(self, container_name: str, line: str) -> None:
        """Process a single log line and detect errors."""
        try:
            for pattern, error_type in self.error_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    await error_collector.log_error(
                        service=container_name,
                        error_type=error_type,
                        message=line[:200],
                        context={"container": container_name, "full_log": line}
                    )
                    break
        except Exception as e:
            logger.error(f"Error processing log line: {e}")
    
    async def get_service_health(self) -> Dict[str, Dict[str, Any]]:
        """Get health status of all monitored services."""
        health = {}
        for container_name in self.monitored_containers:
            try:
                container = self.client.containers.get(container_name)
                health[container_name] = {
                    "status": container.status,
                    "running": container.status == "running",
                    "uptime": container.attrs["State"]["StartedAt"],
                    "error_count": len(error_collector.get_errors_by_service(container_name))
                }
            except docker.errors.NotFound:
                health[container_name] = {"status": "not_found", "running": False, "error_count": 0}
        return health

log_monitor = LogMonitor()
