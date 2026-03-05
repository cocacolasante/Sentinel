# Sentinel Skills Reference

> **37 skills** across 9 categories. All triggered via natural language in Slack, the CLI (`brain chat`), or the REST API (`POST /api/v1/chat`).

All skills are triggered via natural language in Slack, the CLI (`brain chat`), or the REST API (`POST /api/v1/chat`). The intent classifier maps your message to the right skill automatically.

---

## Communication

### Gmail — Read
Read, search, or manage your inbox.

**Trigger phrases:**
- "check my email", "do I have any emails", "read my inbox"
- "show unread emails", "search emails for X", "find emails from X"
- "read email from Sarah", "show me my latest emails"
- "mark email X as read", "show email labels"

---

### Gmail — Send
Compose and send emails.

**Trigger phrases:**
- "send an email to X", "email X about Y", "compose email to X"
- "draft an email to X saying Y", "write an email to X"

---

### Gmail — Reply
Reply to an existing email thread.

**Trigger phrases:**
- "reply to that email", "reply to X's email saying Y"
- "respond to email ID X with Y"

---

### WhatsApp — Read
Check recent WhatsApp messages.

**Trigger phrases:**
- "read my WhatsApp", "check WhatsApp messages", "show WhatsApp from X"
- "any WhatsApp messages", "WhatsApp inbox"

**Requires:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`

---

### WhatsApp — Send
Send a WhatsApp message.

**Trigger phrases:**
- "WhatsApp X saying Y", "send WhatsApp to +1234 saying Y"
- "text X on WhatsApp", "message X on WhatsApp"

**Requires:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`

---

## Calendar & Contacts

### Calendar — Read
Check your schedule and availability.

**Trigger phrases:**
- "what's on my calendar today", "what do I have tomorrow"
- "show my schedule this week", "am I free on Thursday"
- "what events do I have next week", "check my availability"

**Requires:** `GOOGLE_REFRESH_TOKEN`

---

### Calendar — Write
Create, update, or cancel calendar events.

**Trigger phrases:**
- "schedule a meeting with X on Thursday at 2pm"
- "create a calendar event: X on YYYY-MM-DD at HH:MM"
- "add an event called X to my calendar"
- "cancel my meeting on Thursday", "reschedule X to 3pm"
- "block off Friday afternoon", "set up a 1-hour call with X"

**Requires:** `GOOGLE_REFRESH_TOKEN`

---

### Contacts — Read
Search and look up contacts.

**Trigger phrases:**
- "find contact Laura", "look up X's email", "search contacts for X"
- "what's X's phone number", "show me contact X"

**Requires:** `GOOGLE_REFRESH_TOKEN`

---

### Contacts — Write
Add, update, or delete contacts.

**Trigger phrases:**
- "add contact X with email Y", "save contact X, phone +1234"
- "update X's email to Y", "delete contact X"
- "new contact: name X, company Y, email Z"

**Requires:** `GOOGLE_REFRESH_TOKEN`

---

## GitHub & Code

### GitHub — Read
Check issues, pull requests, and notifications.

**Trigger phrases:**
- "show open issues on repo X", "any new PRs on X"
- "check GitHub notifications", "list issues in owner/repo"
- "what PRs are open", "show me the latest issues"

**Requires:** `GITHUB_TOKEN`

---

### GitHub — Write
Create issues, comment on PRs, close issues.

**Trigger phrases:**
- "create a GitHub issue: title X, body Y"
- "open an issue on repo X about Y"
- "comment on PR #N: Y", "close issue #N"

**Requires:** `GITHUB_TOKEN`

---

### Repo — Read
Read Sentinel's own codebase and files.

**Trigger phrases:**
- "show me X file", "read app/main.py", "cat X"
- "git status", "what's changed", "show me the diff"
- "list files in X", "review the source", "git log"
- "show git status", "any uncommitted changes"

---

### Repo — Write
Edit or create files in Sentinel's codebase.

**Trigger phrases:**
- "edit file X to do Y", "update app/config.py"
- "write a new file at path X with content Y"
- "patch file X: change old to new", "improve file X"
- "refactor X", "fix the bug in X"

---

### Repo — Commit
Commit and push changes to GitHub.

**Trigger phrases:**
- "commit the changes", "commit all changes with message X"
- "push to GitHub", "git commit and push"
- "commit with message X and push"

---

### Code Change (Full Workflow)
Branch → patch file → commit → push → open PR with auto-merge in one shot.

