"""
Autonomous Bug Hunter — scans Loki logs for error patterns, clusters them,
runs LLM root-cause analysis, proposes fixes, and posts a Slack report.

Runs every 6 hours via Celery beat, and on-demand via the bug_hunt skill.

Pipeline:
  1. Fetch error/exception/traceback lines from Loki for the last N hours
  2. Map container IDs → service names via Docker
  3. Cluster similar errors by normalised message fingerprint
  4. Rank clusters by frequency; take top MAX_CLUSTERS
  5. Analyse each cluster with Claude Haiku (root cause + fix proposal)
  6. Post a structured Slack report to sentinel-alerts
  7. Auto-create board tasks for high-severity, auto-fixable bugs
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections import defaultdict

import anthropic
import httpx
from celery import shared_task

logger = logging.getLogger(__name__)

_LOKI_URL = "http://loki:3100"

# Fetch error-level lines; exclude:
# - "level=info" — Go-service info logs (Loki/Prometheus) whose embedded query
#   strings contain "error"/"exception" and false-match the include filter
# - HTTP access log noise from nginx scanner traffic
# - Loki internal query metric fields
_ERROR_QUERY = (
    '{job="docker"} '
    '|~ "(?i)(traceback|error|exception|critical)" '
    '!~ "level=info" '
    '!~ "GET /" '
    '!~ "POST /" '
    '!~ "PUT /" '
    '!~ "No such file or directory" '
    '!~ "query_hash=" '
    '!~ "throughput="'
)

_MAX_LOKI_LINES = 5_000  # Loki default max_entries_limit_per_query
_MAX_CLUSTERS = 10      # clusters to rank
_MAX_ANALYZE = 6        # clusters to send to LLM
_MAX_SAMPLE_LINES = 8   # log lines per cluster sent to LLM

# Dynamic tokens to strip before clustering
_DYNAMIC_RE = re.compile(
    r"\b("
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"  # UUID
    r"[0-9a-f]{32,}|"                       # long hex IDs
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d*Z?|"  # ISO timestamps
    r"\d{10,}|"                              # epoch timestamps / long ints
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|" # IP addresses
    r"s_\d+|"                                # Slack session IDs
    r"task_id=[^\s,)]+|"                     # task IDs
    r"ts=\S+"                                # ts= fields
    r")\b",
    re.IGNORECASE,
)

_LOG_PREFIX_RE = re.compile(
    r"^\d{4}[-/]\d{2}[-/]\d{2}[\sT]\d{2}:\d{2}:\d{2}[.,\s]\d*\s*\|?\s*\w*\s*\|?\s*\S*\s*[-|]\s*"
)
_LOKI_PREFIX_RE = re.compile(
    r"^level=\w+\s+ts=\S+\s+caller=\S+\s+"
)

_ANALYSIS_SYSTEM = (
    "You are an expert SRE and software engineer analyzing application error logs. "
    "Identify root causes and propose specific, actionable fixes. "
    "Respond ONLY with valid compact JSON — no explanation, no markdown."
)

_ANALYSIS_PROMPT = """\
Service: {service}
Error pattern ({count}x in last {hours}h):
{sample}

JSON response schema:
{{
  "severity": "critical|high|medium|low",
  "root_cause": "1-2 sentence root cause",
  "affected_component": "file:line or module or service component",
  "proposed_fix": "specific actionable fix — mention file/function if visible",
  "fix_snippet": "short code example showing the fix, or null",
  "fix_complexity": "simple|medium|complex",
  "auto_fixable": true or false,
  "is_noise": true or false
}}

