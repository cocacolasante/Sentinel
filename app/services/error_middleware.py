import traceback
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from loguru import logger
from app.services.error_logger import error_collector


class ErrorCollectionMiddleware(BaseHTTPMiddleware):
    """Middleware to capture and log all errors from API requests."""

    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)

            # Log 5xx errors
            if response.status_code >= 500:
                await error_collector.log_error(
                    service="fastapi-brain",
                    error_type="http_error",
                    message=(
                        f"HTTP {response.status_code} on "
                        f"{request.method} {request.url.path}"
                    ),
                    context={
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": response.status_code,
                        "client": str(request.client),
                    },
                )

            return response

        except Exception as e:
            tb = traceback.format_exc()
            await error_collector.log_error(
                service="fastapi-brain",
                error_type="unhandled_exception",
                message=str(e),
                stacktrace=tb,
                context={
                    "method": request.method,
                    "path": request.url.path,
                    "client": str(request.client),
                },
            )
            logger.error("Unhandled exception: {}\n{}", e, tb)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "error_id": id(e)},
            )