**Trigger phrases:**
- "update X and deploy", "change X and ship it"
- "fix X and create a PR", "make a code change to X"
- "edit X and open a PR"

---

### Code
Software engineering help — no file edits, pure reasoning.

**Trigger phrases:**
- "write code for X", "help me implement X"
- "review this code", "debug this function"
- "how do I X in Python", "explain this algorithm"
- "help me code X", "write a function that does X"

---

## CI/CD

### CI/CD — Read
Check GitHub Actions workflows and run status.

**Trigger phrases:**
- "list CI/CD workflows", "show GitHub Actions for repo X"
- "what's the status of the latest CI run", "list recent runs"
- "check the pipeline for X", "show workflow runs"

**Requires:** `GITHUB_TOKEN`

---

### CI/CD — Trigger
Manually trigger a GitHub Actions workflow.

**Trigger phrases:**
- "trigger workflow deploy.yml on main", "run CI for repo X"
- "manually trigger X workflow"

**Requires:** `GITHUB_TOKEN`

---

### CI/CD — Debug
Fetch failed CI run logs and suggest or apply fixes.

**Trigger phrases:**
- "why did CI fail", "debug the failing CI run"
- "fix the CI pipeline", "show CI error logs"
- "what broke in the last CI run"

**Requires:** `GITHUB_TOKEN`

---

## Infrastructure (IONOS Cloud)

### IONOS Cloud
Full DCD management: servers, volumes, networks, Kubernetes, SSH, and deploy.

**Trigger phrases by action:**

| Action | Phrases |
|--------|---------|
| List datacenters | "list my datacenters", "show IONOS datacenters" |
| Provision server | "deploy an ubuntu server", "provision a VPS", "spin up a cloud server", "create a CUBE M server" |
| Server status | "list servers in DC X", "what servers do I have" |
| Start/stop/reboot | "start server X", "stop server X", "reboot server X" |
| SSH into server | "SSH into server at IP X", "run command X on server" |
| List images | "list images", "what ubuntu images are available" |
| Volumes | "list volumes", "create a volume", "attach volume X to server Y", "take a snapshot" |
| NICs/LANs | "list NICs", "show network interfaces", "list LANs" |
| Firewall | "list firewall rules", "open port 80 on server X", "add firewall rule" |
| IP Blocks | "list IP blocks", "reserve an IP", "release IP block X" |
| Load balancers | "list load balancers", "create a load balancer" |
| NAT gateways | "list NAT gateways", "create NAT gateway" |
| Kubernetes | "list Kubernetes clusters", "create a k8s cluster", "get kubeconfig for cluster X" |
| Deploy Docker | "deploy Docker container X on server Y" |
| Deploy website | "deploy website to server X", "clone repo and set up Apache", "deploy portfolio" |
| CUBE server | "spin up a cube server", "provision a cube M", "deploy a cube" |

**Requires:** `IONOS_TOKEN` (or `IONOS_USERNAME` + `IONOS_PASSWORD`)

---

### IONOS DNS
Manage DNS zones and records.

**Trigger phrases:**
- "list DNS zones", "show DNS records for domain X"
- "add A record for X pointing to 1.2.3.4"
- "update CNAME for www to X", "delete DNS record X"
- "set MX record for X", "add TXT record"

**Requires:** `IONOS_TOKEN`

---

## Projects (Autonomous Builder)

### Project Builder
Give Sentinel an idea — it creates a GitHub repo, generates code, writes tests, generates a README, deploys a server, and registers everything in the Knowledge Graph.

**Trigger phrases:**
- "build me a REST API for X", "create a project: X"
- "build a React app for X", "make a FastAPI service that does X"
- "scaffold a Node.js project called X"
- "build an app and deploy it", "create and host X"
- "build a ... application", "write a ... service"

**Actions:**
| Intent | Phrases |
|--------|---------|
| Create & build | "build me a ...", "create a project", "make a ... app", "I want to build ..." |
| Build only | "rebuild project X", "re-run the build", "try building again" |
| Deploy | "deploy project X", "spin up a server for X", "host project X on IONOS" |
| Status | "project status", "how is the build going", "is X done building" |
| List | "list my projects", "show projects", "all projects" |

**Tech stack detection:** `python` · `fastapi` · `flask` · `django` · `react` · `nextjs` · `node` · `go` · `rust` · `static`

