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
  knowledge_graph  — query or update the personal knowledge graph (projects, repos, servers, clients, ideas)
  rmm_read         — list managed servers, check device status/health, show RMM events or incidents
  rmm_manage       — run commands on remote servers, restart services/containers, install or upgrade agents, reboot servers
  agent_exec       — execute a local command ON a remote Sentinel Mesh Agent (shell, logs, files, process, disk, restart)
  slack_read       — read messages from a Slack channel, search Slack, list channels, fetch DMs
  reddit_read      — fetch and AI-summarize top posts from a subreddit; post digest to sentinel-reddit
  reddit_schedule  — manage recurring Reddit digest schedules (add, list, remove, pause, resume)
  github_monitor   — add, remove, list, enable, disable, or assign GitHub repo monitors for issue auto-triage
  chat             — general reasoning / writing / coding (no external action)
  arch_advisor     — analyse system architecture and produce an evolution report
  data_intelligence — analyze time series data, detect anomalies, discover patterns across systems
  se_brainstorm    — brainstorm ideas, risks, and a sprint plan for a Sentinel self-improvement task or new project
  se_spec          — write a detailed functional specification for a task or project
  se_plan          — produce a numbered implementation plan + ADRs + implementation notes
  se_implement     — write all code files and produce an implementation summary
  se_review        — conduct a full audit/code-review and produce a verdict (APPROVED/NEEDS WORK/BLOCKED)
  se_workflow      — run the full 5-phase SE pipeline (brainstorm → spec → plan → implement → review)
  se_new_project   — build a new external client project from scratch (init git repo + full pipeline)
  se_status        — list all SE workflow tasks and their current phase/status
  fleet_search     — alias for cross_agent_query: search across all mesh agents
  agent_context_search — alias for cross_agent_query: find patterns/errors across agents
  sentry_to_tasks  — alias for sentry_errors_create_approval_tasks: bulk-create tasks from Sentry errors
  docker_drift     — check or auto-correct Docker Compose drift on a server
  cert_check       — check SSL/TLS certificate expiry for domains
  patch_audit      — audit and apply OS security patches on a server
  dns_audit        — audit DNS records (SPF, DMARC, DKIM, MX) for domains
  backup_check     — verify backup recency and optionally test restore
  infra_snapshot   — snapshot server state to Redis for rollback
  infra_rollback   — rollback server to a previous snapshot
  goal             — add a goal to the autonomous execution queue
  goal_status      — list or check goal queue and goal progress
  wake             — manual trigger of wake loop / goal queue check
  reflect          — run nightly reflection on last 24h execution data
  plan_goal        — plan steps for a goal without executing (dry run)
  autonomy_status       — check/display current autonomy gradient score and trend
  proposal_status       — list pending or recently dispatched improvement proposals
  prompt_refine         — propose, evaluate, or apply A/B prompt variant for a skill
  skill_evolve          — write a new skill from a description (requires opt-in flag)
  self_improvement_status — show Phase 4/5 self-improvement dashboard summary
  post_merge_hook       — (internal) trigger post-merge reload after sentinel/ PR merges
