"""
Project Celery tasks — autonomous build and IONOS deploy.

build_project(project_id)
    Runs an LLM agent loop (up to 20 rounds) to scaffold, write code,
    install dependencies, run tests, and create a start.sh entrypoint.
    Posts step-by-step results to the originating Slack thread.

deploy_project(project_id, ionos_location, server_cores, server_ram_mb)
    1. Provisions an IONOS Ubuntu 22.04 server.
    2. Polls until the public IP is assigned (up to 15 min).
    3. Waits for SSH to come up.
    4. Installs system packages based on the tech stack.
    5. Copies the project via SCP.
    6. Runs start.sh in a screen/nohup session.
    7. Updates the DB and DMs + Slacks the IP.

SSH key:
    Reads the private key from (in priority order):
      1. IONOS_SSH_PRIVATE_KEY   env var (key content, PEM format)
      2. IONOS_SSH_PRIVATE_KEY_PATH env var (path to key file)
      3. /root/sentinel-workspace/.ssh/id_deploy
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import textwrap
import time

from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

_WORKSPACE = "/root/sentinel-workspace" if os.path.isdir("/root/sentinel-workspace") else "/app"
_PROJECTS = f"{_WORKSPACE}/projects"

# Max rounds for the LLM build agent
_BUILD_MAX_ROUNDS = 20

# ── Tech-stack package maps ────────────────────────────────────────────────────

_APT_PACKAGES: dict[str, list[str]] = {
    "python": ["python3", "python3-pip", "python3-venv", "build-essential"],
    "fastapi": ["python3", "python3-pip", "python3-venv", "build-essential"],
    "flask": ["python3", "python3-pip", "python3-venv"],
    "django": ["python3", "python3-pip", "python3-venv", "build-essential"],
    "node": [],  # installed via NodeSource
    "nodejs": [],
    "express": [],
    "react": [],
    "nextjs": [],
    "next": [],
    "go": ["golang-go"],
    "golang": ["golang-go"],
    "rust": ["cargo", "rustc"],
    "static": ["nginx"],
    "html": ["nginx"],
}

_NODE_STACKS = {"node", "nodejs", "express", "react", "nextjs", "next"}

_RUN_CMD: dict[str, str] = {
    "python": "python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt && bash start.sh",
    "fastapi": "python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt && bash start.sh",
    "flask": "python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt && bash start.sh",
    "django": "python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt && bash start.sh",
    "node": "npm install && bash start.sh",
    "nodejs": "npm install && bash start.sh",
    "express": "npm install && bash start.sh",
    "react": "npm install && npm run build && bash start.sh",
    "nextjs": "npm install && npm run build && bash start.sh",
    "next": "npm install && npm run build && bash start.sh",
    "go": "go build -o app . && bash start.sh",
    "golang": "go build -o app . && bash start.sh",
    "rust": "cargo build --release && bash start.sh",
    "static": "bash start.sh",
    "html": "bash start.sh",
}

# ── SSH helpers ────────────────────────────────────────────────────────────────


def _get_ssh_key_path() -> str | None:
    """Return path to the SSH private key, writing it from env var if needed."""
    # 1. Key content in env var → write to temp location in workspace
    key_content = os.environ.get("IONOS_SSH_PRIVATE_KEY", "").strip()
    if key_content:
        key_path = f"{_WORKSPACE}/.ssh/id_deploy"
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        with open(key_path, "w") as f:
            f.write(key_content)
            if not key_content.endswith("\n"):
                f.write("\n")
        os.chmod(key_path, 0o600)
        return key_path

    # 2. Path in env var
    key_path = os.environ.get("IONOS_SSH_PRIVATE_KEY_PATH", "").strip()
    if key_path and os.path.exists(key_path):
        return key_path

    # 3. Well-known workspace location
    default = f"{_WORKSPACE}/.ssh/id_deploy"
    if os.path.exists(default):
        return default

    return None


def _ssh_cmd(ip: str, key_path: str, cmd: str, timeout: int = 60) -> tuple[str, int]:
    """Run cmd on remote server via SSH. Returns (output, exit_code)."""
    args = [
        "ssh",
        "-i",
        key_path,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "BatchMode=yes",
        f"root@{ip}",
        cmd,
    ]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        output = (r.stdout + r.stderr).strip()
        return output, r.returncode
    except subprocess.TimeoutExpired:
        return "[SSH command timed out]", -1
    except Exception as exc:
        return f"[SSH error: {exc}]", -1


def _scp_dir(local_path: str, ip: str, remote_path: str, key_path: str) -> tuple[str, int]:
    """SCP a directory to the remote server (tar + cat approach for reliability)."""
    tar_path = f"/tmp/project_{int(time.time())}.tar.gz"
    try:
        # Create tarball
        r = subprocess.run(
            ["tar", "czf", tar_path, "-C", os.path.dirname(local_path), os.path.basename(local_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0:
            return f"[tar failed: {r.stderr}]", r.returncode

        # SCP tarball
        r2 = subprocess.run(
            [
                "scp",
                "-i",
                key_path,
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=15",
                tar_path,
                f"root@{ip}:{remote_path}.tar.gz",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        os.unlink(tar_path)
        if r2.returncode != 0:
            return f"[scp failed: {r2.stderr}]", r2.returncode

        # Extract on remote
        project_name = os.path.basename(local_path)
        out, code = _ssh_cmd(
            ip,
            key_path,
            f"mkdir -p {remote_path} && tar xzf {remote_path}.tar.gz -C /opt && rm {remote_path}.tar.gz",
            timeout=60,
        )
        return out, code
    except Exception as exc:
        return f"[scp_dir error: {exc}]", -1


# ── IONOS IP polling ───────────────────────────────────────────────────────────


async def _poll_for_ip(dc_id: str, server_id: str, nic_id: str, timeout_sec: int = 900) -> str | None:
    """Poll the IONOS NIC until an IP is assigned. Returns the IP or None on timeout."""
    from app.integrations.ionos import IONOSClient

    client = IONOSClient()
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            nic = await client._get(f"/datacenters/{dc_id}/servers/{server_id}/nics/{nic_id}")
            ips = nic.get("properties", {}).get("ips") or []
            if ips:
                return ips[0]
        except Exception as exc:
            logger.debug("IP poll error (will retry): %s", exc)
        await asyncio.sleep(15)
    return None


async def _wait_for_ssh(ip: str, key_path: str, timeout_sec: int = 600) -> bool:
    """Wait until SSH is accepting connections. Returns True on success."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        _, code = await asyncio.to_thread(_ssh_cmd, ip, key_path, "echo ok", 10)
        if code == 0:
            return True
        await asyncio.sleep(20)
    return False


