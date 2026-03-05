"""
Loguru configuration for the AI Brain.

Two sinks:
  stdout    — human-readable, colorized (Docker logs / dev)
  JSON file — structured JSON, rotated daily, 7-day retention (prod audit trail)

After calling configure(), use `from loguru import logger` everywhere.
stdlib `logging` is intercepted so existing log calls also flow through Loguru.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from loguru import logger


class _InterceptHandler(logging.Handler):
    """Route stdlib logging records into Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno  # type: ignore[assignment]

        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure(log_dir: str = "/var/log/aibrain", level: str = "INFO") -> None:
    """
    Set up Loguru sinks. Call once during app startup.

    Args:
        log_dir: Directory for JSON log files (created if missing).
        level:   Minimum log level (INFO in prod, DEBUG in dev).
    """
    # Remove default Loguru sink
    logger.remove()

    # ── Sink 1: stdout — human-readable ───────────────────────────────────────
    logger.add(
        sys.stdout,
        level=level,
        colorize=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
            "{message}"
        ),
        enqueue=True,
    )

    # ── Sink 2: JSON file — structured, rotated ───────────────────────────────
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger.add(
        log_path / "brain.json",
        level=level,
        rotation="00:00",  # rotate at midnight
        retention="7 days",
        compression="gz",
        serialize=True,  # writes each line as a JSON object
        enqueue=True,  # non-blocking writes
    )

    # ── Intercept stdlib logging ───────────────────────────────────────────────
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).handlers = [_InterceptHandler()]
        logging.getLogger(name).propagate = False

    logger.info("Loguru configured | level={} | log_dir={}", level, log_dir)