Set is_noise=true for: bot/scanner traffic, known-benign reconnects, expected HTTP 404s.
Be specific — reference actual file paths and function names visible in the logs."""

_SEVERITY_BADGE = {
    "critical": "🔴 CRITICAL",
    "high":     "🟠 HIGH",
    "medium":   "🟡 MEDIUM",
    "low":      "🟢 LOW",
}
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_container_service_map() -> dict[str, str]:
    """
    Build short_container_id(12) → clean service name.
    Uses `docker ps -a --last 100` to include recently rebuilt containers
    whose old IDs still appear in Loki logs from the scan window.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--last", "100", "--format", "{{.ID}}\t{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        mapping: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                short_id, name = parts
                clean = (
                    name.strip()
                    .removeprefix("ai-")
                    .removeprefix("brain-")
                )
                mapping[short_id.strip()] = clean
        return mapping
    except Exception as exc:
        logger.warning("Could not build container map: %s", exc)
        return {}


# Caller filename → service name for Go-binary services
_GO_CALLER_SERVICE = {
    "scheduler_processor": "loki",
    "frontend_processor": "loki",
    "ring_watcher":        "loki",
    "compactor":           "loki",
    "ingester":            "loki",
    "engine.go":           "loki",
    "roundtrip.go":        "loki",
    "worker.go":           "loki",
    "nginx":               "nginx",
    "prometheus":          "prometheus",
    "grafana":             "grafana",
}


def _infer_service_from_log(line: str) -> str | None:
    """
    Attempt to infer the service name from the log line content when the
    container ID is not in the Docker map (e.g. recently rebuilt containers).
    """
    # Go-format: "caller=scheduler_processor.go:106"
    caller_match = re.search(r"caller=([\w./]+\.go):", line)
    if caller_match:
        fname = caller_match.group(1).lower()
        for key, svc in _GO_CALLER_SERVICE.items():
            if key in fname:
                return svc
        return "go-service"

    # Python/Loguru: "| ERROR | app.skills.xxx:method:42"
    if re.search(r"\|\s*(ERROR|CRITICAL|WARNING)\s*\|\s*app\.", line):
        # try to get the module
        mod = re.search(r"\|\s*\w+\s*\|\s*app\.([\w.]+):", line)
        if mod:
            parts = mod.group(1).split(".")
            if "worker" in parts:
                return "celery-worker"
            return "brain"
        return "brain"

    # Nginx error format: "2026/03/05 15:06:13 [error] 30#30:"
    if re.search(r"\[\s*error\s*\]\s+\d+#\d+:", line):
        return "nginx"

    return None


def _service_from_filename(filename: str, container_map: dict[str, str]) -> str:
    """Extract service name from Loki filename label, with content-based fallback."""
    match = re.search(r"/containers/([a-f0-9]{64})/", filename)
    if match:
        short_id = match.group(1)[:12]
        return container_map.get(short_id, "")  # empty = unresolved
    return ""


def _normalize_line(line: str) -> str:
    """Strip dynamic tokens for clustering; extract core message."""
    line = _LOKI_PREFIX_RE.sub("", line)
    line = _LOG_PREFIX_RE.sub("", line)
    # For Loki-format: extract msg="..." value
    msg_match = re.search(r'msg="([^"]{10,})"', line)
    if msg_match:
        core = msg_match.group(1)
    else:
        # For Python exceptions: take line from "Error:" or "Exception:" onward
        exc_match = re.search(r'(\w+Error|\w+Exception|Traceback|CRITICAL|ERROR).*', line)
        core = exc_match.group(0) if exc_match else line
    core = _DYNAMIC_RE.sub("*", core)
    return core.strip()[:180]


def _fetch_loki_errors(hours: int) -> dict[str, list[str]]:
    """
    Query Loki for error lines in the last `hours` hours.
    Returns {service: [line, ...]} with service names resolved from Docker.
    """
    container_map = _get_container_service_map()
    end_ns = int(time.time()) * 1_000_000_000
    start_ns = end_ns - hours * 3_600 * 1_000_000_000

    try:
        resp = httpx.get(
            f"{_LOKI_URL}/loki/api/v1/query_range",
            params={
                "query": _ERROR_QUERY,
                "start": str(start_ns),
                "end":   str(end_ns),
                "limit": str(_MAX_LOKI_LINES),
                "direction": "forward",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Bug hunter Loki fetch failed: %s", exc)
        return {}

    service_lines: dict[str, list[str]] = defaultdict(list)
    for stream in data.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        filename = labels.get("filename", "")
        service = _service_from_filename(filename, container_map)
        values = stream.get("values", [])
        if not service and values:
            # Container ID not in map — infer from first non-empty log line
            for _ts, sample in values[:5]:
                inferred = _infer_service_from_log(sample)
                if inferred:
                    service = inferred
                    break
        if not service:
            continue  # truly unidentifiable — skip
        for _ts, line in values:
            service_lines[service].append(line)

    return dict(service_lines)


def _cluster_errors(service_lines: dict[str, list[str]]) -> list[dict]:
    """
    Group lines by (service, normalised_fingerprint).
    Returns list of clusters sorted by frequency descending.
    """
    # cluster_key → {service, fingerprint, lines, count}
    clusters: dict[str, dict] = {}
    for service, lines in service_lines.items():
        for line in lines:
            fp = _normalize_line(line)
            if not fp or len(fp) < 10:
                continue
            key = f"{service}::{fp[:80]}"
            if key not in clusters:
                clusters[key] = {
                    "service": service,
                    "fingerprint": fp,
                    "lines": [],
                    "count": 0,
                }
            clusters[key]["count"] += 1
            if len(clusters[key]["lines"]) < _MAX_SAMPLE_LINES:
                clusters[key]["lines"].append(line[:300])

    return sorted(clusters.values(), key=lambda c: c["count"], reverse=True)


def _analyze_cluster(cluster: dict, hours: int) -> dict | None:
    """Call Claude Haiku to analyse a single error cluster. Returns parsed JSON or None."""
    from app.config import get_settings

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    sample = "\n".join(f"  {ln}" for ln in cluster["lines"][:_MAX_SAMPLE_LINES])
    prompt = _ANALYSIS_PROMPT.format(
        service=cluster["service"],
        count=cluster["count"],
        hours=hours,
        sample=sample,
    )

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_ANALYSIS_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Strip markdown code fences if present
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Bug hunter LLM returned non-JSON for %s: %s", cluster["service"], exc)
        return None
    except Exception as exc:
        logger.error("Bug hunter LLM call failed: %s", exc)
        return None


def _create_fix_task(cluster: dict, analysis: dict) -> int | None:
    """
    Create a board task for every non-noise bug. Returns task_id or None.
    Priority is mapped from LLM severity. All tasks use approval_level=1 (auto-execute).
    """
    from app.db import postgres

    if analysis.get("is_noise"):
        return None

    severity = analysis.get("severity", "low")
    priority_num = {"critical": 5, "high": 4, "medium": 3, "low": 2}.get(severity, 2)
    priority_str = "urgent" if severity == "critical" else ("high" if severity == "high" else severity)

    title = f"[BugHunt] {cluster['service']} — {cluster['fingerprint'][:80]}"
    description = (
        f"Detected by Autonomous Bug Hunter\n"
        f"Service: {cluster['service']}\n"
        f"Frequency: {cluster['count']}x\n"
        f"Fingerprint: {cluster['fingerprint']}\n\n"
        f"Root cause: {analysis.get('root_cause', '')}\n"
        f"Affected component: {analysis.get('affected_component', '')}\n"
        f"Proposed fix: {analysis.get('proposed_fix', '')}\n"
        f"Fix complexity: {analysis.get('fix_complexity', '')}\n"
        f"Auto-fixable: {analysis.get('auto_fixable', False)}\n\n"
        f"Sample log lines:\n" + "\n".join(cluster["lines"][:3])
    )
    try:
        row = postgres.execute_one(
            """
            INSERT INTO tasks (title, description, status, priority, priority_num,
                               approval_level, source, tags)
            VALUES (%s, %s, 'pending', %s, %s, 1, 'bug-hunter', %s::jsonb)
            RETURNING id
            """,
            (
                title, description,
                priority_str,
                priority_num,
                json.dumps(["bug-hunter", cluster["service"], severity]),
            ),
        )
        return row["id"]
    except Exception as exc:
        logger.error("Bug hunter failed to create task: %s", exc)
        return None


def _build_slack_report(
    findings: list[dict],
    total_lines: int,
    hours: int,
    tasks_created: list[int],
) -> str:
    """Build the full Slack bug hunt report message."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    services = sorted({f["service"] for f in findings})
    actionable = [f for f in findings if not f["analysis"].get("is_noise") and f["analysis"].get("severity") in ("critical", "high")]
    noise = [f for f in findings if f["analysis"].get("is_noise")]

    header = (
        f"🐛 *Autonomous Bug Hunt — last {hours}h* | _{now}_\n"
        f"Scanned {total_lines} log lines across: {', '.join(services)}\n"
        f"Found *{len(findings)} patterns* · *{len(actionable)} actionable* · {len(noise)} noise\n"
        "─" * 44
    )

    lines = [header]

    # Sort by severity then count
    sorted_findings = sorted(
        findings,
        key=lambda f: (
            _SEVERITY_ORDER.get(f["analysis"].get("severity", "low"), 4),
            -f["count"],
        ),
    )

    for f in sorted_findings:
        a = f["analysis"]
        if a.get("is_noise"):
            continue  # noise printed separately at bottom

        badge = _SEVERITY_BADGE.get(a.get("severity", "low"), "🟢 LOW")
        lines.append(
            f"\n{badge} · *{f['service']}* · {f['count']}x\n"
            f"`{f['fingerprint'][:100]}`\n"
            f"📍 {a.get('affected_component', 'unknown')}\n"
            f"🔍 *Root cause:* {a.get('root_cause', '—')}\n"
            f"🔧 *Fix:* {a.get('proposed_fix', '—')}"
        )
        snippet = a.get("fix_snippet")
        if snippet:
            lines.append(f"```{snippet[:300]}```")

        # Find associated task
        task_id = f.get("task_id")
        if task_id:
            lines.append(f"📋 Fix task *#{task_id}* auto-created")

    # Noise summary
    if noise:
        noise_summary = ", ".join(f"{n['service']} ({n['count']}x)" for n in noise[:5])
        lines.append(f"\n_Noise filtered: {noise_summary}_")

    # Footer
    if tasks_created:
        lines.append(f"\n📋 *{len(tasks_created)} task(s) created* and investigations dispatched — watch for fix PRs in #sentinel-alerts")
    lines.append(f"_Next scheduled hunt in 6h_")

    return "\n".join(lines)


# ── Investigate & fix individual bug ─────────────────────────────────────────

@shared_task(
    name="app.worker.bug_hunter_tasks.investigate_and_fix_bug",
    bind=True,
    max_retries=1,
    default_retry_delay=60,
    soft_time_limit=300,
    time_limit=360,
)
def investigate_and_fix_bug(self, task_id: int, finding: dict) -> dict:
    """
    Investigate and attempt to fix a single bug-hunt finding.
    Called automatically after _create_fix_task for every non-noise bug.

    finding keys: service, fingerprint, count, lines, analysis
      analysis keys: severity, root_cause, affected_component, proposed_fix,
                     fix_snippet, fix_complexity, auto_fixable
    """
    import asyncio as _asyncio

    try:
        return _asyncio.run(_investigate_and_fix_bug(task_id, finding))
    except Exception as exc:
        logger.error("investigate_and_fix_bug failed task=%s: %s", task_id, exc, exc_info=True)
        _mark_bug_task(task_id, "failed")
        return {"error": str(exc)}


def _mark_bug_task(task_id: int, status: str) -> None:
    try:
        from app.db import postgres
        postgres.execute(
            "UPDATE tasks SET status=%s, updated_at=NOW() WHERE id=%s",
            (status, task_id),
        )
    except Exception as exc:
        logger.warning("Could not update bug task %s to %s: %s", task_id, status, exc)


async def _investigate_and_fix_bug(task_id: int, finding: dict) -> dict:
    """
    1. Mark task in_progress + post Slack start notification.
    2. Resolve affected source file(s) from LLM analysis.
    3. Ask LLM (Sonnet) to produce a precise patch given the source + context.
    4. Apply patches → commit on sentinel/bughunt-{task_id} branch → push → open PR.
    5. Post Slack summary. Mark task done / failed.
    """
    import asyncio
    import os
    import re

    import anthropic

    from app.config import get_settings
    from app.integrations.slack_notifier import post_alert_sync

    settings = get_settings()
    analysis = finding.get("analysis", {})
    service = finding.get("service", "unknown")
    fingerprint = finding.get("fingerprint", "")
    count = finding.get("count", 0)
    log_lines = finding.get("lines", [])

    severity = analysis.get("severity", "low")
    root_cause = analysis.get("root_cause", "")
    affected_component = analysis.get("affected_component", "")
    proposed_fix = analysis.get("proposed_fix", "")
    fix_snippet = analysis.get("fix_snippet", "")
    auto_fixable = analysis.get("auto_fixable", False)

    badge = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(severity, "🟢")

    # ── 0. Service → config file map ──────────────────────────────────────────
    # Maps service names to their config files in the repo so the LLM can patch them.
    _SERVICE_CONFIG_MAP: dict[str, list[str]] = {
        "loki":           ["observability/loki-config.yml"],
        "promtail":       ["observability/promtail-config.yml"],
        "prometheus":     ["prometheus/prometheus.yml"],
        "nginx":          ["nginx/nginx.conf"],
        "grafana":        [
            "grafana/provisioning/datasources/prometheus.yaml",
            "grafana/provisioning/dashboards/dashboard.yaml",
        ],
    }

    # ── 1. Mark in_progress + notify ─────────────────────────────────────────
    _mark_bug_task(task_id, "in_progress")
    post_alert_sync(
        f"{badge} *Bug Hunt — Investigating* · task #{task_id}\n"
        f"*Service:* {service} · {count}x\n"
        f"*Pattern:* `{fingerprint[:100]}`\n"
        f"*Root cause:* {root_cause}"
    )

    # ── 2. Resolve source file from affected_component ────────────────────────
    _CODE_ROOT = "/root/sentinel-workspace" if os.path.isdir("/root/sentinel-workspace") else "/app"

    # affected_component may be "app/skills/foo.py:42" or "app.skills.foo:method"
    candidate_files: list[str] = []

    # Pattern: path/to/file.py[:line]
    path_match = re.search(r"(app/[\w/]+\.py)", affected_component)
    if path_match:
        candidate_files.append(path_match.group(1))

    # Pattern: app.module.submodule → app/module/submodule.py
    mod_match = re.search(r"(app(?:\.\w+)+)", affected_component)
    if mod_match and not candidate_files:
        mod_path = mod_match.group(1).replace(".", "/") + ".py"
        candidate_files.append(mod_path)

    # Check service config map for infrastructure services
    if not candidate_files and service in _SERVICE_CONFIG_MAP:
        candidate_files.extend(_SERVICE_CONFIG_MAP[service])

    # Grep codebase for the service name if no file found yet
    if not candidate_files:
        try:
            result = subprocess.run(
                ["grep", "-rl", service.replace("-", "_"), f"{_CODE_ROOT}/app", "--include=*.py"],
                capture_output=True, text=True, timeout=10,
            )
            for p in result.stdout.strip().splitlines()[:3]:
                rel = p.replace(f"{_CODE_ROOT}/", "")
                if rel not in candidate_files:
                    candidate_files.append(rel)
        except Exception:
            pass

    # ── 3. Read source files ──────────────────────────────────────────────────
    file_context = ""
    for fname in candidate_files[:3]:
        fpath = f"{_CODE_ROOT}/{fname}"
        try:
            with open(fpath) as fh:
                content = fh.read()
            excerpt = content[:4000] + ("\n... [truncated]" if len(content) > 4000 else "")
            file_context += f"\n\n=== {fname} ===\n{excerpt}"
        except Exception as exc:
            logger.warning("Could not read %s: %s", fname, exc)

    # ── 4. LLM patch plan ─────────────────────────────────────────────────────
    fix_plan: dict = {
        "fixable": False,
        "patches": [],
        "commit_message": f"fix(bughunt): {service} — {fingerprint[:60]}",
        "summary": "LLM analysis unavailable",
    }

    if auto_fixable or analysis.get("fix_complexity") in ("simple", "medium"):
        sample_logs = "\n".join(f"  {l}" for l in log_lines[:6])
        files_list = "\n".join(f"  - {f}" for f in candidate_files) if candidate_files else "  (none identified)"
        prompt = (
            f"You are an expert SRE fixing a recurring production bug.\n\n"
            f"Service: {service}\n"
            f"Error pattern ({count}x):\n{sample_logs}\n\n"
            f"Root cause: {root_cause}\n"
            f"Affected component: {affected_component}\n"
            f"Proposed fix: {proposed_fix}\n"
            + (f"Fix snippet hint: {fix_snippet}\n" if fix_snippet else "")
            + f"\nSource files available for patching:\n{files_list}\n"
            + (file_context if file_context else "")
            + "\n\nProduce a JSON fix plan. Respond with ONLY valid JSON, no markdown:\n"
            "{\n"
            '  "fixable": true/false,\n'
            '  "patches": [\n'
            '    {"file": "path/to/file.py", "old": "exact verbatim text to replace", "new": "replacement text"}\n'
            "  ],\n"
            '  "commit_message": "fix(service): what was changed",\n'
            '  "summary": "human-readable description of the fix"\n'
            "}\n\n"
            "Rules:\n"
            "- Each 'old' must be EXACT verbatim text from the file shown above\n"
            "- Only patch files listed in 'Source files available for patching'\n"
            "- Files may be Python, YAML, nginx conf, JSON, or any other format — patch them as-is\n"
            "- fixable=false if the root cause is purely environmental (external service outage, credentials, networking)\n"
            "- fixable=false if no source files were provided above\n"
            "- If already fixed in the source, set fixable=false and explain in summary"
        )

        try:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            response = await asyncio.to_thread(
                client.messages.create,
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            # Extract first complete JSON object
            brace_depth = 0
            json_start = raw.find("{")
            json_end = -1
            if json_start != -1:
                for i, ch in enumerate(raw[json_start:], json_start):
                    if ch == "{":
                        brace_depth += 1
                    elif ch == "}":
                        brace_depth -= 1
                        if brace_depth == 0:
                            json_end = i + 1
                            break
            if json_end != -1:
                raw = raw[json_start:json_end]
            fix_plan = json.loads(raw)
        except Exception as exc:
            logger.warning("Bug hunter LLM patch plan failed task=%s: %s", task_id, exc)
            fix_plan["summary"] = f"LLM patch plan failed: {exc}"
    else:
        fix_plan["summary"] = (
            f"Bug documented. Fix complexity={analysis.get('fix_complexity','?')} "
            f"and auto_fixable={auto_fixable} — manual review recommended."
        )

    # ── 5. Apply patches → commit → push → PR ────────────────────────────────
    patches_applied: list[str] = []
    patch_errors: list[str] = []
    pr_url: str = ""

    if fix_plan.get("fixable") and fix_plan.get("patches"):
        try:
            from app.integrations.repo import RepoClient

            repo = RepoClient()
            if repo.is_configured():
                await repo.ensure_repo()
                branch = f"sentinel/bughunt-{task_id}"
                await repo.create_branch(branch)

                for patch in fix_plan["patches"]:
                    try:
                        await repo.patch_file(patch["file"], patch["old"], patch["new"])
                        patches_applied.append(patch["file"])
                    except Exception as exc:
                        patch_errors.append(f"{patch['file']}: {exc}")
                        logger.warning("Patch failed for %s: %s", patch["file"], exc)

                if patches_applied:
                    commit_msg = fix_plan.get("commit_message", f"fix(bughunt): task #{task_id}")
                    await repo.commit(
                        f"{commit_msg}\n\nAuto-fixed by Sentinel Bug Hunter (task #{task_id})",
                        files=patches_applied,
                    )
                    pr_result = await repo.push(
                        pr_title=f"fix(bughunt): {service} — {fingerprint[:70]}",
                        pr_body=(
                            f"**Service:** {service} | **Severity:** {severity} | **Occurrences:** {count}x\n\n"
                            f"**Root cause:** {root_cause}\n\n"
                            f"**Files changed:** {', '.join(f'`{f}`' for f in patches_applied)}\n\n"
                            f"_{fix_plan.get('summary', '')}_\n\n"
                            f"---\n*Auto-generated by Sentinel Bug Hunter (task #{task_id}). Review carefully before merging.*"
                        ),
                    )
                    pr_match = re.search(r"(https://github\.com/\S+)", pr_result or "")
                    pr_url = pr_match.group(1) if pr_match else pr_result
            else:
                patch_errors.append("Repo not configured")
        except Exception as exc:
            patch_errors.append(f"Repo operation failed: {exc}")
            logger.error("Bug hunter repo patch failed task=%s: %s", task_id, exc, exc_info=True)

    # ── 6. Post Slack result + mark done ─────────────────────────────────────
    if patches_applied and pr_url:
        slack_msg = (
            f"{badge} *Bug Hunt — Fix Pushed* · task #{task_id}\n"
            f"*Service:* {service} · {count}x | *Severity:* {severity}\n"
            f"*Root cause:* {root_cause}\n"
            f"*Files patched:* {', '.join(f'`{f}`' for f in patches_applied)}\n"
            f"*PR:* {pr_url}"
        )
        _mark_bug_task(task_id, "done")
    elif patches_applied:
        slack_msg = (
            f"{badge} *Bug Hunt — Patched (no PR URL)* · task #{task_id}\n"
            f"*Service:* {service} | *Files:* {', '.join(patches_applied)}"
        )
        _mark_bug_task(task_id, "done")
    elif patch_errors:
        slack_msg = (
            f"{badge} *Bug Hunt — Patch Failed* · task #{task_id}\n"
            f"*Service:* {service}\n"
            f"*Errors:* {'; '.join(patch_errors[:2])}\n"
            f"*Summary:* {fix_plan.get('summary', '')}"
        )
        _mark_bug_task(task_id, "failed")
    else:
        slack_msg = (
            f"{badge} *Bug Hunt — Needs Manual Fix* · task #{task_id}\n"
            f"*Service:* {service} · {count}x | *Severity:* {severity}\n"
            f"*Root cause:* {root_cause}\n"
            f"*Why not auto-patched:* {fix_plan.get('summary', 'See task for details')}\n"
            f"_Task remains open on the board for manual review._"
        )
        _mark_bug_task(task_id, "manual_review")

    post_alert_sync(slack_msg)

    return {
        "task_id": task_id,
        "service": service,
        "patches_applied": patches_applied,
        "patch_errors": patch_errors,
        "pr_url": pr_url,
        "summary": fix_plan.get("summary", ""),
    }


# ── Main Celery task ──────────────────────────────────────────────────────────

@shared_task(
    name="app.worker.bug_hunter_tasks.run_bug_hunt",
    bind=False,
    max_retries=1,
    default_retry_delay=120,
    soft_time_limit=300,
    time_limit=360,
)
def run_bug_hunt(hours: int = 24) -> dict:
    """
    Autonomous Bug Hunt — scan Loki logs, cluster errors, LLM-analyse top clusters,
    post Slack report, create fix tasks.
    """
    from app.integrations.slack_notifier import post_alert_sync

    logger.info("Bug hunter starting — scanning last %sh", hours)

    # ── 1. Fetch ──────────────────────────────────────────────────────────────
    service_lines = _fetch_loki_errors(hours)
    if not service_lines:
        logger.info("Bug hunter: no error lines found in Loki")
        return {"status": "clean", "hours": hours}

    total_lines = sum(len(v) for v in service_lines.values())
    logger.info("Bug hunter: %d lines across %d services", total_lines, len(service_lines))

    # ── 2. Cluster ────────────────────────────────────────────────────────────
    clusters = _cluster_errors(service_lines)
    top_clusters = clusters[:_MAX_CLUSTERS]

    if not top_clusters:
        logger.info("Bug hunter: no clusters formed")
        return {"status": "clean", "hours": hours}

    # ── 3. Analyse ────────────────────────────────────────────────────────────
    findings: list[dict] = []
    tasks_created: list[int] = []

    for cluster in top_clusters[:_MAX_ANALYZE]:
        logger.info(
            "Bug hunter: analysing %s · %s (%dx)",
            cluster["service"], cluster["fingerprint"][:60], cluster["count"],
        )
        analysis = _analyze_cluster(cluster, hours)
        if not analysis:
            continue

        finding = {**cluster, "analysis": analysis}

        # ── 4. Create task for every non-noise bug ────────────────────────
        task_id = _create_fix_task(cluster, analysis)
        if task_id:
            finding["task_id"] = task_id
            tasks_created.append(task_id)

        findings.append(finding)

    if not findings:
        logger.info("Bug hunter: LLM analysis produced no findings")
        return {"status": "no_findings", "hours": hours, "total_lines": total_lines}

    # ── 5. Post Slack report ──────────────────────────────────────────────────
    report = _build_slack_report(findings, total_lines, hours, tasks_created)
    post_alert_sync(report)

    # ── 6. Dispatch investigate_and_fix for every created task ────────────────
    dispatched: list[int] = []
    for finding in findings:
        tid = finding.get("task_id")
        if tid is None:
            continue
        try:
            investigate_and_fix_bug.apply_async(
                kwargs={"task_id": tid, "finding": {
                    "service":     finding["service"],
                    "fingerprint": finding["fingerprint"],
                    "count":       finding["count"],
                    "lines":       finding["lines"],
                    "analysis":    finding["analysis"],
                }},
                queue="tasks_general",
                countdown=2,  # slight stagger so report posts first
            )
            dispatched.append(tid)
        except Exception as exc:
            logger.error("Could not dispatch investigate_and_fix_bug for task %s: %s", tid, exc)

    logger.info(
        "Bug hunt complete: %d findings, %d tasks created, %d investigations dispatched",
        len(findings), len(tasks_created), len(dispatched),
    )
    return {
        "status": "complete",
        "hours": hours,
        "total_lines": total_lines,
        "clusters_found": len(clusters),
        "findings": len(findings),
        "tasks_created": tasks_created,
        "investigations_dispatched": dispatched,
    }
