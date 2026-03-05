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
  6. Post a structured Slack report to brain-alerts
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
    Create a board task only for auto-fixable bugs. Returns task_id or None.
    Noise bugs and non-auto-fixable bugs are skipped — no task is created.
    """
    from app.db import postgres

    if analysis.get("is_noise"):
        return None

    auto_fixable = bool(analysis.get("auto_fixable"))
    if not auto_fixable:
        return None

    severity = analysis.get("severity", "low")
    approval_level = 1

    title = f"[BugHunt] {cluster['service']} — {cluster['fingerprint'][:80]}"
    description = (
        f"Detected by Autonomous Bug Hunter\n"
        f"Service: {cluster['service']}\n"
        f"Frequency: {cluster['count']}x\n"
        f"Fingerprint: {cluster['fingerprint']}\n\n"
        f"Root cause: {analysis.get('root_cause', '')}\n"
        f"Affected component: {analysis.get('affected_component', '')}\n"
        f"Proposed fix: {analysis.get('proposed_fix', '')}\n"
        f"Fix complexity: {analysis.get('fix_complexity', '')}\n\n"
        f"Sample log lines:\n" + "\n".join(cluster["lines"][:3])
    )
    try:
        priority_num = {"critical": 5, "high": 4, "medium": 3, "low": 2}.get(severity, 2)
        priority_str = "high" if severity == "critical" else severity
        row = postgres.execute_one(
            """
            INSERT INTO tasks (title, description, status, priority, priority_num,
                               approval_level, source, tags)
            VALUES (%s, %s, 'pending', %s, %s, %s, 'bug-hunter', %s::jsonb)
            RETURNING id
            """,
            (
                title, description,
                priority_str,
                priority_num,
                approval_level,
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
    task_note = f" · {len(tasks_created)} fix task(s) queued" if tasks_created else ""
    lines.append(f"\n_Next scheduled hunt in 6h{task_note}_")

    return "\n".join(lines)


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

        # ── 4. Create fix task if warranted ───────────────────────────────
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

    logger.info(
        "Bug hunt complete: %d findings, %d tasks created",
        len(findings), len(tasks_created),
    )
    return {
        "status": "complete",
        "hours": hours,
        "total_lines": total_lines,
        "clusters_found": len(clusters),
        "findings": len(findings),
        "tasks_created": tasks_created,
    }
