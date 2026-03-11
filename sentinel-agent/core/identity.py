"""
Agent identity management — persistent agent_id and server fingerprint.
"""

from __future__ import annotations

import json
import os
import platform
import socket

_IDENTITY_PATH = "/etc/sentinel-agent/identity.json"


def load_or_create_identity() -> dict:
    """Load persisted identity or create a new one."""
    if os.path.exists(_IDENTITY_PATH):
        try:
            with open(_IDENTITY_PATH) as f:
                return json.load(f)
        except Exception:
            pass

    identity = get_server_fingerprint()
    try:
        os.makedirs(os.path.dirname(_IDENTITY_PATH), exist_ok=True)
        with open(_IDENTITY_PATH, "w") as f:
            json.dump(identity, f, indent=2)
    except Exception:
        pass
    return identity


def get_server_fingerprint() -> dict:
    """Collect stable server identity information."""
    try:
        ip_address = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip_address = "unknown"

    return {
        "hostname": socket.gethostname(),
        "ip_address": ip_address,
        "os_name": f"{platform.system()} {platform.release()}",
        "python_version": platform.python_version(),
        "machine": platform.machine(),
    }