**Requires:** `ANTHROPIC_API_KEY`; `GITHUB_TOKEN` for GitHub repo creation

---

## Automation

### n8n — Execute
Run a named n8n workflow.

**Trigger phrases:**
- "run workflow X", "trigger n8n workflow X"
- "execute the daily brief workflow"

**Requires:** `N8N_WEBHOOK_URL`, `N8N_API_KEY`

---

### n8n — Manage
List, create, activate, or delete n8n workflows.

**Trigger phrases:**
- "list n8n workflows", "show all workflows"
- "activate workflow X", "deactivate workflow X"
- "create a new n8n workflow called X", "delete workflow X"

**Requires:** `N8N_API_KEY`

---

### Smart Home
Control Home Assistant devices.

**Trigger phrases:**
- "turn on the lights", "turn off living room light"
- "what's the thermostat set to", "set temperature to 72"
- "lock the front door", "toggle the kitchen light"
- "is the X on", "status of X device"

**Requires:** `HOME_ASSISTANT_URL`, `HOME_ASSISTANT_TOKEN`

---

## Error Monitoring

### Sentry — Read
List and inspect Sentry error issues.

**Trigger phrases:**
- "show Sentry errors", "list recent errors"
- "what errors are in Sentry", "check Sentry issues"
- "show issue ID X in Sentry", "recent unresolved errors"

**Requires:** `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`

---

### Sentry — Manage
Resolve, ignore, assign, or comment on Sentry issues.

**Trigger phrases:**
- "resolve Sentry issue X", "ignore error X in Sentry"
- "assign issue X to user@co.com", "comment on Sentry issue X"
- "mark error X as resolved"

**Requires:** `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`

---

### Bug Hunter
Scan logs and Sentry for recent errors, trace root causes, and propose fixes.

**Trigger phrases:**
- "scan logs for errors", "hunt for bugs", "analyze errors"
- "bug hunt", "check for errors in the last 24h"
- "SRE scan", "what errors are happening", "find bugs"
- "scan for issues", "analyze errors from last Xh"

**Requires:** `ANTHROPIC_API_KEY`

---

## Tasks & Research

### Task — Create
Create tracked tasks on the Sentinel board.

**Trigger phrases:**
- "create a task: X", "add a task to track X"
- "new task: title X, priority 3", "log a task for X"
- "remember to do X", "track this: X"
- "create 3 tasks: X, Y, Z" _(creates multiple at once)_

---

### Task — Read
List and view tasks.

**Trigger phrases:**
- "list my tasks", "show open tasks", "what tasks are pending"
- "show tasks in progress", "view my task board"
- "show high priority tasks", "what's on my board"

---

### Task — Update
Update task status, priority, or details.

**Trigger phrases:**
- "mark task #N done", "complete task #N"
- "update task #N to in_progress", "change priority of task #N to 2"
- "close task #N", "set task #N status to cancelled"

---

### Research
Quick research with a concise Slack summary.

**Trigger phrases:**
- "research X", "look up X", "find information about X"
- "what is X", "tell me about X", "summarize X"

**Requires:** `ANTHROPIC_API_KEY`

---

### Deep Research
Multi-source deep research — produces a full report, posts to `#sentinel-research`, and optionally emails the owner.

**Trigger phrases:**
- "deep research X", "do a deep dive on X"
- "investigate X in depth", "write a research report on X"
- "research and email me about X", "look into X and send me a report"

**Requires:** `ANTHROPIC_API_KEY`; `OWNER_EMAIL` for email delivery

---

## Knowledge Graph

### Knowledge Graph
Build and query a personal Neo4j graph of everything Sentinel works on — projects, repos, servers, clients, ideas, people, tech.

**Trigger phrases by action:**

| Action | Phrases |
|--------|---------|
| Add node | "add project X", "register repo X", "add server X to the graph" |
| Connect nodes | "connect X to Y", "link X to Y", "X uses Y", "X runs on Y" |
| Show relationships | "show relationships for X", "what is X connected to", "graph for X" |
| Search | "search graph for X", "find X in the graph", "is X in the graph" |
| Stats | "knowledge graph stats", "how many nodes", "graph overview", "what's in my knowledge graph" |
| Visualize | "show the knowledge graph", "open the graph", "visualize the graph", "knowledge graph visualization" |

**Node types:** `Project` · `Repo` · `Server` · `Client` · `Idea` · `Domain` · `Person` · `Task` · `Skill` · `Tech`