# ── build_project Celery task ──────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    name="app.worker.project_tasks.build_project",
    queue="tasks_workspace",
    max_retries=0,
    soft_time_limit=1800,  # 30 min soft
    time_limit=1900,
)
def build_project(
    self,
    project_id: int,
    auto_deploy: bool = False,
    ionos_location: str = "de/fra",
) -> dict:
    """LLM agent loop that scaffolds and builds the project."""
    try:
        return asyncio.run(
            _build_project(
                self.request.id or str(project_id),
                project_id,
                auto_deploy=auto_deploy,
                ionos_location=ionos_location,
            )
        )
    except Exception as exc:
        logger.error("build_project(%s) crashed: %s", project_id, exc, exc_info=True)
        _update_project(project_id, status="failed", build_log=str(exc))
        return {"error": str(exc)}


async def _build_project(
    celery_id: str,
    project_id: int,
    auto_deploy: bool = False,
    ionos_location: str = "de/fra",
) -> dict:
    import anthropic
    from app.config import get_settings
    from app.db import postgres
    from app.memory.redis_client import RedisMemory
    from app.integrations.slack_notifier import post_thread_reply_sync

    settings = get_settings()
    redis = RedisMemory()

    # ── Load project ──────────────────────────────────────────────────────────
    row = postgres.execute_one("SELECT * FROM projects WHERE id=%s", (project_id,))
    if not row:
        return {"error": f"Project #{project_id} not found"}

    name = row["name"]
    description = row.get("description") or ""
    tech_stack = (row.get("tech_stack") or "python").lower()
    path = row.get("path") or f"{_PROJECTS}/{row['slug']}"
    slug = row["slug"]
    channel = row.get("slack_channel") or ""
    thread_ts = row.get("slack_thread_ts") or ""

    os.makedirs(path, exist_ok=True)

    _update_project(project_id, status="building")

    # ── Create GitHub repo ────────────────────────────────────────────────────
    github_url = await _create_github_repo(slug, description or name)
    if github_url:
        postgres.execute(
            "UPDATE projects SET deploy_url=%s, updated_at=NOW() WHERE id=%s",
            (github_url, project_id),
        )

    if channel and thread_ts:
        repo_note = f"\n📦 Repo: {github_url}" if github_url else ""
        post_thread_reply_sync(
            f"🔨 *Building project: {name}*\n"
            f"Tech stack: {tech_stack} | Path: `{path}`{repo_note}\n"
            "_Writing code, tests, and docs — I'll update this thread as I go..._",
            channel,
            thread_ts,
        )

    # Acquire workspace lock
    acquired = False
    for _ in range(10):
        if redis.acquire_workspace_lock(celery_id):
            acquired = True
            break
        await asyncio.sleep(30)
    if not acquired:
        _update_project(project_id, status="failed", build_log="Could not acquire workspace lock")
        return {"error": "workspace_lock_timeout"}

    # ── LLM build agent ───────────────────────────────────────────────────────
    system_prompt = textwrap.dedent(f"""
        You are Sentinel, an expert software engineer building a complete project autonomously.

        Project: {name}
        Tech stack: {tech_stack}
        Directory: {path}
        Description: {description}

        Your job is to write a complete, working project in {path}/.
        Each response is ONE of:
          {{"command": "<bash command>", "reasoning": "<why>"}}
          {{"done": true, "summary": "<what was built>"}}
          {{"done": true, "failed": true, "summary": "<what went wrong>"}}

        Rules:
        - Write ALL files using echo/cat/heredoc or python3 -c "..."
        - ALWAYS create these files: README.md, .gitignore, start.sh
        - start.sh must start the application on port 8080 (use nohup/background for servers)
        - For Python: create requirements.txt and a working main entry point
        - For Node: create package.json with a "start" script
        - For Go: create go.mod and main.go
        - For static: create index.html and start.sh that runs "python3 -m http.server 8080"
        - Install all dependencies (pip install / npm install / go mod tidy)
        - Test that the code at least starts (run it briefly, check for import errors)
        - Maximum {_BUILD_MAX_ROUNDS} commands total — be efficient
        - Use absolute path: {path}/
        - No markdown in your response — pure JSON only
    """).strip()

    messages: list[dict] = [{"role": "user", "content": f"Build: {name}\nDescription: {description}"}]

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    build_log = []
    all_good = True

    for round_num in range(_BUILD_MAX_ROUNDS):
        try:
            resp = await asyncio.to_thread(
                client.messages.create,
                model="claude-sonnet-4-6",  # use Sonnet for code quality
                max_tokens=1024,
                system=system_prompt,
                messages=messages,
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            action = json.loads(raw)
        except Exception as exc:
            logger.error("Build LLM round %d failed for project #%s: %s", round_num, project_id, exc)
            all_good = False
            build_log.append(f"❌ LLM error: {exc}")
            break

        if action.get("done"):
            summary = action.get("summary", "Build complete")
            icon = "❌" if action.get("failed") else "✅"
            build_log.append(f"{icon} {summary}")
            if action.get("failed"):
                all_good = False
            break

        cmd = (action.get("command") or "").strip()
        if not cmd:
            break

        # Execute command
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=path,
                executable="/bin/bash",
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            output = (stdout or b"").decode("utf-8", errors="replace")[:2000].strip()
            exit_code = proc.returncode or 0
        except asyncio.TimeoutError:
            output, exit_code = "[timed out after 5min]", -1
        except Exception as exc:
            output, exit_code = f"[error: {exc}]", -1

        icon = "✅" if exit_code == 0 else "❌"
        snippet = f"```\n{output[:600]}\n```" if output else ""
        log_line = f"{icon} Round {round_num + 1}: `{cmd[:100]}`\n{snippet}".strip()
        build_log.append(log_line)
        logger.info("Project #%s build round %d exit=%d cmd=%s", project_id, round_num + 1, exit_code, cmd[:80])

        # Save running log to DB
        _update_project(project_id, build_log="\n\n".join(build_log))

        # Feed back to LLM
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Output (exit {exit_code}):\n```\n{output[:1500]}\n```\n"
                    + ("Continue." if exit_code == 0 else "Command FAILED. Fix it or mark done/failed.")
                ),
            }
        )

        if exit_code != 0 and round_num >= _BUILD_MAX_ROUNDS - 3:
            all_good = False
            break

    redis.release_workspace_lock(celery_id)

    final_status = "built" if all_good else "failed"
    full_log = "\n\n".join(build_log)
    _update_project(project_id, status=final_status, build_log=full_log)

    # ── Post-build: tests + README + git push + KG ────────────────────────────
    if all_good:
        await _post_build(
            project_id=project_id,
            name=name,
            slug=slug,
            description=description,
            tech_stack=tech_stack,
            path=path,
            github_url=github_url,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            settings=settings,
        )

    # Post Slack summary
    if channel and thread_ts:
        if all_good:
            header = f"✅ *{name}* — build complete!"
            log_tail = "\n".join(build_log[-4:])
            repo_line = f"\n📦 {github_url}" if github_url else ""
            body = (
                f"Code built at `{path}`{repo_line}\n"
                f"```\n{log_tail}\n```\n"
            ) + (
                "🚀 Queuing IONOS deploy next..."
                if auto_deploy
                else f"_Say 'deploy project {name}' to spin up a staging server._"
            )
        else:
            header = f"❌ *{name}* — build failed"
            log_tail = "\n".join(build_log[-8:])
            body = f"```\n{log_tail}\n```"
        divider = "─" * 36
        post_thread_reply_sync(f"{header}\n{divider}\n{body}", channel, thread_ts)

    if all_good and auto_deploy:
        deploy_project.apply_async(
            args=[project_id],
            kwargs={"ionos_location": ionos_location},
            queue="tasks_general",
        )

    return {"project_id": project_id, "status": final_status, "rounds": round_num + 1}


