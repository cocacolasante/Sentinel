# CI/CD Setup — GitOps Loop with GHCR + Webhook Deploy

Automated delivery pipeline for Sentinel AI Brain:

```
AI edits code → commits → opens PR → human reviews/merges
→ GitHub Actions builds Docker image → pushes to GHCR
→ webhook triggers server → brain restarts with new image
```

After initial setup, every merge to `main` deploys automatically with no manual steps.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Files Added](#files-added)
3. [Step 1 — Generate the Webhook Secret](#step-1--generate-the-webhook-secret)
4. [Step 2 — Add GitHub Repository Secret](#step-2--add-github-repository-secret)
5. [Step 3 — Add Secret to Server .env](#step-3--add-secret-to-server-env)
6. [Step 4 — Start the Deploy Agent](#step-4--start-the-deploy-agent)
7. [Step 5 — Reload Nginx](#step-5--reload-nginx)
8. [Step 6 — Push Code and Wait for First Image](#step-6--push-code-and-wait-for-first-image)
9. [Step 7 — Make the GHCR Package Public](#step-7--make-the-ghcr-package-public)
10. [Step 8 — First Manual Deploy from GHCR](#step-8--first-manual-deploy-from-ghcr)
11. [Step 9 — Set Branch Protection](#step-9--set-branch-protection)
12. [Verification Checklist](#verification-checklist)
13. [AI Opens PRs Automatically](#ai-opens-prs-automatically)
14. [Secrets Reference](#secrets-reference)
15. [Troubleshooting](#troubleshooting)

---

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│  GitHub Actions (.github/workflows/release-image.yml)           │
│                                                                  │
│  trigger: CI workflow passes on main                            │
│      │                                                           │
│      ▼                                                           │
│  docker/build-push-action  →  ghcr.io/cocacolasante/sentinel    │
│      │                         :latest + :sha-<short SHA>        │
│      ▼                                                           │
│  curl POST https://sentinelai.cloud/webhooks/deploy             │
│         header: X-Deploy-Secret: <secret>                       │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼ nginx proxies /webhooks/ → deploy-agent:9000
┌─────────────────────────────────────────────────────────────────┐
│  deploy-agent container  (python:3.11-slim, stays up always)    │
│                                                                  │
│  webhook_server.py verifies X-Deploy-Secret                     │
│      │                                                           │
│      ▼                                                           │
│  deploy.sh                                                       │
│    docker pull ghcr.io/cocacolasante/sentinel:latest            │
│    docker compose up -d --no-deps brain celery-worker \         │
│                          celery-beat flower                      │
└─────────────────────────────────────────────────────────────────┘
```

**Key design decision:** `deploy-agent` is a separate container that is never in the list of services being restarted. It stays alive while the brain upgrades itself, so the webhook receiver is always reachable.

**Total time from merge to live:** ~2–4 minutes (CI) + ~30s (image pull) + ~5s (compose restart).

---

## Files Added

| File | Purpose |
|------|---------|
| `.github/workflows/release-image.yml` | Builds + pushes image, fires deploy webhook |
| `deploy/deploy.sh` | Pulls image, restarts brain services |
| `deploy/webhook_server.py` | HTTP server that receives the deploy signal |
| `Dockerfile` | Added `gh` CLI + OCI labels (for GHCR linking) |
| `docker-compose.yml` | Added `image:` tags, `GH_TOKEN`, `deploy-agent` service |
| `nginx/nginx.conf` | Added `/webhooks/` proxy block |
| `app/brain/llm_router.py` | Added safe PR workflow to AI system prompt |

---

## Step 1 — Generate the Webhook Secret

Run this on the server (or your local machine):

```bash
openssl rand -hex 32
```

Save the output — you will need it in both Step 2 and Step 3. It must be identical in both places.

Example output:
```
a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0
```

---

## Step 2 — Add GitHub Repository Secret

1. Go to your repository on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret**
4. Name: `DEPLOY_WEBHOOK_SECRET`
5. Value: paste the output from Step 1
6. Click **Add secret**

The `release-image.yml` workflow reads this secret as `${{ secrets.DEPLOY_WEBHOOK_SECRET }}` and sends it as the `X-Deploy-Secret` header. GitHub Actions keeps it masked in logs.

---

## Step 3 — Add Secret to Server .env

On the server, open `/root/sentinel/.env` and add:

```bash
DEPLOY_WEBHOOK_SECRET=<paste the value from Step 1>
```

Also confirm these are already set (they enable the AI to open PRs):

```bash
GITHUB_TOKEN=ghp_...          # personal access token with repo + workflow scope
GITHUB_USERNAME=cocacolasante
```

`GITHUB_TOKEN` is forwarded into the brain container as `GH_TOKEN`, which the `gh` CLI uses automatically.

---

## Step 4 — Start the Deploy Agent

```bash
cd /root/sentinel
docker compose up -d deploy-agent
```

Verify it started:

```bash
docker compose logs deploy-agent
# Expected output:
# 2026-03-03 12:00:00 INFO Webhook listening on :9000
```

Check it is reachable from within the Docker network:

```bash
docker compose exec brain curl -s http://deploy-agent:9000/
# Expected: 404 or connection response (any response means it's up)
```

---

## Step 5 — Reload Nginx

The `nginx.conf` now includes a `/webhooks/` location block. Apply it:

```bash
docker compose restart nginx
```

Verify nginx picked up the new config:

```bash
docker compose logs nginx | tail -5
# Should show no errors
```

Test the route is reachable (will return 403 because no secret header — that is correct):

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X POST https://sentinelai.cloud/webhooks/deploy
# Expected: 403
```

---

## Step 6 — Push Code and Wait for First Image

Commit and push the changes from this CI/CD setup:

```bash
cd /root/sentinel
git add .github/workflows/release-image.yml \
        deploy/deploy.sh \
        deploy/webhook_server.py \
        Dockerfile \
        docker-compose.yml \
        nginx/nginx.conf \
        app/brain/llm_router.py
git commit -m "feat: GHCR build + webhook deploy + AI PR workflow"
git push origin main
```

Watch the workflows run:

1. Go to your repo on GitHub → **Actions** tab
2. You will see the **CI** workflow start (lint → test → build)
3. Once CI passes, **Release Image** will trigger automatically
4. The Release Image workflow will push `ghcr.io/cocacolasante/sentinel:latest` to GHCR

The Release Image workflow takes ~3–5 minutes. Check it completed:

```bash
gh run list --workflow=release-image.yml --limit=5
```

---

## Step 7 — Make the GHCR Package Public

GHCR packages are private by default. The server needs to pull without authentication, so make it public:

1. Go to **github.com** → click your profile picture → **Your profile**
2. Click the **Packages** tab
3. Find `sentinel` in the list
4. Click the package name → **Package settings** (bottom of left sidebar)
5. Scroll to the **Danger Zone** section
6. Click **Change visibility** → select **Public** → confirm

From this point, `docker pull ghcr.io/cocacolasante/sentinel:latest` works on any machine with no authentication.

Verify from the server:

```bash
docker pull ghcr.io/cocacolasante/sentinel:latest
# Should download without a login prompt
```

---

## Step 8 — First Manual Deploy from GHCR

Switch the running containers to use the GHCR image instead of the locally built one:

```bash
cd /root/sentinel

# Pull the latest image from GHCR
docker compose pull brain celery-worker celery-beat flower

# Restart the brain services using the pulled image
docker compose up -d --no-deps brain celery-worker celery-beat flower
```

Verify the brain is alive:

```bash
curl https://sentinelai.cloud/api/v1/health
# Expected: {"status": "ok", "redis": true, "postgres": true}
```

Check which image the brain is running:

```bash
docker inspect ai-brain | grep -A2 '"Image"'
# Should show the ghcr.io/cocacolasante/sentinel digest
```

From this point forward, every merge to `main` deploys automatically — no manual steps needed.

---

## Step 9 — Set Branch Protection

This prevents anyone (including the AI) from pushing directly to `main`. All changes must go through a PR with CI passing.

Run once from the server:

```bash
source /root/sentinel/.env

curl -s -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/cocacolasante/sentinel/branches/main/protection \
  -d '{
    "required_status_checks": {
      "strict": true,
      "contexts": ["Lint", "Test", "Build Docker image"]
    },
    "enforce_admins": false,
    "required_pull_request_reviews": null,
    "restrictions": null,
    "allow_force_pushes": false,
    "allow_deletions": false
  }'
```

A successful response returns the full protection JSON. If you see `{"message":"Not Found"}`, check that `GITHUB_TOKEN` has `repo` scope.

**What this does:**

- Direct push to `main` is blocked for everyone including the AI
- A PR can only merge after Lint, Test, and Build Docker image all pass
- `enforce_admins: false` means repo admins can still merge emergency hotfixes via the GitHub UI
- No human review is required — the AI can merge its own PRs once CI passes (via `gh pr merge --auto`)

---

## Verification Checklist

Run through these after completing all steps:

```bash
# 1. Deploy agent is running
docker compose ps deploy-agent
# Status: Up

# 2. Webhook returns 403 for requests without a valid secret (correct behavior)
curl -s -o /dev/null -w "%{http_code}" \
  -X POST https://sentinelai.cloud/webhooks/deploy
# Expected: 403

# 3. Webhook accepts a request with the correct secret
source /root/sentinel/.env
curl -s -X POST https://sentinelai.cloud/webhooks/deploy \
  -H "Content-Type: application/json" \
  -H "X-Deploy-Secret: $DEPLOY_WEBHOOK_SECRET" \
  -d '{"image": "ghcr.io/cocacolasante/sentinel:latest", "sha": "test"}'
# Expected: {"status": "deploy started", "sha": "test"}

# 4. Brain is alive after the test deploy
curl https://sentinelai.cloud/api/v1/health
# Expected: {"status": "ok", "redis": true, "postgres": true}

# 5. Branch protection is active (should be rejected)
git push origin main
# Expected: error: GH006: Protected branch update failed

# 6. Image exists in GHCR
gh api /user/packages/container/sentinel/versions --jq '.[0].metadata.container.tags'
# Expected: ["latest", "sha-<hash>"]

# 7. Check deploy agent logs for the test deploy
docker logs ai-deploy-agent
# Expected: lines showing "Deploy: image=... sha=test" and "Deploy complete"
```

---

## AI Opens PRs Automatically

The brain's system prompt now includes a safe code-change workflow. When you ask the AI to modify its own code, it follows these steps automatically:

```
You: "Add a rate-limit header to the health check response"

Brain:
  1. git checkout -b feat/health-rate-limit-header
  2. server_shell → read_file path=app/router/health.py
  3. repo_write → apply the change
  4. server_shell → git add -A && git commit -m "feat: add rate-limit header to health endpoint"
  5. server_shell → git push origin feat/health-rate-limit-header
  6. server_shell → gh pr create --title "feat: add rate-limit header to health endpoint" \
                                  --body "..." --base main
  7. server_shell → gh pr merge --auto --squash
  8. "PR #42 opened and auto-merge enabled. Deploys automatically after CI passes (~3 min)."
```

The AI will never push directly to `main` (branch protection blocks it) and always enables auto-merge so no human approval is needed once CI is green.

### Ask the AI to make a change

From Slack, the CLI (`brain chat`), or the REST API:

```
create a feature branch and add a /ping endpoint that returns {"pong": true}
```

The AI will open a PR and tell you the PR number. You can watch it:

```bash
gh pr list
gh pr view <number>
gh run list   # watch CI
```

---

## Secrets Reference

| Where | Key | Value |
|-------|-----|-------|
| GitHub → Repo → Settings → Secrets → Actions | `DEPLOY_WEBHOOK_SECRET` | `openssl rand -hex 32` output |
| `/root/sentinel/.env` | `DEPLOY_WEBHOOK_SECRET` | Same value as above |
| `/root/sentinel/.env` | `GITHUB_TOKEN` | GitHub PAT with `repo` + `workflow` scope |
| `/root/sentinel/.env` | `GITHUB_USERNAME` | `cocacolasante` |

`GITHUB_TOKEN` from `.env` is passed into the brain container as `GH_TOKEN` via `docker-compose.yml`. The `gh` CLI picks it up automatically, so `gh pr create` works inside the container without any extra login step.

**GHCR package is public** — no PAT is needed on the server for `docker pull`. The `GHCR_PAT` variable in `docker-compose.yml` is included for future private image scenarios but is not required today.

---

## Troubleshooting

### Release Image workflow is not triggering

**Symptom:** CI passes but Release Image never starts.

**Cause:** `workflow_run` only triggers for runs on the default branch (`main`). If you pushed to a different branch, Release Image will not fire.

**Fix:** Merge to `main` or push directly to `main` (while branch protection is still off).

Also check that the CI workflow name exactly matches the string in `release-image.yml`:

```yaml
# release-image.yml
on:
  workflow_run:
    workflows: ["CI"]    # must match the `name:` field in ci.yml exactly
```

Open `ci.yml` and confirm the top line is `name: CI`.

---

### Webhook returns 403 even with the correct secret

**Cause:** The secret in `.env` does not match the secret in GitHub.

**Fix:**

```bash
# Check what the server has (shows the value)
grep DEPLOY_WEBHOOK_SECRET /root/sentinel/.env

# Compare to what GitHub has stored
# GitHub → Repo → Settings → Secrets — you cannot read the value, only update it

# Solution: regenerate, update both places
openssl rand -hex 32
# paste into GitHub Secrets and into .env, then restart deploy-agent:
docker compose restart deploy-agent
```

---

### deploy-agent shows "no such file" for deploy.sh

**Cause:** The `deploy/` directory is bind-mounted from `/root/sentinel/deploy` but the files are missing or the directory was not created.

**Fix:**

```bash
ls /root/sentinel/deploy/
# If empty or missing, the git checkout did not include the files

git status deploy/
git log --oneline deploy/

# Re-pull if needed
git pull origin main

# Confirm the container sees the files
docker compose exec deploy-agent ls /deploy/
```

---

### Image pull fails on the server after making the package public

**Cause:** Docker's credential cache may have stale authentication state.

**Fix:**

```bash
# Remove any stale GHCR credentials
docker logout ghcr.io

# Pull again (no login required for public packages)
docker pull ghcr.io/cocacolasante/sentinel:latest
```

---

### Brain services restart but show the old image

**Cause:** `docker compose up -d --no-deps` uses the locally cached image, not the newly pulled one.

**Fix:** Always pull before restarting:

```bash
docker compose pull brain celery-worker celery-beat flower
docker compose up -d --no-deps brain celery-worker celery-beat flower

# Confirm the new digest is running
docker inspect ai-brain --format '{{.Image}}'
```

---

### gh CLI inside the brain container fails with "authentication required"

**Cause:** `GH_TOKEN` is not set in the brain container environment.

**Check:**

```bash
docker compose exec brain env | grep GH_TOKEN
# Should print: GH_TOKEN=ghp_...
```

**Fix:** Confirm `GITHUB_TOKEN` is in `/root/sentinel/.env` (the compose file maps it to `GH_TOKEN`), then restart the brain:

```bash
docker compose up -d brain
docker compose exec brain gh auth status
# Expected: Logged in to github.com as cocacolasante
```

---

### Branch protection curl returns 404

**Cause:** `GITHUB_TOKEN` lacks sufficient scope or the repo path is wrong.

**Check:**

```bash
source /root/sentinel/.env
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/cocacolasante/sentinel \
  | jq '.full_name'
# Expected: "cocacolasante/sentinel"
```

If this returns `null` or an error, the token either does not have `repo` scope or is invalid. Generate a new PAT at **github.com → Settings → Developer settings → Personal access tokens** with `repo` and `workflow` scopes.

---

### CI is failing — tests or lint errors

The Release Image workflow only runs after CI passes. Fix the test/lint errors in a feature branch, open a PR, and the pipeline resumes on merge.

Common quick fixes:

```bash
# Run lint locally
pip install ruff==0.9.0
ruff check .

# Auto-fix lint errors
ruff check . --fix

# Run tests locally
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
```
