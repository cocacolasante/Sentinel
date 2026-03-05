"""
Architecture Evolution Advisor — analyses your system architecture (or Sentinel
itself) and produces a prioritised improvement report delivered via Slack and email.

Flow:
  1. Gather live context if analysing Sentinel (docker services, compose, nginx)
  2. Call Claude Sonnet with a structured architecture analysis prompt
  3. Post to #sentinel-research Slack channel
  4. Send full HTML email to owner
  5. Return executive summary to dispatcher

Trigger examples:
  "analyse sentinel architecture"
  "review my architecture for scalability"
  "what are the bottlenecks in my system?"
  "architecture advice for <description>"
"""

from __future__ import annotations

import logging
import re
import subprocess

import anthropic

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """You are a principal software architect and SRE with 20+ years experience.
Analyse the provided system architecture description (and any live context) and produce
a comprehensive, actionable Architecture Evolution Report.

Structure your report with EXACTLY these markdown sections:

## Architecture Overview
(Describe the current state: components, data flows, key dependencies)

## Bottleneck Analysis
(Where will the system break under load? Quote specific numbers where estimable.
 E.g. "Single Postgres instance will saturate at ~500 concurrent writes")

## Reliability & Single Points of Failure
(What fails if one component goes down? What has no redundancy?)

## Security & Exposure Assessment
(Auth gaps, exposed surfaces, secrets handling, network attack surface)

## Performance Optimisations
(Caching opportunities, async processing, query tuning, CDN, connection pooling)

## Scalability Roadmap
(What to do at 2x, 10x, and 100x current load — concrete steps)

## Quick Wins
(High-impact, low-effort changes — ordered by ROI. Each should take < 1 day)

## Prioritised Action Plan
(Numbered list: P1 Critical → P2 High → P3 Medium. Each item: action + rationale + effort estimate)

## Technology Recommendations
(Specific tools, libraries, or services to adopt or replace, with brief justification)

Rules:
- Be specific. Reference actual component names, file paths, and tech visible in the context.
- Avoid generic advice. Every recommendation must be tied to the actual architecture.
- Quantify wherever possible (latency, throughput, memory, cost).
- Aim for 900–1400 words total."""

# ── Context gathering ─────────────────────────────────────────────────────────

