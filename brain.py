#!/usr/bin/env python3
"""
Brain CLI — terminal interface to the AI Brain.

Usage:
  python brain.py                   Interactive REPL (joins shared primary session)
  python brain.py --session NAME    Resume or create a named private session
  python brain.py chat "message"    One-shot chat (uses shared session)
  python brain.py health            System health
  python brain.py costs             Today's LLM spend
  python brain.py pending           List pending write approvals
  python brain.py approve <id>      Approve a pending task
  python brain.py cancel  <id>      Cancel a pending task
  python brain.py level [1|2|3]     Get / set approval level
  python brain.py tasks             Show Celery queue
  python brain.py sessions          List saved sessions
  python brain.py mytasks           List task board (created by the AI)
  python brain.py context           Show server / repo / env context

Environment:
  BRAIN_URL      Base URL of the Brain API (default: http://localhost:8000)
  BRAIN_SESSION  Session ID to use instead of the shared "brain" session
  NO_COLOR       Set to any value to disable ANSI colours

Cross-interface memory:
  By default the CLI joins the same "brain" primary session used by Slack and
  the REST API, so all interfaces share warm memory context.  Use --session to
  start a private isolated session instead.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import threading
import uuid
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


# ── .env auto-loader ──────────────────────────────────────────────────────────

_ENV_FILE: Path | None = None


def _load_env_file() -> Path | None:
    """
    Look for a .env file in: script dir → cwd → parent dirs (up to 3 levels).
    Parse KEY=VALUE lines and inject into os.environ (existing vars win).
    Returns the path of the loaded file, or None.
    """
    candidates: list[Path] = []
    script_dir = Path(__file__).resolve().parent
    candidates.append(script_dir / ".env")
    cwd = Path.cwd()
    if cwd != script_dir:
        candidates.append(cwd / ".env")
    for parent in cwd.parents[:3]:
        candidates.append(parent / ".env")

    for path in candidates:
        if path.is_file():
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, _, val = line.partition("=")
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        if key and key not in os.environ:
                            os.environ[key] = val
                return path
            except Exception:
                pass
    return None


_ENV_FILE = _load_env_file()

# ── Config ────────────────────────────────────────────────────────────────────
BRAIN_URL       = os.environ.get("BRAIN_URL", "http://localhost:8000").rstrip("/")
# Shared primary session — all interfaces (Slack, CLI, REST) share this session.
# Override with BRAIN_SESSION env var or --session flag for a private session.
PRIMARY_SESSION = os.environ.get("BRAIN_SESSION", "brain")
HISTORY_FILE    = os.path.expanduser("~/.brain_history")
SESSIONS_FILE   = os.path.expanduser("~/.brain_sessions.json")
MAX_SESSIONS    = 3   # number of sessions shown in picker + stored
_REPO_ROOT      = Path(_ENV_FILE).parent if _ENV_FILE else None

# ── ANSI colours ──────────────────────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    BLUE   = "\033[34m"
    CYAN   = "\033[36m"
    WHITE  = "\033[37m"

if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    for _a in list(vars(C)):
        if not _a.startswith("_"):
            setattr(C, _a, "")

# ── Spinner ───────────────────────────────────────────────────────────────────

_SPINNER_ENABLED = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

# ── Deferred-response detection ───────────────────────────────────────────────
# Patterns that indicate the brain promised to do something but didn't actually
# return results yet.  cmd_chat will auto-follow-up when these are detected.

_DEFERRED_RE = re.compile(
    r"running (this|both|all|these|it|now|the command)"
    r"|one moment"
    r"|stand by"
    r"|will report back"
    r"|give me a (moment|second|sec)"
    r"|working on it"
    r"|let me (run|do|execute|check|create|try)"
    r"|i('ll| will) (run|do|execute|check|create|fix|apply|make)",
    re.IGNORECASE,
)

# The brain showed shell commands as literal text instead of executing them
_BASH_BLOCK_RE = re.compile(r"```\s*(bash|sh|shell|zsh)\s*\n", re.IGNORECASE)

_MAX_FOLLOWUPS   = 3
_FOLLOWUP_MSG    = "execute all the commands above and show me the exact output"

class _Spinner:
    """Animated terminal spinner with phase labels, runs in a background thread."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _PHASES = [
        ( 0.0, "Thinking"),
        ( 2.5, "Routing intent"),
        ( 5.0, "Running skill"),
        (10.0, "Composing response"),
        (22.0, "Still working"),
    ]
    _WIDTH = 55   # width of the spinner line (used to blank it on exit)

    def __init__(self) -> None:
        self._stop    = threading.Event()
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._t0: float = 0.0

    def _phase_label(self, elapsed: float) -> str:
        label = self._PHASES[0][1]
        for threshold, name in self._PHASES:
            if elapsed >= threshold:
                label = name
        return label

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            elapsed = time.time() - self._t0
            frame   = self._FRAMES[i % len(self._FRAMES)]
            label   = self._phase_label(elapsed)
            line    = (
                f"\r{C.CYAN}{frame}{C.RESET} "
                f"{C.DIM}{label}...{C.RESET}  "
                f"{C.DIM}{elapsed:.1f}s{C.RESET}"
            )
            sys.stdout.write(line)
            sys.stdout.flush()
            i += 1
            time.sleep(0.1)

    def start(self) -> "_Spinner":
        self._t0 = time.time()
        self._thread.start()
        return self

    def stop(self) -> float:
        """Stop the spinner, clear the line, return elapsed seconds."""
        self._stop.set()
        self._thread.join()
        sys.stdout.write(f"\r{' ' * self._WIDTH}\r")
        sys.stdout.flush()
        return time.time() - self._t0


