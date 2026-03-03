#!/usr/bin/env python3
"""
Brain CLI — terminal interface to the AI Brain.

Usage:
  python brain.py                  Interactive REPL (picks or starts a session)
  python brain.py --session NAME   Resume or create a named session
  python brain.py chat "message"   One-shot chat (uses last session)
  python brain.py health           System health
  python brain.py costs            Today's LLM spend
  python brain.py pending          List pending write approvals
  python brain.py approve <id>     Approve a pending task
  python brain.py cancel  <id>     Cancel a pending task
  python brain.py level [1|2|3]    Get / set approval level
  python brain.py tasks            Show Celery queue
  python brain.py sessions         List saved sessions

Environment:
  BRAIN_URL   Base URL of the Brain API (default: http://localhost:8000)
  NO_COLOR    Set to any value to disable ANSI colours
"""

import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
BRAIN_URL     = os.environ.get("BRAIN_URL", "http://localhost:8000").rstrip("/")
HISTORY_FILE  = os.path.expanduser("~/.brain_history")
SESSIONS_FILE = os.path.expanduser("~/.brain_sessions.json")
MAX_SESSIONS  = 3   # number of sessions shown in picker + stored

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
        choice = input(f"{C.CYAN}Pick a session (1–{len(sessions)}, or n for new):{C.RESET} ").strip().lower()
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
    print(f"{C.DIM}{'─' * 60}{C.RESET}")

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_chat(message: str, session_id: str) -> None:
    d = _api("POST", "/api/v1/chat", {"message": message, "session_id": session_id})
    if not _ok(d):
        return
    reply  = d.get("reply", "")
    intent = d.get("intent", "chat")
    agent  = d.get("agent", "default")
    print(f"\n{C.GREEN}{C.BOLD}Brain{C.RESET} {C.DIM}[{intent} · {agent}]{C.RESET}")
    _sep()
    print(reply)
    _sep()
    print()
    # Persist session metadata
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
  /help              Show this help
  /sessions          List saved sessions
  /level             Current approval level
  /level <1|2|3>     Set approval level
  /pending           List write tasks awaiting approval
  /approve <id>      Approve a task (first 8 chars of ID)
  /cancel  <id>      Cancel a task
  /tasks             Celery task queue
  /health            System health
  /costs             Today's LLM spend
  /history           Recent write action history
  /clear             Clear this session's memory
  /exit              Quit ({C.DIM}also Ctrl+D{C.RESET})

  {C.DIM}Any other input is sent to Brain as a chat message.{C.RESET}
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

    name = session_id.replace("cli-", "")
    print(f"{C.CYAN}{C.BOLD}Brain CLI{C.RESET}  {C.DIM}session={name}  url={BRAIN_URL}{C.RESET}")
    print(f"Approval: {lc}{C.BOLD}Level {lvl}{C.RESET}  {C.DIM}{lbl}{C.RESET}")
    print(f"{C.DIM}Type /help for commands. Ctrl+D or /exit to quit.{C.RESET}\n")

    while True:
        try:
            line = input(f"{C.CYAN}>{C.RESET} ").strip()
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
        # Try to find existing session by name, else create one with that name
        existing = _get_session_by_name(name)
        session_id = existing if existing else f"cli-{name}"
        if existing:
            print(f"{C.DIM}Resuming session '{name}'{C.RESET}")
        else:
            print(f"{C.DIM}New session '{name}'{C.RESET}")

    # ── No subcommand → REPL ──────────────────────────────────────────────────
    if not args:
        if session_id is None:
            session_id = _pick_session()
        repl(session_id)
        return

    # ── Subcommands ───────────────────────────────────────────────────────────
    cmd  = args[0].lower()
    rest = args[1:]

    # For one-shot chat, use last session if not specified
    if session_id is None:
        sessions   = _load_sessions()
        session_id = sessions[0]["id"] if sessions else f"cli-{uuid.uuid4().hex[:8]}"

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
    else:
        print(f"Usage: {sys.argv[0]} [--session NAME] [chat|health|costs|pending|approve|cancel|level|tasks|history|sessions]")
        sys.exit(1)


if __name__ == "__main__":
    main()