"""

import json
import logging
from datetime import date as _date

import anthropic

from app.config import get_settings

logger = logging.getLogger(__name__)
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
  cicd_debug:     {{"repo": "owner/name", "run_id": "", "fix": false}}
  contacts_read:  {{"action": "search" | "list" | "lookup_email", "query": "Laura", "email": ""}}
  contacts_write: {{"action": "add" | "update" | "delete", "name": "Laura Smith", "email": "laura@co.com", "phone": "+12125551234", "company": "", "id": ""}}
  whatsapp_read:  {{"action": "list" | "get", "to": "+12125551234", "limit": 20}}
  whatsapp_send:  {{"to": "+12125551234", "body": "Hey, are we still on for tomorrow?"}}
  ionos_cloud:    {{"action": "<action>", ...params}}
    Datacenter: list_datacenters | get_datacenter | create_datacenter | update_datacenter | delete_datacenter  (params: datacenter_id, name, location, description)
    Server: list_servers | get_server | server_status | create_server | update_server | start_server | stop_server | reboot_server | suspend_server | delete_server | get_server_console  (params: datacenter_id, server_id, name, cores, ram_mb, cpu_family)
    Provision: provision_server  (params: name, location, cores, ram_mb, storage_gb, ubuntu_version, datacenter_id, cube_template="Basic Cube M"|"Basic Cube L"|etc, static_ip=true/false, wait_for_ready=true/false)
    SSH/Deploy: ssh_exec | deploy_docker | configure_server | deploy_website  (params: host, command, username, image, container_name, port_map, repo_url, domain, branch)
    Templates: list_templates
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
  task_create:    Single task: {{"title": "Fix the login bug", "description": "Users can't log in on mobile", "priority": 3, "approval_level": 1, "due_date": "2026-03-10", "tags": "bug,mobile", "assigned_to": ""}}
                  Multiple tasks (ALWAYS use this when user asks to create 2+ tasks): {{"tasks": [{{"title": "Task one", "description": "...", "priority": 3, "approval_level": 1}}, {{"title": "Task two", "description": "...", "priority": 3, "approval_level": 1}}]}}
  task_read:      {{"action": "list" | "get", "status": "pending" | "in_progress" | "done" | "cancelled" | "", "priority": 4, "id": "", "limit": 20}}
  task_update:    {{"id": 42, "status": "in_progress" | "done" | "cancelled" | "pending", "priority": 4, "approval_level": 1, "title": "", "description": "", "tags": "", "assigned_to": ""}}
  deep_research:  {{"topic": "quantum computing", "context": "focus on near-term applications", "email": ""}}
  bug_hunt:       {{"hours": 24, "focus": "auth module"}}
  arch_advisor:   {{"target": "Sentinel AI platform", "focus": "scalability", "description": "optional free-text description if analysing an external system", "email": ""}}
  knowledge_graph: {{"action": "add" | "connect" | "show" | "search" | "stats" | "visualize", "label": "Project" | "Repo" | "Server" | "Client" | "Idea" | "Person" | "Tech", "name": "MyApp", "from": "SentinelAI", "to": "sentinel-repo", "relationship": "USES", "query": "search term"}}
  data_intelligence: {{
      "action": "analyze" | "anomalies" | "trends" | "patterns" | "overview",
      "metric": "cpu" | "memory" | "tasks" | "errors" | "api_usage" | "redis" | "celery" | "network" | "disk" | "milestones" | "auto",
      "window": "1h" | "6h" | "24h" | "7d" | "30d",
      "threshold": 2.5,
      "promql": "<optional raw PromQL expression>"
    }}
  rmm_read:       {{"action": "list" | "get" | "status" | "events" | "incidents" | "inventory" | "meshes", "node_id": "", "name": "", "group": "production" | "staging" | "dev", "project": "", "severity": "", "hours": 24, "limit": 20}}
  rmm_manage:     {{"action": "run_command" | "restart_service" | "restart_container" | "reboot" | "upgrade_agent" | "install_agent", "node_id": "", "name": "", "command": "", "service": "", "container": "", "host": "", "mesh_id": "", "username": "ubuntu"}}
  slack_read:     {{"action": "history" | "search" | "list_channels" | "dm_history", "channel": "sentinel-alerts", "query": "search term", "user": "username", "limit": 20}}
  reddit_read:    {{"subreddit": "python", "limit": 10, "time_filter": "day" | "week" | "month" | "year" | "all", "channel": "sentinel-reddit"}}
  reddit_schedule: {{"action": "add" | "list" | "remove" | "pause" | "resume", "subreddit": "worldnews", "cron": "0 8 * * *", "channel": "sentinel-reddit", "time_filter": "day", "limit": 10, "id": ""}}
  github_monitor: {{"action": "add" | "remove" | "list" | "assign" | "enable" | "disable",
                   "repo": "owner/repo", "agent_id": "uuid", "label": "my-app",
                   "issue_filter": "is:open label:bug", "poll_labels": "bug,enhancement"}}
  agent_registry: {{"action": "list" | "get" | "fleet_summary" | "health", "agent_id": "", "env": "staging" | "production", "connected": true | false}}
  agent_manage:   {{"action": "provision" | "revoke" | "dispatch_patch", "agent_id": "", "app_name": "", "sentinel_env": "staging", "hostname": "", "diff_text": ""}}
  remote_log_analysis: {{"agent_id": "", "error_event": {{"stack_trace": "", "context_lines": [], "file_paths": []}}}}
  patch_dispatch: {{"agent_id": "", "diff_text": "", "triggered_by": "manual" | "log_error" | "sentry", "files_changed": []}}
  cross_agent_query: {{"query": "search term", "namespace_filter": "all" | "<agent_id>" | "<app_name>"}}
  agent_exec:     Execute a command ON a remote agent's server. Use when [AGENT CONTEXT] is present OR user says "on agent X" / "on the remote server".
                  {{"agent_id": "<from AGENT CONTEXT or user>", "command": "shell" | "read_logs" | "process_status" | "disk_usage" | "restart_app" | "read_file" | "list_files" | "write_file" | "env_info", "args": {{
                    "cmd": "shell command to run",
                    "lines": 100,
                    "path": "/path/to/file",
                    "content": "file content for write_file",
                    "append": false,
                    "recursive": false,
                    "pattern": "*.py",
                    "force": false,
                    "approved": false
                  }}}}
  se_brainstorm:  {{"title": "Add Redis caching to the brain", "description": "cache LLM responses for repeated queries", "repo": "owner/sentinel", "slug": "", "project_type": "sentinel"}}
  se_spec:        {{"title": "Add Redis caching to the brain", "description": "...", "slug": "add-redis-caching", "repo": "", "project_type": "sentinel"}}
  se_plan:        {{"title": "Add Redis caching to the brain", "slug": "add-redis-caching", "description": "", "repo": "", "project_type": "sentinel"}}
  se_implement:   {{"title": "Add Redis caching to the brain", "slug": "add-redis-caching", "description": "", "repo": "", "project_type": "sentinel"}}
  se_review:      {{"title": "Add Redis caching to the brain", "slug": "add-redis-caching", "description": "", "repo": "", "project_type": "sentinel"}}
  se_workflow:    {{"title": "Add Redis caching to the brain", "description": "cache LLM responses for repeated queries", "slug": "", "repo": "owner/sentinel", "project_type": "sentinel"}}
  se_new_project: {{"title": "ClientCo Landing Page", "description": "Marketing site for ClientCo with contact form", "tech_stack": "static", "slug": "", "repo": ""}}
  se_status:      {{}}
  code:           {{}}
  skill_discover: {{}}
  chat:           {{}}
  docker_drift:   {{"server": "localhost", "auto_correct": false, "dry_run": true}}
  cert_check:     {{"action": "check_all" | "check" | "renew", "domain": "example.com", "server": "localhost"}}
  patch_audit:    {{"server": "localhost", "auto_apply": false, "dry_run": true, "severity_threshold": "medium"}}
  dns_audit:      {{"action": "audit_all" | "audit", "domain": "example.com"}}
  backup_check:   {{"server": "localhost", "test_restore": false, "backup_path": "/var/backups/sentinel"}}
  infra_rollback: {{"action": "rollback" | "list", "server": "localhost", "snapshot_key": ""}}
  goal:           {{"title": "Check docker drift on all servers", "description": "...", "priority": 5.0, "skill_hint": "docker_drift"}}
  goal_status:    {{"action": "list", "limit": 10}}
  reflect:        {{"lookback_hours": 24}}
  plan_goal:      {{"goal_id": "", "title": "Plan docker drift check", "description": "..."}}
  autonomy_status:        {{}}
  proposal_status:        {{"limit": 10}}
  prompt_refine:          {{"skill_name": "docker_drift", "action": "propose" | "evaluate" | "apply"}}
  skill_evolve:           {{"description": "A skill that does X", "title": "new_skill_name"}}
  self_improvement_status: {{"period_hours": 24}}

Routing guidance:
  SE Workflow Pipeline:
  - "brainstorm X", "/brainstorm X", "brainstorm ideas for X" → se_brainstorm with title=X
  - "/spec-task X", "spec out X", "write a spec for X", "specification for X" → se_spec with title=X
  - "/plan-task X", "plan out X", "create a plan for X", "implementation plan for X" → se_plan with title=X
  - "/implement-task X", "implement X", "code up X", "build X", "write the code for X" → se_implement with title=X
  - "/review-task X", "audit X", "code review X", "review the implementation of X" → se_review with title=X
  - "se workflow X", "full pipeline for X", "run the full se pipeline for X", "all 5 phases for X" → se_workflow with title=X
  - "build me a X", "new project: X", "build a website for X", "build a new app X", "new external project X", "create a client project X", "build a React app", "build me a website", "create a REST API", "scaffold a new project", "build a dashboard for", "write a FastAPI service", "create a Node app", "build a landing page for" → se_new_project with title=X
  - "se status", "show my se tasks", "show my projects", "list se workflow tasks", "se pipeline status" → se_status
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
  - "deploy website", "deploy portfolio", "deploy site to server", "clone repo and configure apache", "pull repo and set up web server" → ionos_cloud action=deploy_website with host, repo_url, domain
  - "spin up a cube server", "provision a cube m", "deploy a cube", "cube m server" → ionos_cloud action=provision_server with cube_template="Basic Cube M", static_ip=true, wait_for_ready=true
  - "list cube templates", "what cube servers are available", "show server templates" → ionos_cloud action=list_templates
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
  - "research X", "do a deep dive on X", "research and send me X", "investigate X in depth", "write a research report on X", "look into X and email me", "deep research X" → deep_research with topic=X
  - "scan logs", "analyze errors", "hunt for bugs", "check for errors", "bug hunt", "analyze errors from last Xh", "find bugs", "what errors are happening", "scan for issues", "run bug hunt", "SRE scan" → bug_hunt with hours extracted from message (default 24)
  - "analyse architecture", "analyze architecture", "architecture review", "architecture advice", "system design review", "review my architecture", "architecture improvements", "what are the bottlenecks", "system bottlenecks", "scale my system", "architecture evolution", "suggest architecture improvements", "analyse sentinel architecture", "analyze sentinel" → arch_advisor with target extracted (default "Sentinel AI platform"), focus extracted if mentioned (e.g. "scalability", "security", "performance", "reliability")
  - "create a task", "add a task", "track this", "new task", "remember to", "log a task" → task_create
  - "remind me", "reminder for", "to-do", "to do list", "don't forget", "add to my reminders", "set alarm", "add a reminder" → task_create (with title from the reminder text and appropriate due_date)
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
  ProjectSkill vs SE Workflow — important distinction:
  - project_create / project_build / project_deploy / project_status / project_list are for the ProjectSkill:
    full lifecycle management of named, tracked projects (stored in DB with build logs, deploy records, etc.)
  - se_new_project / se_workflow are for the 5-phase SE pipeline (brainstorm→spec→plan→implement→review)
    and are better suited for one-off tasks, Sentinel self-improvement, and structured engineering work.
  - When in doubt and the request mentions an external client or "build me a [app type]", prefer se_new_project.
  - "build me a ...", "create a project", "build a project", "make a ... app", "build an app", "scaffold a project", "I want to build ...", "write a ... application", "create a ... service" → project_create with name=<project name>, description=<full description>, tech_stack=<detected stack>, deploy=<true if user mentions staging/deploy/server>
  - "build and deploy ...", "create and host ...", "build ... and spin up a server", "deploy to staging" included in creation → project_create with deploy=true, ionos_location=<detected or 'eu'>
  - "deploy project ...", "spin up a server for ...", "deploy ... to ionos", "host project ...", "put it on a server" → project_deploy with project_id or slug
  - "rebuild project", "re-run the build", "try building again", "restart the build" → project_build
  - "project status", "how is the build going", "check project ...", "is ... done building" → project_status
  - "list my projects", "show projects", "what projects exist", "all projects" → project_list
  - "analyze API usage", "API usage trends", "show request trends", "traffic analysis" → data_intelligence with metric=tasks, action=trends
  - "detect anomalies", "find anomalies in metrics", "any spikes in CPU", "unusual activity" → data_intelligence with action=anomalies, metric=cpu (or extracted metric)
  - "server metrics analysis", "analyze server metrics", "how is the server performing" → data_intelligence with metric=cpu, window=24h
  - "analyze memory usage", "memory trends", "memory over time" → data_intelligence with metric=memory
  - "task activity patterns", "when are tasks running", "task throughput" → data_intelligence with metric=tasks
  - "error rate trends", "analyze errors over time", "error spike" → data_intelligence with metric=errors
  - "redis performance", "redis ops trends" → data_intelligence with metric=redis_ops
  - "celery task trends", "worker activity" → data_intelligence with metric=celery_tasks
  - "show patterns", "discover patterns", "time patterns", "usage patterns", "when is it busiest", "peak usage times" → data_intelligence with action=patterns
  - "system overview", "how is everything performing", "health trends", "dashboard analysis" → data_intelligence with metric=auto
  - "what's causing the spike", "why did CPU jump", "investigate the anomaly", "correlate metrics" → data_intelligence with action=anomalies
  - "list managed servers", "show all agents", "which servers are online", "RMM inventory", "show my infrastructure", "list rmm devices", "which servers are offline" → rmm_read with action=list
  - "show RMM status", "infrastructure health", "server health summary", "how many servers online" → rmm_read with action=status
  - "RMM events", "recent agent events", "server events" → rmm_read with action=events
  - "show incidents", "any server incidents", "infrastructure incidents", "recent alerts" → rmm_read with action=incidents
  - "show production servers", "list staging servers", "list dev servers" → rmm_read with action=list, group=<extracted>
  - "infrastructure report", "server inventory report" → rmm_read with action=inventory
  - "show meshes", "list mesh groups", "MeshCentral meshes" → rmm_read with action=meshes
  - "restart service X on server Y", "restart nginx on server Y" → rmm_manage with action=restart_service, service=X, node_id=Y
  - "restart container X on server Y", "restart docker container X" → rmm_manage with action=restart_container, container=X, node_id=Y
  - "run command X on server Y", "execute X on Y", "run shell command" → rmm_manage with action=run_command, command=X, node_id=Y
  - "reboot server X", "restart server X" → rmm_manage with action=reboot, node_id=X
  - "upgrade agent on X", "update meshcentral agent" → rmm_manage with action=upgrade_agent, node_id=X
  - "install meshcentral agent on X", "add server X to RMM" → rmm_manage with action=install_agent, host=X
  - "read slack channel X", "show messages in X", "what's in #X", "pull messages from X", "check #X", "show #X", "read #sentinel-alerts", "what did sentinel post to slack", "show recent slack messages", "what was sent to sentinel-alerts", "show me what was posted in X" → slack_read with action=history, channel=X
  - "search slack for X", "find messages about X in slack", "slack search X" → slack_read with action=search, query=X
  - "list slack channels", "what channels is sentinel in", "show all channels" → slack_read with action=list_channels
  - "show my DMs with X", "read DMs with X" → slack_read with action=dm_history, user=X
  - "summarize r/X", "what's the news in r/X", "check r/X", "give me a news update from r/X" → reddit_read with subreddit=X
  - "what's trending in r/X", "top posts in r/X this week" → reddit_read with subreddit=X, time_filter="week"
  - "run the Reddit skill", "fetch reddit posts" → reddit_read (ask for subreddit if not specified)
  - "set up a Reddit news schedule for r/X", "send me r/X every day at 8am" → reddit_schedule with action=add, subreddit=X
  - "list my Reddit schedules", "show Reddit digests" → reddit_schedule with action=list
  - "remove Reddit schedule for r/X", "delete Reddit digest for r/X" → reddit_schedule with action=remove, subreddit=X
  - "pause Reddit digest for r/X" → reddit_schedule with action=pause, subreddit=X
  - "resume Reddit digest for r/X" → reddit_schedule with action=resume, subreddit=X
  - "start monitoring repo X", "watch repo X", "monitor repo X", "add repo monitor X" → github_monitor action=add, repo=X
  - "stop monitoring repo X", "unwatch repo X", "remove repo monitor X" → github_monitor action=remove, repo=X
  - "assign repo X to agent Y" → github_monitor action=assign, repo=X, agent_id=Y
  - "list monitored repos", "show github monitors", "what repos are monitored", "github monitor list" → github_monitor action=list
  - "enable monitoring for X", "resume monitoring X" → github_monitor action=enable, repo=X
  - "disable monitoring for X", "pause monitoring X" → github_monitor action=disable, repo=X
  - "traffic spike Tuesday", "Tuesday pattern", "weekly patterns", "day of week analysis" → data_intelligence with action=patterns, window=7d
  - "add project X", "add repo X", "add node X", "register project X" → knowledge_graph with action=add, label=<type>, name=X
  - "connect X to Y", "link X to Y", "X uses Y", "X runs on Y" → knowledge_graph with action=connect, from=X, to=Y, relationship=<inferred>
  - "show relationships for X", "what is X connected to", "graph for X" → knowledge_graph with action=show, name=X
  - "search graph for X", "find X in the graph", "is X in the graph" → knowledge_graph with action=search, query=X
  - "knowledge graph stats", "how many nodes", "graph overview" → knowledge_graph with action=stats
  - "show the knowledge graph", "open the graph", "visualize the graph", "knowledge graph visualization" → knowledge_graph with action=visualize
  - "what's in my knowledge graph", "show all graph nodes", "graph summary" → knowledge_graph with action=stats
  - "list agents" | "show mesh agents" | "fleet status" | "connected agents" | "agent health" → agent_registry action=list
  - "fleet summary" | "how many agents online" | "mesh fleet" → agent_registry action=fleet_summary
  - "provision agent" | "add server to mesh" | "register new agent" → agent_manage action=provision
  - "revoke agent" | "disconnect agent" | "remove agent" → agent_manage action=revoke, agent_id={{id}}
  - "dispatch patch to agent" | "patch remote server" | "send patch to agent" → patch_dispatch agent_id={{id}}
  - "cross-agent errors" | "same error on multiple agents" | "fleet-wide search" → cross_agent_query
  - "across all agents", "cross-fleet", "fleet-wide", "search agent codebases", "all agents had this", "other servers had this", "find this error everywhere", "check all remotes" → cross_agent_query (fleet_search / agent_context_search aliases also map here)
  - "analyze agent logs" | "agent error analysis" | "remote log" → remote_log_analysis
  - "fix failing tests" | "auto-patch" | "self heal" | "heal tests" | "fix the tests" → self_heal, params: {{test_path, repo_path}}
  - "scaffold a project" | "build me a X service" | "create a new project" → project_scaffold, params: {{description, slug}}
  - "audit dependencies" | "check for CVEs" | "scan packages for vulnerabilities" → audit_deps, params: {{repo_path}}
  - "generate tests for" | "write unit tests for" | "create tests for" → generate_tests, params: {{source_path, repo_path}}
  - "verify the deploy" | "check deployment health" | "confirm deploy" → verify_deploy, params: {{base_url}}
  - "check docker drift", "docker compose drift", "is docker drifted", "detect docker drift" → docker_drift with server extracted (default localhost)
  - "auto-correct docker drift", "fix docker drift", "correct docker compose" → docker_drift with auto_correct=true
  - "check ssl certs", "check certificates", "ssl expiry", "cert status", "tls expiry" → cert_check with action=check_all
  - "audit dns", "check dns records", "spf check", "dmarc check", "dkim check", "dns health" → dns_audit
  - "check backups", "verify backups", "backup status", "is backup recent", "backup health" → backup_check
  - "test restore", "test backup restore" → backup_check with test_restore=true
  - "rollback infra", "rollback server", "restore snapshot", "infra rollback" → infra_rollback
  - "list snapshots", "show infra snapshots" → infra_rollback with action=list
  - "add a goal", "queue a goal", "new goal", "schedule a goal" → goal with title extracted
  - "show goals", "goal queue", "list goals", "pending goals", "what goals are queued" → goal_status
  - "run wake cycle", "trigger wake", "check goal queue", "wake up" → wake
  - "reflect on execution", "nightly reflection", "analyze last 24h", "skill patterns" → reflect
  - "plan goal", "plan steps for", "create execution plan" → plan_goal
  - "autonomy score", "check autonomy", "autonomy gradient", "autonomy status", "what's my autonomy score" → autonomy_status
  - "pending proposals", "list proposals", "proposal queue", "improvement proposals" → proposal_status
  - "refine prompt", "a/b test prompt", "improve skill prompt", "prompt variant" → prompt_refine
  - "evolve a skill", "write a new skill", "create a skill", "generate skill" → skill_evolve
  - "self improvement", "improvement dashboard", "improvement status", "show improvement metrics" → self_improvement_status
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
cicd_debug      — fetch failed CI/CD run logs, parse errors, and suggest or apply fixes
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
knowledge_graph — personal knowledge graph: add nodes (projects/repos/servers/ideas/people), connect them, search, visualize
slack_read      — read Slack channel history, search messages, list channels, fetch DMs; channels: sentinel-alerts, sentinel-evals, sentinel-milestones, rmm-production, rmm-dev-staging
reddit_read     — fetch + AI-summarize top/hot posts from any subreddit; posts digest to sentinel-reddit Slack channel
reddit_schedule — add, list, remove, pause, or resume recurring Reddit digest schedules (cron-based, stored in Redis)
github_monitor  — add, remove, list, enable, disable, or assign GitHub repo monitors for issue auto-triage
agent_registry   — list Sentinel Mesh Agents, fleet health, heartbeat status, connected agents
agent_manage     — provision new agent, revoke credentials, dispatch manual patch
remote_log_analysis — analyze error logs from remote agent, generate patch suggestion
patch_dispatch   — sign and send a code patch to a remote Sentinel Mesh Agent
cross_agent_query — fleet-wide query: find similar errors across all agents
code            — software engineering help, code review, debugging, architecture — no file edits
skill_discover  — when no skill exists for a task, analyze the gap and propose a new skill
docker_drift    — check or auto-correct Docker Compose drift on a server
cert_check      — check SSL/TLS certificate expiry for domains
patch_audit     — audit and apply OS security patches on a server
dns_audit       — audit DNS records (SPF, DMARC, DKIM, MX) for domains
backup_check    — verify backup recency and optionally test restore
goal            — add a new goal to the autonomous execution queue
goal_status     — list or check the goal queue and goal progress
wake            — trigger the 15-minute wake loop / goal queue check
reflect         — run nightly reflection and self-improvement analysis
autonomy_status — check the current autonomy gradient score and trend
proposal_status — list pending or recently dispatched improvement proposals
self_improvement_status — show self-improvement dashboard summary
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
        pairs = history[-(max_turns * 2) :]
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
                messages=[
                    {
                        "role": "user",
                        "content": _USER_TEMPLATE.format(
                            message=message,
                            available_skills=skill_list,
                            today=_date.today().isoformat(),
                            recent_context=recent_context,
                            followup_hint=followup_hint,
                        ),
                    }
                ],
            )
            raw = response.content[0].text.strip()
            # Strip any accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw)
            logger.debug(
                "Intent: %s (%.2f) params=%s", result.get("intent"), result.get("confidence"), result.get("params")
            )
            return result
        except Exception as exc:
            logger.warning("Intent classification failed (%s) — defaulting to chat", exc)
            return {"intent": "chat", "confidence": 0.5, "params": {}}
