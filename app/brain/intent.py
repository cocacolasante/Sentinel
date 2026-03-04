"""
Intent Classifier

Uses Claude Haiku to classify each message into a structured intent with
extracted parameters. This drives the Phase 2 integration routing.

Intents:
  gmail_read       — read / search inbox
  gmail_send       — compose / send / draft email
  calendar_read    — check schedule or availability
  calendar_write   — create / update / delete event
  github_read      — list issues, PRs, notifications, repo info
  github_write     — create issue, comment, close issue
  smart_home       — control or query HA devices
  n8n_execute      — trigger a named n8n workflow
  server_shell     — run shell commands on the server (navigate, build, scaffold)
  project_create   — create a new coding project, scaffold and build autonomously
  project_build    — trigger/re-trigger a project build
  project_deploy   — deploy a project to an IONOS staging server
  project_status   — check the status of a project
  project_list     — list all projects
  chat             — general reasoning / writing / coding (no external action)
"""

import json
import logging
from datetime import date as _date

import anthropic

from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are an intent classification engine for a personal AI assistant. "
    "Classify the user message into exactly one intent and extract relevant "
    "parameters as JSON. Be precise. Never add explanations — return only JSON."
)

_USER_TEMPLATE = """Today's date: {today}  (use this to resolve relative dates like "Thursday" or "next week")
{recent_context}
Classify this message into one of these intents:

{available_skills}

Return ONLY valid JSON matching this schema exactly:
{{
  "intent": "<intent_name>",
  "confidence": <0.0-1.0>,
  "params": {{
    // intent-specific extracted fields — empty object if none
  }}
}}

Intent-specific param examples:
  gmail_read:     {{"action": "list" | "read" | "mark_read" | "labels", "query": "is:unread", "max_results": 10, "msg_id": ""}}
  gmail_send:     {{"to": "sarah@co.com", "subject": "Re: meeting", "body_hint": "I'll be 10 min late"}}
  gmail_reply:    {{"msg_id": "abc123", "body_hint": "sounds good, see you then"}}
  calendar_read:  {{"period": "today" | "tomorrow" | "this week" | "next week"}}
  calendar_write: {{"title": "Sprint review", "date": "2026-03-05", "time": "14:00", "duration_min": 60, "description": "", "timezone": "America/New_York", "attendees": ["person@example.com"]}}
  github_read:    {{"repo": "owner/name", "resource": "issues" | "prs" | "notifications"}}
  github_write:   {{"repo": "owner/name", "action": "create_issue", "title": "...", "body": "..."}}
  smart_home:     {{"action": "turn_on" | "turn_off" | "toggle" | "set" | "status", "entity": "light.living_room", "value": null}}
  n8n_execute:    {{"workflow": "daily_brief", "payload": {{}}}}
  n8n_manage:     {{"action": "list" | "get" | "create" | "activate" | "deactivate" | "delete", "workflow_id": "", "name": ""}}
  cicd_read:      {{"action": "list_workflows" | "list_runs" | "get_run", "repo": "owner/name", "workflow_id": "", "run_id": ""}}
  cicd_trigger:   {{"repo": "owner/name", "workflow_id": "deploy.yml", "ref": "main", "inputs": {{}}}}
  contacts_read:  {{"action": "search" | "list" | "lookup_email", "query": "Laura", "email": ""}}
  contacts_write: {{"action": "add" | "update" | "delete", "name": "Laura Smith", "email": "laura@co.com", "phone": "+12125551234", "company": "", "id": ""}}
  whatsapp_read:  {{"action": "list" | "get", "to": "+12125551234", "limit": 20}}
  whatsapp_send:  {{"to": "+12125551234", "body": "Hey, are we still on for tomorrow?"}}
  ionos_cloud:    {{"action": "<action>", ...params}}
    Datacenter: list_datacenters | get_datacenter | create_datacenter | update_datacenter | delete_datacenter  (params: datacenter_id, name, location, description)
    Server: list_servers | get_server | server_status | create_server | update_server | start_server | stop_server | reboot_server | suspend_server | delete_server | get_server_console  (params: datacenter_id, server_id, name, cores, ram_mb, cpu_family)
    Provision: provision_server  (params: name, location, cores, ram_mb, storage_gb, ubuntu_version, datacenter_id)
    SSH/Deploy: ssh_exec | deploy_docker | configure_server  (params: host, command, username, image, container_name, port_map)
    Volume: list_volumes | get_volume | create_volume | update_volume | delete_volume | list_attached_volumes | attach_volume | detach_volume | create_volume_snapshot | restore_snapshot  (params: datacenter_id, volume_id, server_id, name, size_gb, volume_type, image_id, snapshot_id)
    NIC: list_nics | get_nic | create_nic | update_nic | delete_nic  (params: datacenter_id, server_id, nic_id, lan_id, dhcp, ips)
    LAN: list_lans | get_lan | create_lan | update_lan | delete_lan  (params: datacenter_id, lan_id, name, public)
    Snapshot: list_snapshots | get_snapshot | update_snapshot | delete_snapshot  (params: snapshot_id, name, description)
    Firewall: list_firewall_rules | get_firewall_rule | create_firewall_rule | delete_firewall_rule  (params: datacenter_id, server_id, nic_id, rule_id, name, protocol, direction, port_range_start, port_range_end, source_ip, target_ip)
    IP Block: list_ips | get_ip_block | reserve_ip | update_ip_block | release_ip_block  (params: ip_block_id, location, size, name)
    Load Balancer: list_load_balancers | get_load_balancer | create_load_balancer | delete_load_balancer | list_lb_nics | add_lb_nic | remove_lb_nic  (params: datacenter_id, lb_id, nic_id, name, ip, dhcp)
    NAT Gateway: list_nat_gateways | get_nat_gateway | create_nat_gateway | delete_nat_gateway | list_nat_rules | create_nat_rule | delete_nat_rule  (params: datacenter_id, nat_id, rule_id, name, public_ips, rule_type, protocol, source_subnet)
    Kubernetes: list_k8s_clusters | get_k8s_cluster | create_k8s_cluster | delete_k8s_cluster | list_k8s_nodepools | get_k8s_nodepool | create_k8s_nodepool | delete_k8s_nodepool | get_k8s_kubeconfig  (params: cluster_id, nodepool_id, name, k8s_version, datacenter_id, node_count, cores, ram_mb, storage_gb)
    Images: list_images  (params: location, name_filter)
    Request: get_request_status  (params: request_id)
  ionos_dns:      {{"action": "list_zones" | "list_records" | "upsert_record" | "delete_record", "zone_name": "example.com", "name": "www", "type": "A", "content": "1.2.3.4", "ttl": 3600}}
  repo_read:      {{"action": "status" | "diff" | "list_files" | "read_file", "path": "app/main.py"}}
  repo_write:     {{"action": "write_file" | "patch_file", "path": "app/main.py", "content": "...", "old": "...", "new": "..."}}
  repo_commit:    {{"action": "commit" | "push" | "commit_push", "message": "Fix calendar timezone bug", "push": true}}
  code_change:    {{"branch": "feat/short-name", "path": "app/router/chat.py", "old": "exact existing text", "new": "replacement text", "commit_message": "feat: what changed and why", "pr_title": "feat: short description", "pr_body": "What changed and why"}}
  deploy:         {{"reason": "applied fix for contacts bug"}}
  sentry_read:    {{"action": "list" | "get" | "db", "project": "", "query": "is:unresolved", "issue_id": "", "limit": 20}}
  sentry_manage:  {{"action": "resolve" | "ignore" | "assign" | "comment", "issue_id": "123456", "assignee": "user@co.com", "text": "looking into this"}}
  server_shell:   {{"command": "ls -la /root/sentinel-workspace", "cwd": "/root/sentinel-workspace", "action": "read_file" | "search_code" | "list_files" | "inspect_env" | "docker_restart" | "docker_compose", "path": "/root/sentinel-workspace/app/brain/dispatcher.py", "pattern": "def classify", "service": "ai-brain", "sub_command": "ps"}}
  task_create:    {{"title": "Fix the login bug", "description": "Users can't log in on mobile", "priority": 3, "approval_level": 2, "due_date": "2026-03-10", "tags": "bug,mobile", "assigned_to": ""}}
  task_read:      {{"action": "list" | "get", "status": "pending" | "in_progress" | "done" | "cancelled" | "", "priority": 4, "id": "", "limit": 20}}
  task_update:    {{"id": 42, "status": "in_progress" | "done" | "cancelled" | "pending", "priority": 4, "approval_level": 1, "title": "", "description": "", "tags": "", "assigned_to": ""}}
  code:           {{}}
  skill_discover: {{}}
  chat:           {{}}

Routing guidance:
  - "update the CLI", "improve the CLI", "rewrite the CLI", "edit the CLI", "fix the CLI", "make the CLI better", "update brain.py", "edit brain.py", "rewrite brain.py", "show me brain.py", "read brain.py", "open brain.py", "show the CLI code", "look at the CLI code", "read the CLI" → server_shell with action=read_file, path=brain.py
  - "update your code", "edit your own code", "improve yourself", "rewrite yourself", "change your code", "look at yourself", "read your own code" → server_shell with action=list_files, path=/root/sentinel-workspace
  - "deploy an ubuntu server", "provision a server", "spin up a VPS", "create a cloud server", "set up an ubuntu vps" → ionos_cloud action=provision_server
  - "create a datacenter", "create a VDC", "new IONOS datacenter" → ionos_cloud action=create_datacenter
  - "list my datacenters", "show IONOS datacenters" → ionos_cloud action=list_datacenters
  - "list servers", "what servers do I have in DC X" → ionos_cloud action=list_servers, datacenter_id=X
  - "start/stop/reboot server X" → ionos_cloud action=start_server|stop_server|reboot_server
  - "list images", "available OS images", "what ubuntu images" → ionos_cloud action=list_images
  - "list volumes", "show volumes in DC X" → ionos_cloud action=list_volumes, datacenter_id=X
  - "create a volume", "add storage" → ionos_cloud action=create_volume
  - "attach volume X to server Y" → ionos_cloud action=attach_volume
  - "take a snapshot of volume X" → ionos_cloud action=create_volume_snapshot
  - "list NICs", "show network interfaces" → ionos_cloud action=list_nics
  - "list LANs", "show networks in DC" → ionos_cloud action=list_lans
  - "list snapshots", "show my snapshots" → ionos_cloud action=list_snapshots
  - "list firewall rules" → ionos_cloud action=list_firewall_rules
  - "add firewall rule", "open port 80" → ionos_cloud action=create_firewall_rule
  - "list IP blocks", "show reserved IPs" → ionos_cloud action=list_ips
  - "reserve an IP", "allocate IP" → ionos_cloud action=reserve_ip
  - "release IP block X" → ionos_cloud action=release_ip_block
  - "list load balancers", "show LBs" → ionos_cloud action=list_load_balancers
  - "create a load balancer" → ionos_cloud action=create_load_balancer
  - "list NAT gateways", "show NAT" → ionos_cloud action=list_nat_gateways
  - "create NAT gateway" → ionos_cloud action=create_nat_gateway
  - "list Kubernetes clusters", "show k8s clusters" → ionos_cloud action=list_k8s_clusters
  - "create a Kubernetes cluster" → ionos_cloud action=create_k8s_cluster
  - "get kubeconfig for cluster X" → ionos_cloud action=get_k8s_kubeconfig, cluster_id=X
  - "create a node pool", "add k8s nodes" → ionos_cloud action=create_k8s_nodepool
  - "check request status X", "what's the status of IONOS request X" → ionos_cloud action=get_request_status
  - "SSH into server at IP X", "run command X on server" → ionos_cloud action=ssh_exec
  - "deploy docker container X on server" → ionos_cloud action=deploy_docker
  - When the user asks to do MULTIPLE things in one message (e.g. "create a task AND set up a server"), pick the most complex/actionable intent. Mention in context_data that you will handle others afterward.
  - "deploy", "rebuild", "restart the brain", "redeploy", "apply changes", "push and deploy", "deploy the changes" → deploy
  - "update X and deploy", "change X and ship it", "fix X and push", "make a code change to X", "edit X and create a PR" → code_change (full workflow: branch + patch + PR + auto-merge)
  - "improve X", "fix X", "optimize X", "refactor X", "enhance X" where X is a file/code → repo_read then repo_write
  - "review code", "check the code", "analyse the codebase" → repo_read
  - "write code for...", "implement a function...", "help me code..." → code
  - "read file X", "show me the code in X", "cat X", "open X" → server_shell with action=read_file, path=X
  - "search for X in the code", "grep X", "find where X is defined" → server_shell with action=search_code, pattern=X
  - "list files in X", "what files are in X", "show directory X" → server_shell with action=list_files, path=X
  - "show me the codebase", "review the source", "look at the code" → server_shell with action=list_files, path=/root/sentinel-workspace
  - "what does X skill do", "how does X work", "explain X", "show me X" where X is a skill/file/module → server_shell with action=read_file, path=<most relevant file>
  - "what files exist in X", "find all python files", "list all skills" → server_shell with action=list_files or command with find
  - ANY request that requires understanding existing code before acting → server_shell to read the relevant file first
  - "show me X" where X is a git operation (diff, status) → repo_read
  - Requests to edit/create a specific file with a path → repo_write
  - Ambiguous improvement requests without a specific file → code (let LLM suggest approach)
  - "create a task", "add a task", "track this", "new task", "remember to", "log a task" → task_create
  - "list tasks", "show tasks", "what tasks", "my tasks", "view tasks", "open tasks", "pending tasks" → task_read
  - "mark task done", "complete task", "update task", "change priority", "close task", "set task status" → task_update
  - "go to /path", "navigate to", "cd to", "list files in", "what's in this directory" → server_shell
  - "create a directory", "mkdir", "make a folder", "scaffold a project" → server_shell
  - "run this command", "execute on the server", "check disk space", "show processes" → server_shell
  - "create a new project", "set up a repo", "init a project", "npm init / git init" → server_shell
  - "install packages", "pip install", "npm install", "apt install" → server_shell
  - "show me the logs", "tail the logs", "check server logs" → server_shell
  - "what's running", "ps aux", "top", "htop", "check memory / disk" → server_shell
  - "git status", "what's changed", "check git", "show git status", "any uncommitted changes", "check your workspace" → server_shell with command="cd /root/sentinel-workspace && git status"
  - "git log", "show commits", "last N commits", "recent commits", "commit history" → server_shell with command="cd /root/sentinel-workspace && git log --oneline -10"
  - "git diff", "show changes", "what did you change", "show me the diff" → server_shell with command="cd /root/sentinel-workspace && git diff"
  - "push to github", "git push", "push the code", "push changes", "push commits" → server_shell with command="cd /root/sentinel-workspace && git push origin HEAD"
  - "git commit", "commit the changes", "commit all changes" → server_shell with command="cd /root/sentinel-workspace && git add -A && git commit -m '<message>'"
  - "git pull", "pull latest", "pull from github" → server_shell with command="cd /root/sentinel-workspace && git pull"
  - "restart the brain", "restart ai-brain", "restart the container", "docker restart" → server_shell with action=docker_restart, service="ai-brain"
  - "restart nginx", "restart the proxy", "docker restart nginx" → server_shell with action=docker_restart, service="ai-nginx"
  - "docker ps", "what containers are running", "show docker status", "container status" → server_shell with action=docker_compose, sub_command="ps"
  - "docker compose up", "bring up services" → server_shell with action=docker_compose, sub_command="up -d"
  - "what env vars are set", "show configuration", "inspect the config", "what integrations are configured", "show me the env" → server_shell with action=inspect_env
  - "update and deploy", "push and restart", "commit push and deploy", "make it live" → use server_shell for git ops then intent=deploy
  - "show running services", "list docker containers", "docker status" → server_shell with command="docker ps"
  - "build me a ...", "create a project", "build a project", "make a ... app", "build an app", "scaffold a project", "I want to build ...", "write a ... application", "create a ... service" → project_create with name=<project name>, description=<full description>, tech_stack=<detected stack>, deploy=<true if user mentions staging/deploy/server>
  - "build and deploy ...", "create and host ...", "build ... and spin up a server", "deploy to staging" included in creation → project_create with deploy=true, ionos_location=<detected or 'eu'>
  - "deploy project ...", "spin up a server for ...", "deploy ... to ionos", "host project ...", "put it on a server" → project_deploy with project_id or slug
  - "rebuild project", "re-run the build", "try building again", "restart the build" → project_build
  - "project status", "how is the build going", "check project ...", "is ... done building" → project_status
  - "list my projects", "show projects", "what projects exist", "all projects" → project_list
  Tech stack detection for project_create:
    - "python", "fastapi", "flask", "django", "rest api" in description → tech_stack=python or fastapi/flask/django
    - "react", "frontend", "ui" → tech_stack=react
    - "next", "nextjs" → tech_stack=nextjs
    - "node", "express", "javascript backend" → tech_stack=node
    - "go", "golang" → tech_stack=go
    - "rust" → tech_stack=rust
    - "html", "static", "landing page", "website" → tech_stack=static
    - default → tech_stack=python

IMPORTANT for calendar_write: "date" must always be an absolute ISO date (YYYY-MM-DD).
Never output day names like "Thursday" — resolve them using today's date above.

Message: {message}
{followup_hint}"""

