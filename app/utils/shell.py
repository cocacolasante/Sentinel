"""
ShellExecutor — async subprocess wrapper for test runners and sandboxed commands.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class ShellResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> ShellResult:
    """Run a subprocess asynchronously and return a ShellResult."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return ShellResult(returncode=-1, stdout="", stderr="timeout")
    return ShellResult(
        returncode=proc.returncode,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )
