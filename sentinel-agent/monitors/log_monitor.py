"""
LogMonitor — async tail of the application log file, detects errors.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque

import aiofiles

logger = logging.getLogger(__name__)

_ERROR_RE = re.compile(r"(ERROR|CRITICAL|Traceback|Exception|FATAL)", re.I)
_FILE_RE = re.compile(r'File "([^"]+)"')
_CONTEXT_LINES = 50
_DEDUP_WINDOW = 60  # seconds


class LogMonitor:
    def __init__(self, settings):
        self._settings = settings
        self._recent_errors: dict[str, float] = {}  # signature → timestamp

    async def run(self, relay) -> None:
        """Tail the log file and dispatch LOG_ERROR events on matches."""
        log_path = self._settings.app_log_path
        if not log_path:
            logger.info("No app_log_path configured — log monitor disabled")
            return

        logger.info("Log monitor started: %s", log_path)
        context_buf: deque[str] = deque(maxlen=_CONTEXT_LINES)
        post_lines: list[str] = []
        collecting = False
        collect_count = 0

        # Wait for file to exist
        while not os.path.exists(log_path):
            await asyncio.sleep(5)

        async with aiofiles.open(log_path, mode="r") as f:
            # Seek to end
            await f.seek(0, 2)
            while True:
                line = await f.readline()
                if not line:
                    await asyncio.sleep(0.5)
                    continue

                line = line.rstrip()
                context_buf.append(line)

                if _ERROR_RE.search(line):
                    collecting = True
                    collect_count = 0
                    post_lines = [line]
                elif collecting:
                    post_lines.append(line)
                    collect_count += 1
                    if collect_count >= _CONTEXT_LINES:
                        await self._emit_error(relay, post_lines, list(context_buf))
                        collecting = False
                        post_lines = []

    async def _emit_error(self, relay, post_lines: list[str], context_lines: list[str]) -> None:
        """Build and send a LOG_ERROR event, with deduplication."""
        stack_trace = "\n".join(post_lines[:100])
        sig = hash(stack_trace[:200])

        # Dedup within window
        now = time.time()
        if sig in self._recent_errors:
            if now - self._recent_errors[sig] < _DEDUP_WINDOW:
                return
        self._recent_errors[sig] = now

        file_paths = _extract_file_paths(post_lines)
        await relay.send("LOG_ERROR", {
            "stack_trace": stack_trace,
            "context_lines": context_lines[-20:],
            "file_paths": file_paths,
            "commit_sha": None,
        })
        logger.info("LOG_ERROR emitted | files=%s", file_paths)


def _extract_file_paths(lines: list[str]) -> list[str]:
    """Parse Python traceback File lines."""
    paths = []
    for line in lines:
        for match in _FILE_RE.finditer(line):
            path = match.group(1)
            if path not in paths:
                paths.append(path)
    return paths