def _gather_sentinel_context() -> str:
    """Collect live architecture context from the running Sentinel deployment."""
    sections: list[str] = []

    # Running containers + resource usage
    try:
        out = subprocess.run(
            ["docker", "ps", "--format",
             "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        sections.append(f"=== Running Containers ===\n{out}")
    except Exception as exc:
        sections.append(f"=== Running Containers ===\nUnavailable: {exc}")

    # Docker stats snapshot (no-stream)
    try:
        stats = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        sections.append(f"=== Resource Usage (live) ===\n{stats}")
    except Exception as exc:
        sections.append(f"=== Resource Usage ===\nUnavailable: {exc}")

    # docker-compose service definitions (stripped to essentials)
    try:
        with open("/root/sentinel-workspace/docker-compose.yml") as f:
            compose = f.read()
        # Truncate to 4000 chars to avoid token bloat
        if len(compose) > 4000:
            compose = compose[:4000] + "\n... (truncated)"
        sections.append(f"=== docker-compose.yml ===\n{compose}")
    except Exception as exc:
        sections.append(f"=== docker-compose.yml ===\nUnavailable: {exc}")

    # nginx routing summary
    try:
        with open("/root/sentinel-workspace/nginx/nginx.conf") as f:
            nginx = f.read()
        if len(nginx) > 2000:
            nginx = nginx[:2000] + "\n... (truncated)"
        sections.append(f"=== nginx.conf ===\n{nginx}")
    except Exception as exc:
        sections.append(f"=== nginx.conf ===\nUnavailable: {exc}")

    # Key config (env vars present — not values)
    try:
        from app.config import get_settings
        s = get_settings()
        configured = []
        for field in [
            "anthropic_api_key", "openai_api_key", "redis_host", "postgres_host",
            "qdrant_host", "sentry_dsn", "slack_bot_token", "github_token",
            "ionos_api_key", "google_credentials_json",
        ]:
            val = getattr(s, field, None)
            configured.append(f"  {field}: {'✓ set' if val else '✗ not set'}")
        sections.append("=== Integrations configured ===\n" + "\n".join(configured))
    except Exception:
        pass

    return "\n\n".join(sections)


# ── LLM call ─────────────────────────────────────────────────────────────────

async def _generate_report(target: str, focus: str, context: str) -> str:
    """Call Claude Sonnet to produce the architecture analysis report."""
    from app.config import get_settings

    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_parts = [f"System to analyse: {target}"]
    if focus:
        user_parts.append(f"Focus area: {focus}")
    if context:
        user_parts.append(f"\n--- Live System Context ---\n{context}")

    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=_SYSTEM,
        messages=[{"role": "user", "content": "\n\n".join(user_parts)}],
    )
    return msg.content[0].text


# ── Delivery ─────────────────────────────────────────────────────────────────

def _truncate_for_slack(text: str, limit: int = 2900) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n_…(truncated — full report in email)_"


async def _post_to_slack(target: str, report: str, channel: str) -> bool:
    from app.integrations.slack_notifier import post_alert

    header = f"*🏛️ Architecture Evolution Report: {target}*\n{'─' * 44}\n"
    body = _truncate_for_slack(report)
    return await post_alert(header + body, channel=channel)


def _md_to_html(report: str) -> str:
    """Convert markdown report to styled HTML for email."""
    html = report
    html = re.sub(
        r"^## (.+)$",
        r"<h2 style='color:#1a1a2e;margin-top:1.5em;border-bottom:1px solid #ddd;padding-bottom:0.3em;'>\1</h2>",
        html, flags=re.MULTILINE,
    )
    html = re.sub(r"^### (.+)$", r"<h3 style='color:#333;'>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
    html = re.sub(r"`(.+?)`", r"<code style='background:#f4f4f4;padding:2px 4px;border-radius:3px;'>\1</code>", html)

    lines = html.split("\n")
    out_lines: list[str] = []
    in_list = False
    in_ol = False
    for line in lines:
        if re.match(r"^\d+\. ", line):
            if not in_ol:
                if in_list:
                    out_lines.append("</ul>")
                    in_list = False
                out_lines.append("<ol>")
                in_ol = True
            item_text = re.sub(r'^\d+\. ', '', line)
            out_lines.append(f"<li>{item_text}</li>")
        elif re.match(r"^[-*] ", line):
            if in_ol:
                out_lines.append("</ol>")
                in_ol = False
            if not in_list:
                out_lines.append("<ul>")
                in_list = True
            out_lines.append(f"<li>{line[2:]}</li>")
        else:
            if in_list:
                out_lines.append("</ul>")
                in_list = False
            if in_ol:
                out_lines.append("</ol>")
                in_ol = False
            out_lines.append(line)
    if in_list:
        out_lines.append("</ul>")
    if in_ol:
        out_lines.append("</ol>")

    html = "\n".join(out_lines)
    paras = re.split(r"\n{2,}", html)
    body_html = ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if re.match(r"^<(h[123]|ul|ol|li)", para):
            body_html += para + "\n"
        else:
            body_html += f"<p>{para}</p>\n"
    return body_html


async def _send_email(target: str, report: str, to: str) -> bool:
    try:
        from app.integrations.gmail import get_gmail_client

        client = get_gmail_client()
        if not client.is_configured():
            logger.warning("ArchAdvisorSkill: Gmail not configured — skipping email")
            return False

        body_html = _md_to_html(report)
        full_html = (
            "<!DOCTYPE html><html><body style='font-family:Georgia,serif;"
            "max-width:860px;margin:40px auto;line-height:1.75;color:#222;'>"
            f"<h1 style='color:#1a1a2e;'>🏛️ Architecture Evolution Report</h1>"
            f"<p style='color:#555;font-size:1.1em;'>System: <strong>{target}</strong></p>"
            "<hr style='border:1px solid #ddd;margin:20px 0;'>"
            f"{body_html}"
            "<hr style='border:1px solid #ddd;margin:20px 0;'>"
            "<p style='color:#888;font-size:0.85em;'>Generated by Sentinel AI · Architecture Evolution Advisor</p>"
            "</body></html>"
        )

        await client.send_email(
            to=to,
            subject=f"Architecture Report: {target}",
            body=full_html,
            html=True,
        )
        return True
    except Exception as exc:
        logger.error("ArchAdvisorSkill email failed: %s", exc)
        return False


# ── Skill class ───────────────────────────────────────────────────────────────

class ArchAdvisorSkill(BaseSkill):
    name = "arch_advisor"
    description = (
        "Architecture Evolution Advisor — analyses your system (or Sentinel itself) "
        "and delivers a prioritised improvement report: bottlenecks, reliability risks, "
        "security gaps, quick wins, and a concrete action plan. Report posted to "
        "#sentinel-research and emailed to owner."
    )
    trigger_intents = ["arch_advisor"]
    approval_category = ApprovalCategory.STANDARD
    config_vars = ["ANTHROPIC_API_KEY"]

    def is_available(self) -> bool:
        from app.config import get_settings
        return bool(get_settings().anthropic_api_key)

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.config import get_settings

        settings = get_settings()

        target = params.get("target", "").strip() or "Sentinel AI platform"
        focus = params.get("focus", "").strip()
        description = params.get("description", "").strip()
        to_email = params.get("email") or settings.owner_email
        channel = settings.slack_research_channel

        # Gather live context when analysing Sentinel itself
        is_self_analysis = any(
            kw in target.lower()
            for kw in ("sentinel", "this system", "my system", "our system", "self")
        )
        context = ""
        if is_self_analysis:
            logger.info("ArchAdvisorSkill: gathering live Sentinel context")
            context = _gather_sentinel_context()
        elif description:
            context = f"System description provided by user:\n{description}"

        # Build the analysis target string
        analysis_target = target
        if description and not is_self_analysis:
            analysis_target = f"{target}\n\n{description}"

        # ── Generate ─────────────────────────────────────────────────────────
        try:
            report = await _generate_report(analysis_target, focus, context)
        except Exception as exc:
            logger.error("ArchAdvisorSkill LLM call failed: %s", exc)
            return SkillResult(
                context_data=f"[ArchAdvisor failed: {exc}]",
                skill_name=self.name,
            )

        # ── Deliver ──────────────────────────────────────────────────────────
        slack_ok = await _post_to_slack(target, report, channel)
        email_ok = False
        if to_email:
            email_ok = await _send_email(target, report, to_email)

        delivered: list[str] = []
        if slack_ok:
            delivered.append(f"Slack #{channel}")
        if email_ok:
            delivered.append(f"email to {to_email}")
        if not delivered:
            delivered.append("(no delivery — check Slack token / Gmail config)")

        word_count = len(report.split())

        # Extract the Quick Wins section for the inline reply
        quick_wins = ""
        if "## Quick Wins" in report:
            qw_section = report.split("## Quick Wins")[1].split("##")[0].strip()
            quick_wins = qw_section[:600]

        # Extract bottleneck section
        bottlenecks = ""
        if "## Bottleneck Analysis" in report:
            bn_section = report.split("## Bottleneck Analysis")[1].split("##")[0].strip()
            bottlenecks = bn_section[:400]

        context_data = (
            f"🏛️ **Architecture Evolution Report** generated for **{target}** "
            f"({word_count} words, 9 sections).\n"
            f"Delivered via: {', '.join(delivered)}\n\n"
        )
        if bottlenecks:
            context_data += f"**Top Bottlenecks:**\n{bottlenecks}\n\n"
        if quick_wins:
            context_data += f"**Quick Wins:**\n{quick_wins}"

        return SkillResult(context_data=context_data, skill_name=self.name)