async def _create_github_repo(slug: str, description: str, private: bool = True) -> str | None:
    """Create a GitHub repo. Returns clone URL or None."""
    import httpx
    from app.config import get_settings

    token = get_settings().github_token
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                "https://api.github.com/user/repos",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
                json={"name": slug, "description": description, "private": private, "auto_init": False},
            )
            if r.status_code == 201:
                return r.json().get("clone_url") or r.json().get("html_url")
            if r.status_code == 422:  # already exists
                user_r = await c.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"token {token}"},
                )
                username = user_r.json().get("login", "")
                return f"https://github.com/{username}/{slug}.git"
    except Exception as exc:
        logger.warning("GitHub repo creation failed (non-fatal): %s", exc)
    return None


async def _push_to_github(path: str, repo_url: str, commit_msg: str) -> bool:
    """Init git, commit all files, push to GitHub. Returns True on success."""
    from app.config import get_settings

    token = get_settings().github_token
    if not token or not repo_url:
        return False
    auth_url = repo_url.replace("https://", f"https://{token}@")
    steps = [
        f"git -C {path} init -b main 2>/dev/null || git -C {path} checkout -b main 2>/dev/null || true",
        f"git -C {path} config user.email 'sentinel@sentinelai.cloud'",
        f"git -C {path} config user.name 'Sentinel AI'",
        f"git -C {path} add -A",
        f"git -C {path} commit -m '{commit_msg}' --allow-empty",
        f"git -C {path} remote remove origin 2>/dev/null || true",
        f"git -C {path} remote add origin {auth_url}",
        f"git -C {path} push -u origin main --force",
    ]
    for cmd in steps:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode not in (0, 1):  # 1 = nothing to commit is OK
            logger.warning("git push step non-zero: %s\n%s", cmd.split()[0:3], (out or b"").decode()[:200])
            if "push" in cmd:
                return False
    return True