# ── Terminal & Markdown utilities ─────────────────────────────────────────────

def _term_width() -> int:
    """Return current terminal column count (fallback 80)."""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _inline_md(text: str) -> str:
    """Apply inline markdown: **bold**, `code`, *italic*."""
    # Bold+italic ***text***
    text = re.sub(r'\*\*\*(.+?)\*\*\*', lambda m: C.BOLD + C.YELLOW + m.group(1) + C.RESET, text)
    # Bold **text**
    text = re.sub(r'\*\*(.+?)\*\*', lambda m: C.BOLD + m.group(1) + C.RESET, text)
    # Italic *text* (avoid lone *s between words/numbers)
    text = re.sub(r'(?<!\w)\*([^*\s][^*]*?)\*(?!\w)', lambda m: C.DIM + m.group(1) + C.RESET, text)
    # Inline code `code`
    text = re.sub(r'`([^`\n]+)`', lambda m: C.CYAN + m.group(1) + C.RESET, text)
    return text


def _render_md(text: str, width: int | None = None) -> str:
    """
    Render Markdown as ANSI-coloured terminal output.
    Wraps prose at *width* (defaults to terminal width − 4).
    Indents code blocks, formats headers, bullets, and numbered lists.
    """
    if width is None:
        width = max(40, _term_width() - 4)

    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    code_lang = ""

    def _flush_code() -> None:
        for cl in code_buf:
            out.append(f"  {C.CYAN}{cl}{C.RESET}")

    for raw in lines:
        # Code fence toggle
        if raw.startswith("```"):
            if not in_code:
                in_code = True
                code_lang = raw[3:].strip()
                code_buf = []
            else:
                in_code = False
                _flush_code()
                code_lang = ""
            continue

        if in_code:
            code_buf.append(raw)
            continue

        # Horizontal rule (--- / ___ / ***)
        if re.match(r'^[-_*]{3,}\s*$', raw):
            out.append(f"{C.DIM}{'─' * min(width, 60)}{C.RESET}")
            continue

        # ATX headers  # / ## / ###
        m = re.match(r'^(#{1,3})\s+(.*)', raw)
        if m:
            lvl, title = len(m.group(1)), m.group(2).strip()
            if lvl == 1:
                out.append(f"\n{C.BOLD}{C.WHITE}{title}{C.RESET}")
            elif lvl == 2:
                out.append(f"\n{C.BOLD}{title}{C.RESET}")
            else:
                out.append(f"{C.BOLD}{C.DIM}{title}{C.RESET}")
            continue

        # Bullet list  - / * / •
        m = re.match(r'^(\s{0,6})[-*•]\s+(.*)', raw)
        if m:
            pad = len(m.group(1))
            indent = "  " + " " * pad
            avail = max(20, width - len(indent) - 2)
            parts = textwrap.wrap(m.group(2), width=avail) or [""]
            out.append(f"{indent}{C.CYAN}•{C.RESET} {_inline_md(parts[0])}")
            for part in parts[1:]:
                out.append(f"{indent}  {_inline_md(part)}")
            continue

        # Numbered list  1. / 2. etc.
        m = re.match(r'^(\s{0,6})(\d+)[.)]\s+(.*)', raw)
        if m:
            pad = len(m.group(1))
            indent = "  " + " " * pad
            avail = max(20, width - len(indent) - 4)
            parts = textwrap.wrap(m.group(3), width=avail) or [""]
            out.append(f"{indent}{C.CYAN}{m.group(2)}.{C.RESET} {_inline_md(parts[0])}")
            for part in parts[1:]:
                out.append(f"{indent}   {_inline_md(part)}")
            continue

        # Blank line
        if not raw.strip():
            out.append("")
            continue

        # Prose — wrap first (on plain text), then apply inline markdown
        wrapped = textwrap.wrap(raw, width=width) or [raw]
        for line in wrapped:
            out.append(_inline_md(line))

    if in_code and code_buf:
        _flush_code()

    return "\n".join(out)


# ── Session storage ───────────────────────────────────────────────────────────

