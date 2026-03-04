#!/usr/bin/env python3
"""
Google OAuth Setup Script — two-phase, no SSH tunnel required.

Phase 1 (auto):  Prints an auth URL and waits for you to drop the code.
Phase 2 (auto):  You visit the URL, approve, copy the redirect URL from
                 the browser bar, then paste it here via Claude Code:

    Write "/tmp/google_oauth_code.txt" with the full redirect URL.

The script detects the file, exchanges the code, updates .env, done.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CREDS_FILE   = PROJECT_ROOT / "google_credentials.json"
TOKEN_FILE   = PROJECT_ROOT / "google_token.json"
ENV_FILE     = PROJECT_ROOT / ".env"
CODE_FILE    = Path("/tmp/google_oauth_code.txt")

SCOPES = " ".join([
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
])

REDIRECT_URI  = "http://localhost"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def load_client_secrets():
    data = json.loads(CREDS_FILE.read_text())
    kind = list(data.keys())[0]
    info = data[kind]
    return info["client_id"], info["client_secret"]


def build_auth_url(client_id: str) -> str:
    params = {
        "response_type": "code",
        "client_id":     client_id,
        "redirect_uri":  REDIRECT_URI,
        "scope":         SCOPES,
        "access_type":   "offline",
        "prompt":        "consent",
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    data = urllib.parse.urlencode({
        "code":          code,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def update_env(client_id: str, client_secret: str, refresh_token: str):
    def _set(content, key, value):
        lines = content.splitlines()
        new_lines, replaced = [], False
        for line in lines:
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={value}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"{key}={value}")
        return "\n".join(new_lines) + "\n"

    content = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    content = _set(content, "GOOGLE_CLIENT_ID",     client_id)
    content = _set(content, "GOOGLE_CLIENT_SECRET", client_secret)
    content = _set(content, "GOOGLE_REFRESH_TOKEN", refresh_token)
    ENV_FILE.write_text(content)


def main():
    if not CREDS_FILE.exists():
        print(f"ERROR: {CREDS_FILE} not found.")
        sys.exit(1)

    client_id, client_secret = load_client_secrets()

    # ── Phase 1: generate URL ─────────────────────────────────────────────
    CODE_FILE.unlink(missing_ok=True)
    auth_url = build_auth_url(client_id)

    print("\n" + "=" * 70)
    print("STEP 1 — Open this URL in any browser:")
    print("=" * 70)
    print(f"\n{auth_url}\n")
    print("=" * 70)
    print("STEP 2 — After approving, Google redirects to:")
    print("  http://localhost/?code=...  (page will fail to load — that's fine)")
    print()
    print("Copy the FULL URL from your browser address bar, then run this")
    print("command in Claude Code (replace <URL> with what you copied):")
    print()
    print("  Write the URL into /tmp/google_oauth_code.txt")
    print()
    print("Waiting up to 10 minutes for /tmp/google_oauth_code.txt ...")
    print("=" * 70 + "\n")
    sys.stdout.flush()

    # ── Phase 2: wait for code file ───────────────────────────────────────
    for _ in range(600):
        if CODE_FILE.exists():
            break
        time.sleep(1)
    else:
        print("Timed out. Re-run the script to try again.")
        sys.exit(1)

    raw = CODE_FILE.read_text().strip()
    CODE_FILE.unlink(missing_ok=True)

    if raw.startswith("http"):
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        code = parsed.get("code", [raw])[0]
    else:
        code = raw

    print(f"Got code. Exchanging for tokens...")

    # ── Phase 3: exchange & save ──────────────────────────────────────────
    tokens = exchange_code(client_id, client_secret, code)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print(f"ERROR: no refresh_token in response: {tokens}")
        sys.exit(1)

    # Save token file (for future refreshes)
    token_data = {
        "token":         tokens.get("access_token"),
        "refresh_token": refresh_token,
        "token_uri":     TOKEN_ENDPOINT,
        "client_id":     client_id,
        "client_secret": client_secret,
        "scopes":        SCOPES.split(),
    }
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))

    # Auto-update .env
    update_env(client_id, client_secret, refresh_token)

    print("\n" + "=" * 70)
    print("SUCCESS — .env updated with new refresh token (Gmail + Calendar).")
    print("=" * 70)
    print(f"\nRefresh token: {refresh_token[:20]}...")
    print(f"Token file:    {TOKEN_FILE}")
    print(f".env file:     {ENV_FILE}")
    print("\nNow restart the brain:")
    print("  docker restart ai-brain")
    print()


if __name__ == "__main__":
    main()