async def _post_build(
    project_id: int,
    name: str,
    slug: str,
    description: str,
    tech_stack: str,
    path: str,
    github_url: str | None,
    channel: str,
    thread_ts: str,
    client,
    settings,
) -> None:
    """After main build: generate tests + README, push to GitHub, register in KG."""
    import anthropic
    from app.integrations.slack_notifier import post_thread_reply_sync

    def _post(msg: str) -> None:
        if channel and thread_ts:
            post_thread_reply_sync(msg, channel, thread_ts)

    # ── List generated files ──────────────────────────────────────────────────
    try:
        proc = await asyncio.create_subprocess_shell(
            f"find {path} -type f | grep -v __pycache__ | grep -v .git | head -30",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        file_list = (out or b"").decode().strip()
    except Exception:
        file_list = ""

    # ── Generate tests ────────────────────────────────────────────────────────
    try:
        test_resp = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": (
                    f"Project: {name} ({tech_stack})\nDescription: {description}\n"
                    f"Files:\n{file_list}\n\n"
                    "Write a complete test file for this project. "
                    "Output ONLY the file content, no explanation. "
                    "For Python use pytest. For Node use Jest. For Go use testing package."
                ),
            }],
        )
        test_code = test_resp.content[0].text.strip()
        if test_code.startswith("```"):
            test_code = test_code.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        test_filename = {
            "python": "tests/test_main.py", "fastapi": "tests/test_main.py",
            "flask": "tests/test_main.py", "django": "tests/test_main.py",
            "node": "tests/app.test.js", "nodejs": "tests/app.test.js",
            "express": "tests/app.test.js", "go": "main_test.go", "golang": "main_test.go",
        }.get(tech_stack, "tests/test_main.py")

        test_path = os.path.join(path, test_filename)
        os.makedirs(os.path.dirname(test_path), exist_ok=True)
        with open(test_path, "w") as f:
            f.write(test_code)
        _post(f"🧪 *Tests generated* — `{test_filename}`")
    except Exception as exc:
        logger.warning("Test generation failed (non-fatal): %s", exc)

    # ── Generate README ───────────────────────────────────────────────────────
    try:
        readme_resp = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a README.md for this project:\n"
                    f"Name: {name}\nDescription: {description}\nTech: {tech_stack}\n"
                    f"Files:\n{file_list}\n\n"
                    "Include: overview, setup instructions, usage, API endpoints if applicable. "
                    "Output ONLY the markdown, no preamble."
                ),
            }],
        )
        readme = readme_resp.content[0].text.strip()
        with open(os.path.join(path, "README.md"), "w") as f:
            f.write(readme)
        _post("📄 *README.md generated*")
    except Exception as exc:
        logger.warning("README generation failed (non-fatal): %s", exc)

    # ── Push to GitHub ────────────────────────────────────────────────────────
    if github_url:
        pushed = await _push_to_github(path, github_url, f"feat: initial build by Sentinel AI\n\n{description}")
        if pushed:
            _post(f"🚀 *Pushed to GitHub:* {github_url}")
        else:
            _post("⚠️ GitHub push failed — code is still in the workspace.")

    # ── Register in Knowledge Graph ───────────────────────────────────────────
    try:
        from app.integrations.knowledge_graph import auto_register_project
        await auto_register_project(
            name=name,
            repo_url=github_url or "",
            tech=tech_stack,
            description=description,
        )
    except Exception as exc:
        logger.debug("KG registration skipped (non-fatal): %s", exc)