def _load_sessions() -> list[dict]:
    """Load saved sessions from disk. Returns list sorted newest-first."""
    try:
        with open(SESSIONS_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_session(session_id: str, last_user: str, last_reply: str) -> None:
    """Upsert a session entry and keep only the most recent MAX_SESSIONS."""
    sessions = _load_sessions()

    # Remove existing entry for this session_id
    sessions = [s for s in sessions if s.get("id") != session_id]

    # Prepend updated entry
    sessions.insert(0, {
        "id":         session_id,
        "name":       session_id.replace("cli-", ""),
        "last_user":  last_user[:120],
        "last_reply": last_reply[:120],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })

    # Trim to MAX_SESSIONS
    sessions = sessions[:MAX_SESSIONS]

    try:
        with open(SESSIONS_FILE, "w") as f:
            json.dump(sessions, f, indent=2)
    except Exception:
        pass  # non-fatal


def _get_session_by_name(name: str) -> str | None:
    """Return session_id for a named session, or None."""
    for s in _load_sessions():
        if s.get("name") == name or s.get("id") == name:
            return s["id"]
    return None


def _pick_session() -> str:
    """
    Show the last MAX_SESSIONS sessions and let the user pick one,
    or start a new one. Returns the chosen session_id.
    """
    sessions = _load_sessions()

    if not sessions:
        # No history — start fresh immediately
        sid = f"cli-{uuid.uuid4().hex[:8]}"
        print(f"{C.DIM}Starting new session {sid}{C.RESET}\n")
        return sid

    print(f"\n{C.BOLD}Recent sessions:{C.RESET}")
    _sep()
    for i, s in enumerate(sessions, 1):
        ts      = s.get("updated_at", "")[:16].replace("T", " ")
        snippet = s.get("last_user", "")[:55]
        name    = s.get("name", s.get("id", "?"))
        print(
            f"  {C.CYAN}{C.BOLD}[{i}]{C.RESET} "
            f"{C.DIM}{ts}{C.RESET}  "
            f"{C.YELLOW}{name:<20}{C.RESET}  "
            f"{C.DIM}{snippet}…{C.RESET}"
        )
    _sep()
    print(f"  {C.BOLD}[n]{C.RESET} {C.DIM}Start a new session{C.RESET}")
    print()

    try:
        choice = input(f"\001{C.CYAN}\002Pick a session (1–{len(sessions)}, or n for new):\001{C.RESET}\002 ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if choice == "n" or choice == "":
        sid = f"cli-{uuid.uuid4().hex[:8]}"
        print(f"\n{C.DIM}New session: {sid}{C.RESET}\n")
        return sid

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(sessions):
            sid = sessions[idx]["id"]
            print(f"\n{C.DIM}Resuming session: {sid}{C.RESET}\n")
            return sid
    except ValueError:
        pass

    # Bad input → new session
    sid = f"cli-{uuid.uuid4().hex[:8]}"
    print(f"\n{C.DIM}New session: {sid}{C.RESET}\n")
    return sid


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _api(method: str, path: str, body=None) -> dict:
    url  = f"{BRAIN_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", e.reason)
        except Exception:
            detail = e.reason
        return {"_error": f"HTTP {e.code}: {detail}"}
    except Exception as exc:
        return {"_error": str(exc)}


def _ok(d: dict) -> bool:
    if "_error" in d:
        _err(d["_error"])
        return False
    return True


def _err(msg: str) -> None:
    print(f"{C.RED}Error: {msg}{C.RESET}", file=sys.stderr)


def _sep() -> None:
    print(f"{C.DIM}{'─' * min(_term_width() - 1, 60)}{C.RESET}")

# ── Commands ──────────────────────────────────────────────────────────────────

def _is_deferred(reply: str, intent: str) -> bool:
    """
    Return True if the brain promised to do something but didn't return actual
    results — i.e., it needs a follow-up to actually execute.

    Signals:
      1. Reply contains a 'will do' / 'running now' phrase.
      2. Reply contains a ```bash block (LLM showed commands instead of running them).
    """
    if _DEFERRED_RE.search(reply):
        return True
    if _BASH_BLOCK_RE.search(reply):
        return True
    return False


def _fetch_reply(message: str, session_id: str) -> tuple[dict, float]:
    """POST to /api/v1/chat with spinner, return (result_dict, elapsed_seconds)."""
    result: dict = {}

    def _fetch() -> None:
        data = _api("POST", "/api/v1/chat", {"message": message, "session_id": session_id})
        result.update(data)

    thread = threading.Thread(target=_fetch, daemon=True)

    if _SPINNER_ENABLED:
        spinner = _Spinner()
        spinner.start()
        thread.start()
        try:
            thread.join()
        except KeyboardInterrupt:
            spinner.stop()
            print(f"\n{C.YELLOW}Cancelled.{C.RESET}\n")
            return {}, 0.0
        elapsed = spinner.stop()
    else:
        t0 = time.time()
        thread.start()
        thread.join()
        elapsed = time.time() - t0

    return result, elapsed


def cmd_chat(message: str, session_id: str) -> None:
    width = _term_width()

    result, elapsed = _fetch_reply(message, session_id)
    if not _ok(result):
        return

    reply  = result.get("reply", "")
    intent = result.get("intent", "chat")
    agent  = result.get("agent", "default")

    # ── Auto-follow-up loop ───────────────────────────────────────────────────
    # If the brain returned a "will do it" response instead of actual results,
    # automatically send a follow-up to get the real output.
    followups = 0
    while followups < _MAX_FOLLOWUPS and _is_deferred(reply, intent):
        followups += 1
        sep_dim = f"{C.DIM}{'─' * min(width - 1, 72)}{C.RESET}"
        print(
            f"\n{C.YELLOW}{C.BOLD}◆{C.RESET} {C.DIM}Brain deferred — following up "
            f"({followups}/{_MAX_FOLLOWUPS})...{C.RESET}"
        )
        print(sep_dim)
        followup_result, followup_elapsed = _fetch_reply(_FOLLOWUP_MSG, session_id)
        if not _ok(followup_result):
            break
        elapsed += followup_elapsed
        reply   = followup_result.get("reply", reply)
        intent  = followup_result.get("intent", intent)
        agent   = followup_result.get("agent", agent)
        result  = followup_result

    sep = f"{C.DIM}{'─' * min(width - 1, 72)}{C.RESET}"
    agent_tag = (
        f"  {C.DIM}via {agent}{C.RESET}"
        if agent and agent not in ("default", "")
        else ""
    )
    followup_tag = (
        f"  {C.DIM}+{followups} follow-up{'s' if followups > 1 else ''}{C.RESET}"
        if followups
        else ""
    )
    print(
        f"\n{C.GREEN}{C.BOLD}◆{C.RESET} {C.BOLD}Brain{C.RESET}  "
        f"{C.DIM}[{intent}]{C.RESET}"
        f"{agent_tag}"
        f"{followup_tag}"
        f"  {C.DIM}{elapsed:.1f}s{C.RESET}"
    )
    print(sep)
    print(_render_md(reply, width=min(width - 2, 100)))
    print(sep)
    print()
    _save_session(session_id, message, reply)


def cmd_level_get() -> None:
    d = _api("GET", "/api/v1/approval/level")
    if not _ok(d):
        return
    lvl = d.get("level", 1)
    lbl = d.get("label", "")
    colors = {1: C.RED, 2: C.YELLOW, 3: C.GREEN}
    c = colors.get(lvl, C.WHITE)
    print(f"\n{C.BOLD}Approval Level:{C.RESET} {c}{C.BOLD}Level {lvl}{C.RESET}")
    print(f"  {C.DIM}{lbl}{C.RESET}\n")


def cmd_level_set(level: str) -> None:
    try:
        lvl = int(level)
    except ValueError:
        _err("Level must be 1, 2, or 3")
        return
    d = _api("POST", "/api/v1/approval/level", {"level": lvl})
    if not _ok(d):
        return
    print(f"{C.GREEN}Level set to {d.get('level')}{C.RESET}: {d.get('label')}\n")


def cmd_pending() -> None:
    d = _api("GET", "/api/v1/approval/pending")
    if not _ok(d):
        return
    tasks = d.get("tasks", [])
    if not tasks:
        print(f"\n{C.DIM}No pending approvals.{C.RESET}\n")
        return
    print(f"\n{C.YELLOW}{C.BOLD}Pending Approvals — {len(tasks)} task(s){C.RESET}")
    _sep()
    cat_c = {"standard": C.CYAN, "critical": C.YELLOW, "breaking": C.RED}
    for t in tasks:
        cc    = cat_c.get(t.get("category", ""), C.WHITE)
        sid   = t["task_id"][:8]
        title = (t.get("title") or t.get("action", "?"))[:52]
        print(f"  {C.DIM}{sid}{C.RESET}  {cc}{t.get('category','?'):<10}{C.RESET}  {title}")
    _sep()
    print(f"{C.DIM}Use /approve <id> or /cancel <id> (first 8 chars OK){C.RESET}\n")


def _resolve_task_id(prefix: str) -> str | None:
    d = _api("GET", "/api/v1/approval/pending")
    tasks = d.get("tasks", [])
    matches = [t["task_id"] for t in tasks if t["task_id"].startswith(prefix)]
    if not matches:
        _err(f"No pending task matching '{prefix}'")
        return None
    return matches[0]


def cmd_approve(prefix: str) -> None:
    task_id = _resolve_task_id(prefix)
    if not task_id:
        return
    print(f"{C.DIM}Approving…{C.RESET}")
    r = _api("POST", f"/api/v1/approval/approve/{task_id}")
    if not _ok(r):
        return
    print(f"{C.GREEN}Approved and executed.{C.RESET}")
    if r.get("reply"):
        print(f"{C.DIM}{r['reply']}{C.RESET}")
    print()


def cmd_cancel(prefix: str) -> None:
    task_id = _resolve_task_id(prefix)
    if not task_id:
        return
    r = _api("POST", f"/api/v1/approval/cancel/{task_id}")
    if _ok(r):
        print(f"{C.YELLOW}Cancelled.{C.RESET}\n")


def cmd_health() -> None:
    d = _api("GET", "/api/v1/health")
    if not _ok(d):
        return
    ok = lambda v: f"{C.GREEN}OK{C.RESET}" if v else f"{C.RED}FAIL{C.RESET}"
    print(f"\n{C.BOLD}System Health{C.RESET}")
    print(f"  Brain    {ok(d.get('status') == 'ok')}")
    print(f"  Redis    {ok(d.get('redis'))}")
    print(f"  Postgres {ok(d.get('postgres'))}")
    print()


def cmd_costs() -> None:
    d = _api("GET", "/api/v1/costs")
    if not _ok(d):
        return
    print(f"\n{C.BOLD}LLM Costs Today{C.RESET}")
    for k, v in d.items():
        print(f"  {k:<28} {v}")
    print()


def cmd_tasks() -> None:
    d = _api("GET", "/api/v1/tasks")
    if not _ok(d):
        return
    state_c = {"active": C.GREEN, "reserved": C.CYAN, "scheduled": C.YELLOW}
    rows = []
    for state, workers in d.items():
        if not isinstance(workers, dict):
            continue
        for worker, tasks in workers.items():
            for t in (tasks or []):
                rows.append((state, t.get("name", "?").split(".")[-1], (t.get("id") or "")[:8]))
    if not rows:
        print(f"\n{C.DIM}Queue is idle.{C.RESET}\n")
        return
    print(f"\n{C.BOLD}Task Queue ({len(rows)}){C.RESET}")
    for state, name, tid in rows:
        sc = state_c.get(state, C.WHITE)
        print(f"  {sc}{state:<9}{C.RESET}  {name:<30}  {C.DIM}{tid}{C.RESET}")
    print()


def cmd_history() -> None:
    d = _api("GET", "/api/v1/approval/history")
    if not _ok(d):
        return
    tasks = d.get("tasks", [])
    if not tasks:
        print(f"\n{C.DIM}No write history yet.{C.RESET}\n")
        return
    status_c = {"completed": C.GREEN, "failed": C.RED, "cancelled": C.YELLOW}
    print(f"\n{C.BOLD}Write History ({len(tasks)}){C.RESET}")
    _sep()
    for t in tasks:
        sc    = status_c.get(t.get("status", ""), C.WHITE)
        title = (t.get("title") or t.get("action", "?"))[:40]
        print(f"  {sc}{t.get('status','?'):<11}{C.RESET}  {t.get('action','?'):<22}  {title}")
    _sep()
    print()


def cmd_sessions() -> None:
    """List stored sessions."""
    sessions = _load_sessions()
    if not sessions:
        print(f"\n{C.DIM}No saved sessions.{C.RESET}\n")
        return
    print(f"\n{C.BOLD}Saved Sessions ({len(sessions)}){C.RESET}")
    _sep()
    for i, s in enumerate(sessions, 1):
        ts      = s.get("updated_at", "")[:16].replace("T", " ")
        snippet = s.get("last_user", "")[:55]
        name    = s.get("name", s.get("id", "?"))
        sid     = s.get("id", "")
        print(
            f"  {C.CYAN}{C.BOLD}[{i}]{C.RESET} "
            f"{C.DIM}{ts}{C.RESET}  "
            f"{C.YELLOW}{name:<20}{C.RESET}  "
            f"{C.DIM}{snippet}{C.RESET}"
        )
        print(f"      {C.DIM}id: {sid}{C.RESET}")
    _sep()
    print(f"{C.DIM}Resume with: brain --session <name>{C.RESET}\n")


def cmd_mytasks(
    status: str | None = None,
    priority: str | None = None,
) -> None:
    """Show task board — tasks created by the AI brain."""
    path = "/api/v1/board/tasks"
    qs   = []
    if status:
        qs.append(f"status={status}")
    if priority:
        qs.append(f"priority={priority}")
    if qs:
        path += "?" + "&".join(qs)

    d = _api("GET", path)
    if not _ok(d):
        return

    tasks = d.get("tasks", [])
    count = d.get("count", len(tasks))

    if not tasks:
        print(f"\n{C.DIM}No tasks found.{C.RESET}\n")
        return

    pri_c = {1: C.DIM, 2: C.CYAN, 3: C.YELLOW, 4: C.YELLOW, 5: C.RED}
    sta_e = {"pending": "⏳", "in_progress": "🔄", "done": "✅", "cancelled": "❌"}
    apv_l = {1: "auto", 2: "review", 3: "sign-off"}

    print(f"\n{C.BOLD}Task Board{C.RESET}  {C.DIM}({count} tasks){C.RESET}")
    _sep()
    for t in tasks:
        pri  = t.get("priority_num") or 3
        alv  = t.get("approval_level") or 2
        stat = t.get("status", "pending")
        pc   = pri_c.get(pri, C.WHITE)
        emoji = sta_e.get(stat, "?")
        tid  = str(t.get("id", "?"))
        title = (t.get("title") or "?")[:48]
        plabel = t.get("priority_label", str(pri))
        alvlabel = apv_l.get(alv, str(alv))
        print(
            f"  {emoji} {C.BOLD}#{tid:<4}{C.RESET}  "
            f"{pc}{plabel:<12}{C.RESET}  "
            f"apv:{C.DIM}{alvlabel:<10}{C.RESET}  "
            f"{title}"
        )
        if t.get("due_date"):
            print(f"         {C.DIM}Due: {str(t['due_date'])[:10]}{C.RESET}")
    _sep()
    print(f"{C.DIM}Use /mytasks [status] or: brain mytasks pending|done|in_progress{C.RESET}\n")


# ── Server context ─────────────────────────────────────────────────────────────

def _run_cmd(cmd: str, cwd: str | None = None, timeout: int = 5) -> str:
    """Run a shell command and return combined output (stdout + stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        out = (result.stdout + result.stderr).strip()
        return out[:2000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "(timed out)"
    except Exception as e:
        return f"(error: {e})"


def _gather_server_context() -> dict:
    """Collect git, docker, and env info from the server."""
    ctx: dict = {}

    # Repo context
    if _REPO_ROOT and (_REPO_ROOT / ".git").exists():
        ctx["repo_root"]   = str(_REPO_ROOT)
        ctx["git_branch"]  = _run_cmd("git rev-parse --abbrev-ref HEAD",    cwd=str(_REPO_ROOT))
        ctx["git_status"]  = _run_cmd("git status --short",                  cwd=str(_REPO_ROOT))
        ctx["git_log"]     = _run_cmd("git log --oneline -5",                cwd=str(_REPO_ROOT))
    else:
        ctx["repo_root"] = "(not a git repo or .env not found)"

    # Docker containers
    ctx["docker_ps"] = _run_cmd(
        "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null "
        "|| echo '(docker not available)'"
    )

    # Env config summary — show which key groups are configured (not values)
    _KEY_GROUPS = {
        "Anthropic":   ["ANTHROPIC_API_KEY"],
        "OpenAI":      ["OPENAI_API_KEY"],
        "Slack":       ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"],
        "GitHub":      ["GITHUB_TOKEN"],
        "Google":      ["GOOGLE_CLIENT_ID_1"],
        "Sentry":      ["SENTRY_DSN"],
        "IONOS":       ["IONOS_TOKEN"],
        "WhatsApp":    ["TWILIO_ACCOUNT_SID"],
        "n8n":         ["N8N_API_KEY"],
        "Postgres":    ["POSTGRES_HOST"],
        "Redis":       ["REDIS_HOST"],
        "Qdrant":      ["QDRANT_HOST"],
    }
    configured = []
    missing    = []
    for group, keys in _KEY_GROUPS.items():
        if any(os.environ.get(k) for k in keys):
            configured.append(group)
        else:
            missing.append(group)
    ctx["env_configured"] = configured
    ctx["env_missing"]    = missing
    ctx["env_file"]       = str(_ENV_FILE) if _ENV_FILE else "(not found)"

    return ctx


def cmd_context() -> None:
    """Show full server / repo / env context."""
    print(f"\n{C.BOLD}Server Context{C.RESET}")
    _sep()

    ctx = _gather_server_context()

    # Env file
    ef = ctx.get("env_file", "(not found)")
    print(f"{C.BOLD}Env file:{C.RESET}  {C.GREEN if _ENV_FILE else C.RED}{ef}{C.RESET}")

    # Repo
    print(f"\n{C.BOLD}Repository:{C.RESET}  {ctx.get('repo_root', '?')}")
    if ctx.get("git_branch"):
        print(f"  Branch:  {C.CYAN}{ctx['git_branch']}{C.RESET}")
    if ctx.get("git_log"):
        print(f"\n{C.DIM}Recent commits:{C.RESET}")
        for line in ctx["git_log"].splitlines():
            print(f"  {C.DIM}{line}{C.RESET}")
    if ctx.get("git_status"):
        gstat = ctx["git_status"]
        if gstat and gstat != "(no output)":
            print(f"\n{C.YELLOW}Uncommitted changes:{C.RESET}")
            for line in gstat.splitlines():
                print(f"  {line}")
        else:
            print(f"\n{C.GREEN}Working tree clean{C.RESET}")

    # Docker
    print(f"\n{C.BOLD}Docker containers:{C.RESET}")
    for line in ctx.get("docker_ps", "(unavailable)").splitlines():
        print(f"  {line}")

    # Integrations
    configured = ctx.get("env_configured", [])
    missing    = ctx.get("env_missing", [])
    print(f"\n{C.BOLD}Integrations:{C.RESET}")
    if configured:
        print(f"  {C.GREEN}Configured:{C.RESET}  {', '.join(configured)}")
    if missing:
        print(f"  {C.DIM}Not set:    {', '.join(missing)}{C.RESET}")

    _sep()
    print()


def cmd_git() -> None:
    """Show git status and recent log."""
    root = str(_REPO_ROOT) if _REPO_ROOT else None
    if not root:
        print(f"\n{C.YELLOW}No repo root found (is .env in the repo dir?){C.RESET}\n")
        return
    print(f"\n{C.BOLD}Git — {root}{C.RESET}")
    _sep()
    branch = _run_cmd("git rev-parse --abbrev-ref HEAD", cwd=root)
    print(f"Branch: {C.CYAN}{branch}{C.RESET}\n")
    print(_run_cmd("git status", cwd=root))
    print(f"\n{C.BOLD}Recent commits:{C.RESET}")
    print(_run_cmd("git log --oneline -10", cwd=root))
    _sep()
    print()


def cmd_docker() -> None:
    """Show Docker container states."""
    print(f"\n{C.BOLD}Docker Containers{C.RESET}")
    _sep()
    print(_run_cmd("docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}'"))
    _sep()
    print()


def cmd_clear(session_id: str) -> None:
    _api("DELETE", f"/api/v1/chat/{session_id}")
    # Remove from local sessions file too
    sessions = [s for s in _load_sessions() if s.get("id") != session_id]
    try:
        with open(SESSIONS_FILE, "w") as f:
            json.dump(sessions, f, indent=2)
    except Exception:
        pass
    print(f"{C.DIM}Session memory cleared and removed from history.{C.RESET}\n")


# ── REPL ──────────────────────────────────────────────────────────────────────

_HELP = f"""
{C.BOLD}Commands{C.RESET}
{C.DIM}{'─' * 56}{C.RESET}
  {C.CYAN}/help{C.RESET}              Show this help
  {C.CYAN}/health{C.RESET}            System health check
  {C.CYAN}/costs{C.RESET}             Today's LLM spend
  {C.CYAN}/context{C.RESET}           Server, repo, Docker, env context
  {C.CYAN}/git{C.RESET}               Git status and recent commits
  {C.CYAN}/docker{C.RESET}            Docker container states
  {C.CYAN}/sessions{C.RESET}          List saved sessions
  {C.CYAN}/level{C.RESET}             Current approval level
  {C.CYAN}/level <1|2|3>{C.RESET}     Set approval level
  {C.CYAN}/pending{C.RESET}           Write tasks awaiting approval
  {C.CYAN}/approve <id>{C.RESET}      Approve a pending task
  {C.CYAN}/cancel  <id>{C.RESET}      Cancel a pending task
  {C.CYAN}/tasks{C.RESET}             Celery task queue
  {C.CYAN}/mytasks{C.RESET}           Task board (AI-created tasks)
  {C.CYAN}/mytasks <status>{C.RESET}  Filter: pending, in_progress, done
  {C.CYAN}/history{C.RESET}           Recent write action history
  {C.CYAN}/clear{C.RESET}             Clear this session's memory
  {C.CYAN}/exit{C.RESET}              Quit {C.DIM}(also Ctrl+D){C.RESET}
{C.DIM}{'─' * 56}{C.RESET}
{C.BOLD}Self-modification{C.RESET}
  {C.DIM}read brain.py{C.RESET}             → reads the CLI source file
  {C.DIM}update brain.py to add X{C.RESET}  → patches file + commits
  {C.DIM}deploy{C.RESET}                    → rebuilds + restarts brain container
  {C.DIM}read app/brain/intent.py{C.RESET}  → reads intent classifier
{C.DIM}{'─' * 56}{C.RESET}
  {C.DIM}Any other input is sent to Brain as a chat message.
  Default session is shared with Slack and REST API ({C.RESET}{C.GREEN}[shared ✦]{C.DIM}).
  Use --session NAME for a private isolated session.{C.RESET}
"""


def _handle_slash(line: str, session_id: str) -> bool:
    """Handle /command. Returns False to exit."""
    parts = line[1:].split(maxsplit=2)
    cmd   = parts[0].lower() if parts else ""

    if cmd in ("exit", "quit", "q"):
        return False
    elif cmd == "help":
        print(_HELP)
    elif cmd == "sessions":
        cmd_sessions()
    elif cmd == "level":
        if len(parts) > 1:
            cmd_level_set(parts[1])
        else:
            cmd_level_get()
    elif cmd == "pending":
        cmd_pending()
    elif cmd == "approve":
        if len(parts) > 1:
            cmd_approve(parts[1])
        else:
            _err("Usage: /approve <task-id-prefix>")
    elif cmd == "cancel":
        if len(parts) > 1:
            cmd_cancel(parts[1])
        else:
            _err("Usage: /cancel <task-id-prefix>")
    elif cmd == "health":
        cmd_health()
    elif cmd == "costs":
        cmd_costs()
    elif cmd == "tasks":
        cmd_tasks()
    elif cmd == "history":
        cmd_history()
    elif cmd == "mytasks":
        status = parts[1] if len(parts) > 1 else None
        cmd_mytasks(status=status)
    elif cmd == "context":
        cmd_context()
    elif cmd == "git":
        cmd_git()
    elif cmd == "docker":
        cmd_docker()
    elif cmd == "clear":
        cmd_clear(session_id)
    else:
        _err(f"Unknown command /{cmd} — try /help")
    return True


def repl(session_id: str) -> None:
    # Readline history
    try:
        import readline
        try:
            readline.read_history_file(HISTORY_FILE)
        except FileNotFoundError:
            pass
        readline.set_history_length(500)
        _rl = readline
    except ImportError:
        _rl = None

    # Startup banner
    d   = _api("GET", "/api/v1/approval/level")
    lvl = d.get("level", "?") if "_error" not in d else "?"
    lbl = d.get("label", d.get("_error", "")) if "_error" not in d else d.get("_error", "")
    lc  = {1: C.RED, 2: C.YELLOW, 3: C.GREEN}.get(lvl, C.WHITE)

    width = _term_width()
    name = session_id.replace("cli-", "")
    is_shared = (session_id == PRIMARY_SESSION)

    # Gather branch if available
    branch = ""
    if _REPO_ROOT and (_REPO_ROOT / ".git").exists():
        try:
            branch_out = subprocess.run(
                "git rev-parse --abbrev-ref HEAD",
                shell=True, capture_output=True, text=True, timeout=3,
                cwd=str(_REPO_ROOT),
            )
            branch = branch_out.stdout.strip()
        except Exception:
            pass

    # Title line
    shared_tag = f"  {C.GREEN}[shared ✦]{C.RESET}" if is_shared else ""
    print(f"\n{C.BOLD}{C.CYAN}Brain{C.RESET} {C.GREEN}◆{C.RESET}  {C.DIM}{BRAIN_URL}{C.RESET}")

    # Session + approval
    session_label = f"{C.DIM}session={C.RESET}{C.CYAN}{name}{C.RESET}{shared_tag}"
    approval_label = f"{lc}{C.BOLD}Level {lvl}{C.RESET}  {C.DIM}{lbl}{C.RESET}"
    print(f"{session_label}  {approval_label}")

    # Env + branch footer
    footer_parts: list[str] = []
    if _ENV_FILE:
        footer_parts.append(f"env={C.GREEN}{_ENV_FILE.name}{C.RESET}")
    if branch:
        footer_parts.append(f"branch={C.CYAN}{branch}{C.RESET}")
    footer_parts.append(f"{C.DIM}/help for commands · Ctrl+D to quit{C.RESET}")
    print("  ".join(footer_parts))
    print(f"{C.DIM}{'─' * min(width - 1, 72)}{C.RESET}\n")

    # Wrap ANSI codes in \001/\002 so readline knows they are zero-width.
    # Without these markers readline miscounts the prompt length, causing
    # typed text to wrap back to column 0 and overwrite itself.
    _rl_prompt = "\001" + C.GREEN + "\002>\001" + C.RESET + "\002 "

    while True:
        try:
            line = input(_rl_prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        if _rl:
            try:
                _rl.write_history_file(HISTORY_FILE)
            except Exception:
                pass

        if line.startswith("/"):
            if not _handle_slash(line, session_id):
                break
        else:
            cmd_chat(line, session_id)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    # ── --session flag ────────────────────────────────────────────────────────
    session_id = None
    if args and args[0] == "--session":
        if len(args) < 2:
            _err("Usage: brain --session <name>")
            sys.exit(1)
        name = args[1]
        args = args[2:]
        # Special values: "shared" / "brain" → use the primary session
        if name in ("shared", PRIMARY_SESSION):
            session_id = PRIMARY_SESSION
            print(f"{C.DIM}Joining shared primary session '{PRIMARY_SESSION}'{C.RESET}")
        else:
            # Try to find existing session by name, else create one with that name
            existing = _get_session_by_name(name)
            session_id = existing if existing else f"cli-{name}"
            if existing:
                print(f"{C.DIM}Resuming session '{name}'{C.RESET}")
            else:
                print(f"{C.DIM}New private session '{name}'{C.RESET}")

    # ── No subcommand → REPL ──────────────────────────────────────────────────
    if not args:
        if session_id is None:
            # Default to the shared primary session so CLI participates in
            # cross-interface memory (same pool as Slack and REST API).
            # Use --session NAME to start a private isolated session instead.
            session_id = PRIMARY_SESSION
        repl(session_id)
        return

    # ── Subcommands ───────────────────────────────────────────────────────────
    cmd  = args[0].lower()
    rest = args[1:]

    # For one-shot chat, use the shared primary session if not specified
    if session_id is None:
        session_id = PRIMARY_SESSION

    if cmd == "chat":
        if not rest:
            _err("Usage: brain chat <message>")
        else:
            cmd_chat(" ".join(rest), session_id)
    elif cmd == "health":
        cmd_health()
    elif cmd == "costs":
        cmd_costs()
    elif cmd == "pending":
        cmd_pending()
    elif cmd == "approve":
        if rest:
            cmd_approve(rest[0])
        else:
            _err("Usage: brain approve <id-prefix>")
    elif cmd == "cancel":
        if rest:
            cmd_cancel(rest[0])
        else:
            _err("Usage: brain cancel <id-prefix>")
    elif cmd == "level":
        if rest:
            cmd_level_set(rest[0])
        else:
            cmd_level_get()
    elif cmd == "tasks":
        cmd_tasks()
    elif cmd == "history":
        cmd_history()
    elif cmd == "sessions":
        cmd_sessions()
    elif cmd == "mytasks":
        status   = rest[0] if rest else None
        priority = rest[1] if len(rest) > 1 else None
        cmd_mytasks(status=status, priority=priority)
    elif cmd == "context":
        cmd_context()
    elif cmd == "git":
        cmd_git()
    elif cmd == "docker":
        cmd_docker()
    else:
        print(
            f"Usage: {sys.argv[0]} [--session NAME] "
            "[chat|health|costs|pending|approve|cancel|level|tasks|history|sessions"
            "|mytasks|context|git|docker]"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
