#!/usr/bin/env python3
"""
Google OAuth Setup Script — run ONCE to get your refresh token.

What this does:
  1. Opens a browser for you to sign in to Google
  2. Requests offline access to Gmail + Calendar
  3. Saves the refresh token to .env

Usage:
  cd ~/ai-brain
  python3 scripts/google_auth.py

Prerequisites:
  1. Go to https://console.cloud.google.com
  2. Create a project (or use an existing one)
  3. Enable: Gmail API + Google Calendar API
  4. Go to APIs & Services > Credentials > Create Credentials > OAuth client ID
  5. Application type: Desktop app
  6. Download the JSON file and save it as google_credentials.json in ~/ai-brain/
  7. Run this script

After running, add the printed values to your .env file.
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
]

CREDS_FILE   = PROJECT_ROOT / "google_credentials.json"
TOKEN_FILE   = PROJECT_ROOT / "google_token.json"
ENV_FILE     = PROJECT_ROOT / ".env"


def main():
    if not CREDS_FILE.exists():
        print(f"ERROR: {CREDS_FILE} not found.")
        print("Download your OAuth credentials JSON from Google Cloud Console")
        print("and save it as google_credentials.json in the project root.")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("Missing dependencies. Run: pip install google-auth-oauthlib google-auth-httplib2")
        sys.exit(1)

    creds = None

    # Load existing token if available
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    # Refresh or run new flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())

    print("\n" + "=" * 60)
    print("SUCCESS — Google OAuth authorised!")
    print("=" * 60)
    print(f"\nClient ID:      {creds.client_id}")
    print(f"Client Secret:  {creds.client_secret}")
    print(f"Refresh Token:  {creds.refresh_token}")
    print("\nAdd these to your .env file:")
    print(f"  GOOGLE_CLIENT_ID={creds.client_id}")
    print(f"  GOOGLE_CLIENT_SECRET={creds.client_secret}")
    print(f"  GOOGLE_REFRESH_TOKEN={creds.refresh_token}")

    # Optionally write directly to .env
    if ENV_FILE.exists():
        answer = input("\nAuto-update .env with these values? [y/N] ").strip().lower()
        if answer == "y":
            env_content = ENV_FILE.read_text()

            def _replace_or_append(content: str, key: str, value: str) -> str:
                lines = content.splitlines()
                replaced = False
                new_lines = []
                for line in lines:
                    if line.startswith(f"{key}="):
                        new_lines.append(f"{key}={value}")
                        replaced = True
                    else:
                        new_lines.append(line)
                if not replaced:
                    new_lines.append(f"{key}={value}")
                return "\n".join(new_lines) + "\n"

            env_content = _replace_or_append(env_content, "GOOGLE_CLIENT_ID",     creds.client_id)
            env_content = _replace_or_append(env_content, "GOOGLE_CLIENT_SECRET", creds.client_secret)
            env_content = _replace_or_append(env_content, "GOOGLE_REFRESH_TOKEN", creds.refresh_token)
            ENV_FILE.write_text(env_content)
            print(f".env updated at {ENV_FILE}")

    print("\nYou can now restart the Brain and Gmail/Calendar will be active.")
    print(f"(Token also saved to {TOKEN_FILE} — delete it to re-authorise)")


if __name__ == "__main__":
    main()