_DEFAULT_SKILLS = """gmail_read      — read, check, or search Gmail inbox; read a specific email; mark as read
gmail_send      — compose, send, or draft an email
gmail_reply     — reply to a specific email in-thread
calendar_read   — check schedule, events, or availability
calendar_write  — create, update, reschedule, or cancel a calendar event
github_read     — check issues, PRs, notifications, or repo info
github_write    — create an issue, comment on a PR, close an issue
smart_home      — control or query a smart home device (lights, thermostat, locks, etc.)
n8n_execute     — run a specific n8n workflow by name
n8n_manage      — list, create, activate, or delete n8n workflows
cicd_read       — check CI/CD pipelines: list GitHub Actions workflows, view run status
cicd_trigger    — trigger a GitHub Actions workflow manually
contacts_read   — search or look up a contact by name or email in the address book
contacts_write  — add, update, or delete a contact in the address book
whatsapp_read   — read or check recent WhatsApp messages
whatsapp_send   — send a WhatsApp message to a contact or number
ionos_cloud     — full IONOS DCD management: datacenters, servers (provision/start/stop/reboot/SSH), volumes (CRUD/attach/snapshot), NICs, LANs, firewall rules, IP blocks, load balancers, NAT gateways, Kubernetes clusters/nodepools, Docker deploy
ionos_dns       — manage IONOS DNS zones and records (A, CNAME, MX, TXT, etc.)
repo_read       — read, list, diff, or check status of Sentinel's own codebase/files
repo_write      — create or edit a file in Sentinel's codebase; improve, refactor, or patch files
repo_commit     — commit and/or push changes in Sentinel's repository to GitHub
code_change     — full autonomous code-change workflow: branch → patch file → commit → push → open PR + auto-merge in one shot
sentry_read     — list, search, or inspect Sentry error issues; show recent errors
sentry_manage   — resolve, ignore, assign, or comment on a Sentry issue
server_shell    — run shell commands on the server: read/write files, search code, list dirs, run builds, inspect processes/logs, git push/commit/pull, docker restart (action=docker_restart), docker compose (action=docker_compose), inspect env vars (action=inspect_env)
deploy          — rebuild the Sentinel Docker image with latest committed code and restart the container
task_create     — create a new tracked task with title, priority (1–5), and approval level (1–3)
task_read       — list, filter, or view existing tasks; check task status or priority
task_update     — update a task's status, priority, approval level, or description
code            — software engineering help, code review, debugging, architecture — no file edits
skill_discover  — when no skill exists for a task, analyze the gap and propose a new skill
chat            — anything else: analysis, writing, questions, conversation"""