**Interactive graph viewer:** `https://sentinelai.cloud/api/v1/graph/viz`

**Requires:** `NEO4J_PASSWORD` (Neo4j service in docker-compose)

---

## Sentinel Self-Management

### Architecture Advisor
Analyse Sentinel's architecture (or any system) and produce a prioritised improvement report with bottlenecks, security gaps, quick wins, and an action plan. Report sent to `#sentinel-research` and emailed to owner.

**Trigger phrases:**
- "analyse architecture", "architecture review", "review my architecture"
- "what are the bottlenecks", "analyse Sentinel architecture"
- "suggest architecture improvements", "system design review"
- "scale my system", "architecture evolution", "system bottlenecks"

**Requires:** `ANTHROPIC_API_KEY`

---

### Server Shell
Run shell commands on the Sentinel server directly.

**Trigger phrases:**
- "read file X", "show me X", "cat X", "open X"
- "list files in X", "what files are in X"
- "search for X in the code", "grep X", "find where X is defined"
- "check disk space", "show processes", "what's running", "docker ps"
- "tail the logs", "show server logs", "check memory"
- "docker restart X", "restart the brain", "restart ai-brain"
- "docker compose up", "docker compose ps"
- "git status", "git log", "git diff", "git push", "git pull"
- "what env vars are set", "inspect the config", "show me the env"

---

### Deploy
Rebuild the Sentinel Docker image with latest committed code and restart the container.

**Trigger phrases:**
- "deploy", "rebuild", "restart the brain", "redeploy"
- "apply changes", "push and deploy", "deploy the changes"
- "make it live", "ship it"

---

### Skill Discovery
When no existing skill covers a request, analyzes the gap and proposes a new skill.

**Trigger phrases:**
- Automatically triggered when no other skill matches
- "what can you do", "do you have a skill for X"
- "can you add a skill for X"

---

## Summary Table

| Skill | Intent | Requires Config |
|-------|--------|-----------------|
| gmail_read | gmail_read | GOOGLE_REFRESH_TOKEN |
| gmail_send | gmail_send | GOOGLE_REFRESH_TOKEN |
| gmail_reply | gmail_reply | GOOGLE_REFRESH_TOKEN |
| calendar_read | calendar_read | GOOGLE_REFRESH_TOKEN |
| calendar_write | calendar_write | GOOGLE_REFRESH_TOKEN |
| contacts_read | contacts_read | GOOGLE_REFRESH_TOKEN |
| contacts_write | contacts_write | GOOGLE_REFRESH_TOKEN |
| github_read | github_read | GITHUB_TOKEN |
| github_write | github_write | GITHUB_TOKEN |
| repo_read | repo_read | — |
| repo_write | repo_write | — |
| repo_commit | repo_commit | — |
| code_change | code_change | GITHUB_TOKEN |
| cicd_read | cicd_read | GITHUB_TOKEN |
| cicd_trigger | cicd_trigger | GITHUB_TOKEN |
| cicd_debug | cicd_debug | GITHUB_TOKEN |
| whatsapp_read | whatsapp_read | TWILIO_* |
| whatsapp_send | whatsapp_send | TWILIO_* |
| ionos_cloud | ionos_cloud | IONOS_TOKEN |
| ionos_dns | ionos_dns | IONOS_TOKEN |
| project | project_create/build/deploy/status/list | ANTHROPIC_API_KEY |
| n8n | n8n_execute | N8N_API_KEY |
| n8n_manage | n8n_manage | N8N_API_KEY |
| smart_home | smart_home | HOME_ASSISTANT_URL + TOKEN |
| sentry_read | sentry_read | SENTRY_AUTH_TOKEN |
| sentry_manage | sentry_manage | SENTRY_AUTH_TOKEN |
| bug_hunt | bug_hunt | ANTHROPIC_API_KEY |
| task_create | task_create | — |
| task_read | task_read | — |
| task_update | task_update | — |
| research | research | ANTHROPIC_API_KEY |
| deep_research | deep_research | ANTHROPIC_API_KEY |
| knowledge_graph | knowledge_graph | NEO4J_PASSWORD |
| arch_advisor | arch_advisor | ANTHROPIC_API_KEY |
| server_shell | server_shell | — |
| deploy | deploy | — |
| code | code | ANTHROPIC_API_KEY |
| chat | chat | ANTHROPIC_API_KEY |
| skill_discover | skill_discover | ANTHROPIC_API_KEY |
