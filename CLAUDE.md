# Sentinel — Identity & Architecture Guide

## What is Sentinel?

Sentinel is Anthony's personalized AI assistant platform built by CSuite Code.  It is a
FastAPI brain backed by Celery workers, Redis, PostgreSQL, Grafana, and a Slack bot.

All natural-language requests arrive via three interfaces:
- **Slack bot** (`app/router/slack.py`) — Socket Mode, real-time
- **CLI** (`brain.py`) — REPL and one-shot commands
- **REST API** (`POST /api/v1/chat`) — for programmatic access and n8n workflows

Every interface goes through the same `Dispatcher` → `IntentClassifier` → `SkillRegistry`
pipeline so behaviour is identical across all three.

---

## Three-Path Architecture

```
User message
     │
     ▼
IntentClassifier (Haiku) ──► intent + params
     │
     ▼
SkillRegistry.dispatch()
     │
     ├─► Skill.execute() ──────────────────────────────────────────────────────┐
     │        │                                                                 │
     │        ▼                                                                 │
     │   SkillResult.context_data (live data injected into LLM prompt)         │
     │                                                                          │
     ▼                                                                          │
LLMRouter (Sonnet) ◄─────── augmented prompt (context + memory + TELOS) ◄─────┘
     │
     ▼
DispatchResult.reply → Slack / CLI / REST
```

---

## Core Principles

1. **Act now, never defer** — if you need data, use a skill to fetch it immediately.
2. **Never output shell commands** — use `server_shell` skill, not bash in response text.
3. **Always open a PR** — self-modification goes to a feature branch + PR, never main.
4. **Non-destructive by default** — prefer `patch_file` over `write_file`.
5. **Structured logging** — milestones are written to `ai_milestones` DB + Slack.

---

## Skill Routing Quick Reference

| Trigger phrase | Intent | Skill |
|---|---|---|
| "read my email" | `gmail_read` | GmailReadSkill |
| "send email to X" | `gmail_send` | GmailSendSkill |
| "what's on my calendar" | `calendar_read` | CalendarReadSkill |
| "create event X" | `calendar_write` | CalendarWriteSkill |
| "list github issues" | `github_read` | GitHubReadSkill |
| "list tasks / show tasks" | `task_read` | TaskReadSkill |
| "run pipeline for X" | `se_workflow` | SEWorkflowSkill |
| "build me a X" | `se_new_project` | SEWorkflowSkill |
| "brainstorm X" | `se_brainstorm` | SEWorkflowSkill |

---

## SE Workflow Pipeline

The SE Workflow Skill provides a **5-phase autonomous pipeline** powered by Claude Opus
as an expert subagent at each phase.

### Two Modes

**Mode 1 — Sentinel self-work** (`project_type: sentinel`)
- Task artefacts: `/root/sentinel-workspace/se-tasks/{slug}/`
- Git commits to: `/root/sentinel-workspace` (the live Sentinel repo)
- Use for: new skills, bug fixes, refactors, architectural improvements to Sentinel itself

**Mode 2 — New external project** (`project_type: project`)
- Task artefacts: `/root/projects/{slug}/`
- Git repo: `/root/projects/{slug}/` (own git repo, optional GitHub remote)
- Use for: client websites, APIs, SaaS apps, microservices built for external use

### Pipeline Phases

| Phase | Command | Output files |
|---|---|---|
| Brainstorm | `/brainstorm X` or `se_brainstorm` | `brainstorm.md`, `sprint.md` |
| Spec | `/spec-task X` or `se_spec` | `spec.md` |
| Plan | `/plan-task X` or `se_plan` | `plan.md`, `decisions.md`, `implementation-notes.md`, `status.md` |
| Implement | `/implement X` or `se_implement` | `implementation.md`, `code/{files}`, `status.md` |
| Review | `/review X` or `se_review` | `audit.md` (verdict: APPROVED / NEEDS WORK / BLOCKED) |
| Full pipeline | `se workflow X` | All of the above |
| New project | `build me a X` | git init + README + all phases |

### Example Conversations

**Sentinel self-improvement:**
```
You:      brainstorm adding a rate-limit dashboard to Sentinel
Sentinel: [se_brainstorm] → brainstorm.md + sprint.md committed to sentinel-workspace/se-tasks/...

You:      spec it out
Sentinel: [se_spec] → spec.md committed

You:      full pipeline
Sentinel: [se_workflow] → all 5 phases, final audit.md with VERDICT: APPROVED
```

**New external project:**
```
You:      build me a landing page for ClientCo — static HTML, contact form, modern design
Sentinel: [se_new_project] → /root/projects/clientco-landing-page/ created
          git init, README written, full 5-phase pipeline runs
          Final audit.md with VERDICT and deployment notes
```

### Expert Subagent Behaviour

Each phase sends a focused system prompt to Claude Opus:
- **Brainstorm** — software architect generating ideas, risks, and a sprint plan
- **Spec** — product manager writing acceptance criteria, data models, API contracts
- **Plan** — senior architect producing numbered steps and ADRs
- **Implement** — senior engineer writing production-ready, testable code
- **Review** — principal engineer auditing correctness, security, and completeness

---

## Approval Levels

| Level | Behaviour |
|---|---|
| 1 (default) | Auto-queued by Celery scan within 60 seconds |
| 2 | Owner DM'd; waits for approval |
| 3 | Requires explicit written confirmation |

---

## Response Style

- Direct and concise by default; only elaborate when asked
- Markdown formatting for structured output
- No trailing summaries — Anthony can read the diff
- Code changes always go via feature branch → PR; never directly to main