# ── Classifier ────────────────────────────────────────────────────────────────

class IntentClassifier:
    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    @staticmethod
    def _fmt_history(history: list[dict], max_turns: int = 4) -> str:
        """
        Format the last `max_turns` (user+assistant) pairs as a compact context block.
        Truncates long messages so the Haiku prompt stays short.
        """
        if not history:
            return ""
        # history is a flat list: [user, assistant, user, assistant, ...]
        # Take only the most recent pairs
        pairs = history[-(max_turns * 2):]
        lines = ["Recent conversation (use this to understand follow-up messages):"]
        for turn in pairs:
            role = "User" if turn.get("role") == "user" else "Assistant"
            content = (turn.get("content") or "")[:300].replace("\n", " ")
            lines.append(f"{role}: {content}")
        return "\n".join(lines) + "\n"

    def classify(
        self,
        message: str,
        available_skills: str = "",
        history: list[dict] | None = None,
    ) -> dict:
        """
        Classify a message and return a dict with keys:
          intent (str), confidence (float), params (dict)
        Falls back to {"intent": "chat", "confidence": 0.5, "params": {}} on any error.

        history: prior conversation turns (Anthropic format) — used so that short
                 follow-up replies like "yes", "the first one", or "personal" are
                 correctly resolved to their parent intent.
        """
        try:
            skill_list = available_skills.strip() if available_skills.strip() else _DEFAULT_SKILLS
            recent_context = self._fmt_history(history) if history else ""

            # Hint the classifier when the message is suspiciously short (likely a follow-up)
            followup_hint = ""
            if history and len(message.strip().split()) <= 6:
                followup_hint = (
                    "NOTE: This message is very short and likely a direct answer to the "
                    "assistant's last question above. Use the conversation context to "
                    "determine the correct intent and fill in missing params accordingly."
                )

            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": _USER_TEMPLATE.format(
                        message=message,
                        available_skills=skill_list,
                        today=_date.today().isoformat(),
                        recent_context=recent_context,
                        followup_hint=followup_hint,
                    ),
                }],
            )
            raw = response.content[0].text.strip()
            # Strip any accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw)
            logger.debug("Intent: %s (%.2f) params=%s", result.get("intent"), result.get("confidence"), result.get("params"))
            return result
        except Exception as exc:
            logger.warning("Intent classification failed (%s) — defaulting to chat", exc)
            return {"intent": "chat", "confidence": 0.5, "params": {}}