# ── deploy_project Celery task ─────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    name="app.worker.project_tasks.deploy_project",
    queue="tasks_general",
    max_retries=0,
    soft_time_limit=1800,  # 30 min — provisioning + SSH setup takes time
    time_limit=1900,
)
def deploy_project(
    self,
    project_id: int,
    ionos_location: str = "de/fra",
    server_cores: int = 2,
    server_ram_mb: int = 2048,
) -> dict:
    """Provision IONOS server, SSH install, deploy project, report IP."""
    try:
        return asyncio.run(_deploy_project(project_id, ionos_location, server_cores, server_ram_mb))
    except Exception as exc:
        logger.error("deploy_project(%s) crashed: %s", project_id, exc, exc_info=True)
        _update_project(project_id, status="failed")
        _slack_error(project_id, str(exc))
        return {"error": str(exc)}


async def _deploy_project(
    project_id: int,
    ionos_location: str,
    server_cores: int,
    server_ram_mb: int,
) -> dict:
    from app.db import postgres
    from app.integrations.ionos import IONOSClient
    from app.integrations.slack_notifier import post_thread_reply_sync, post_dm_sync
    from app.config import get_settings

    settings = get_settings()

    row = postgres.execute_one("SELECT * FROM projects WHERE id=%s", (project_id,))
    if not row:
        return {"error": f"Project #{project_id} not found"}

    name = row["name"]
    slug = row["slug"]
    tech_stack = (row.get("tech_stack") or "python").lower()
    path = row.get("path") or f"{_PROJECTS}/{slug}"
    channel = row.get("slack_channel") or ""
    thread_ts = row.get("slack_thread_ts") or ""

    def _post(msg: str) -> None:
        if channel and thread_ts:
            post_thread_reply_sync(msg, channel, thread_ts)

    _update_project(project_id, status="deploying")
    _post(f"🚀 *Deploying {name}* to IONOS ({ionos_location})\n_Provisioning server..._")

    # ── SSH key ───────────────────────────────────────────────────────────────
    key_path = _get_ssh_key_path()
    if not key_path:
        err = (
            "No SSH private key found. Set IONOS_SSH_PRIVATE_KEY env var "
            "(private key PEM content) or IONOS_SSH_PRIVATE_KEY_PATH."
        )
        _update_project(project_id, status="failed")
        _post(f"❌ *Deploy failed — {name}*\n{err}")
        return {"error": err}

    # ── Provision server ──────────────────────────────────────────────────────
    ionos = IONOSClient()
    try:
        prov = await ionos.provision_server(
            name=f"sentinel-{slug}",
            location=ionos_location,
            cores=server_cores,
            ram_mb=server_ram_mb,
            storage_gb=30,
            ubuntu_version="22",
        )
    except Exception as exc:
        err = f"IONOS provisioning failed: {exc}"
        _update_project(project_id, status="failed")
        _post(f"❌ *Deploy failed — {name}*\n{err}")
        return {"error": err}

    dc_id = prov["datacenter_id"]
    server_id = prov["server_id"]
    nic_id = prov["nic_id"]

    postgres.execute(
        "UPDATE projects SET ionos_dc_id=%s, ionos_server_id=%s, ionos_nic_id=%s, updated_at=NOW() WHERE id=%s",
        (dc_id, server_id, nic_id, project_id),
    )
    _post(f"✅ *Server provisioned* — waiting for IP assignment...\nServer ID: `{server_id[:12]}…`")

    # ── Wait for IP ───────────────────────────────────────────────────────────
    ip = await _poll_for_ip(dc_id, server_id, nic_id, timeout_sec=900)
    if not ip:
        err = "Timed out waiting for public IP assignment (15 min)"
        _update_project(project_id, status="failed")
        _post(f"❌ *Deploy failed — {name}*\n{err}")
        return {"error": err}

    postgres.execute(
        "UPDATE projects SET deploy_ip=%s, updated_at=NOW() WHERE id=%s",
        (ip, project_id),
    )
    _post(f"✅ *IP assigned:* `{ip}`\n_Waiting for SSH to come up (~2 min)..._")

    # ── Wait for SSH ──────────────────────────────────────────────────────────
    ssh_ok = await _wait_for_ssh(ip, key_path, timeout_sec=600)
    if not ssh_ok:
        err = f"SSH timed out on {ip} after 10 min — server may still be booting"
        _update_project(project_id, status="failed")
        _post(f"❌ *Deploy failed — {name}*\n{err}")
        return {"error": err}

    _post(f"✅ *SSH connected to {ip}* — installing packages...")

    # ── System packages ───────────────────────────────────────────────────────
    apt_pkgs = ["curl", "git", "screen"] + _APT_PACKAGES.get(tech_stack, ["python3", "python3-pip"])
    apt_cmd = f"DEBIAN_FRONTEND=noninteractive apt-get update -qq && apt-get install -y {' '.join(apt_pkgs)}"

    out, code = await asyncio.to_thread(_ssh_cmd, ip, key_path, apt_cmd, timeout=300)
    if code != 0:
        logger.warning("apt install had issues (exit %d): %s", code, out[:200])
        # Non-fatal — continue

    # Node.js via NodeSource if needed
    if tech_stack in _NODE_STACKS:
        node_setup = "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs"
        out, code = await asyncio.to_thread(_ssh_cmd, ip, key_path, node_setup, timeout=180)
        logger.info("Node.js install exit=%d", code)

    _post(f"✅ *Packages installed* — uploading project files...")

    # ── Upload project ────────────────────────────────────────────────────────
    remote_dir = f"/opt/{slug}"
    out, code = await asyncio.to_thread(_scp_dir, path, ip, remote_dir, key_path)
    if code != 0:
        err = f"SCP upload failed: {out[:300]}"
        _update_project(project_id, status="failed")
        _post(f"❌ *Deploy failed — {name}*\n{err}")
        return {"error": err}

    _post(f"✅ *Files uploaded* — starting application...")

    # ── Run project ───────────────────────────────────────────────────────────
    run_cmd_template = _RUN_CMD.get(tech_stack, "bash start.sh")
    # Build the run command: install deps then start
    run_script = f"cd /opt/{slug} && {run_cmd_template}"

    # Wrap in screen so it survives SSH disconnect
    screen_cmd = f"screen -dmS app bash -c 'cd /opt/{slug} && {run_cmd_template} >> /var/log/app.log 2>&1'"
    out, code = await asyncio.to_thread(_ssh_cmd, ip, key_path, screen_cmd, timeout=300)
    logger.info("App start exit=%d output=%s", code, out[:200])

    # Give it a few seconds to start
    await asyncio.sleep(5)

    # Quick health check
    health_cmd = (
        f"curl -sf http://localhost:8080/ || curl -sf http://localhost:3000/ || echo 'app may still be starting'"
    )
    health_out, _ = await asyncio.to_thread(_ssh_cmd, ip, key_path, health_cmd, 15)

    # ── Finalize ──────────────────────────────────────────────────────────────
    deploy_url = f"http://{ip}:8080"
    _update_project(project_id, status="deployed", deploy_ip=ip, deploy_url=deploy_url)

    success_msg = (
        f"🌐 *{name} is live!*\n"
        f"{'─' * 36}\n"
        f"• IP: `{ip}`\n"
        f"• URL: {deploy_url}\n"
        f"• Server: `{server_id[:12]}…` | Datacenter: `{dc_id[:12]}…`\n"
        f"• Tech: {tech_stack}\n\n"
        f"Health check: `{health_out[:200]}`\n\n"
        "_To check logs: SSH root@{ip} and run `screen -r app` or `cat /var/log/app.log`_"
    )
    _post(success_msg)

    # DM the owner
    try:
        dm_text = (
            f"🌐 *Project deployed: {name}*\n"
            f"IP: `{ip}` | URL: {deploy_url}\n"
            f"Tech: {tech_stack} | Location: {ionos_location}"
        )
        post_dm_sync(dm_text)
    except Exception:
        pass

    return {
        "project_id": project_id,
        "status": "deployed",
        "ip": ip,
        "url": deploy_url,
        "server_id": server_id,
        "datacenter_id": dc_id,
    }


# ── Helpers ────────────────────────────────────────────────────────────────────


def _update_project(project_id: int, **fields) -> None:
    if not fields:
        return
    try:
        from app.db import postgres

        set_clauses = ", ".join(f"{k}=%s" for k in fields) + ", updated_at=NOW()"
        postgres.execute(
            f"UPDATE projects SET {set_clauses} WHERE id=%s",
            (*fields.values(), project_id),
        )
    except Exception as exc:
        logger.warning("Could not update project #%s: %s", project_id, exc)


def _slack_error(project_id: int, error: str) -> None:
    try:
        from app.db import postgres
        from app.integrations.slack_notifier import post_thread_reply_sync

        row = postgres.execute_one(
            "SELECT name, slack_channel, slack_thread_ts FROM projects WHERE id=%s",
            (project_id,),
        )
        if row and row.get("slack_channel") and row.get("slack_thread_ts"):
            post_thread_reply_sync(
                f"❌ *Deploy failed — {row['name']}*\n`{error[:400]}`",
                row["slack_channel"],
                row["slack_thread_ts"],
            )
    except Exception:
        pass
