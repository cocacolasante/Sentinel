# Reddit Skill — Setup & Reference

## Env Vars

| Variable | Default | Description |
|---|---|---|
| `SLACK_REDDIT_CHANNEL` | `sentinel-reddit` | Slack channel for digest delivery |
| `REDDIT_USER_AGENT` | `sentinel-ai-brain/1.0 (by /u/sentinel_ai)` | HTTP User-Agent for Reddit API |
| `REDDIT_SUBREDDITS` | `` (disabled) | Comma-separated list for static daily digest |
| `REDDIT_SCHEDULE_HOUR` | `8` | UTC hour for static digest (set `-1` to disable) |

Add these to your `.env` file if you want to customize defaults.

---

## One-Off Usage (Slack)

Ask Sentinel in Slack:

```
summarize r/python
what's trending in r/worldnews this week
check r/technology
give me a news update from r/MachineLearning
top posts in r/Python --limit 5
```

The digest is posted to `#sentinel-reddit` and also shown inline in Slack.

---

## Dynamic Schedules (via Slack)

Schedules are stored permanently in Redis (`sentinel:reddit:schedules`).

### Add a schedule
```
set up a Reddit news schedule for r/python every day at 8am
send me r/worldnews every day at 9am UTC
```
This creates a cron entry `0 8 * * *` for daily at 08:00 UTC.

### List schedules
```
list my Reddit schedules
show Reddit digests
```

### Remove a schedule
```
remove Reddit schedule for r/python
delete Reddit digest for r/worldnews
```

### Pause / Resume
```
pause Reddit digest for r/python
resume Reddit digest for r/python
```

---

## Static Schedule (config-based)

Set in `.env`:
```
REDDIT_SUBREDDITS=python,worldnews,MachineLearning
REDDIT_SCHEDULE_HOUR=8
```

These run daily at 08:00 UTC via the `reddit-digest-dispatch` Celery beat task.

---

## Manual Dispatch (Docker)

```bash
# Trigger immediately from Celery container
docker exec sentinel-celery-1 python -c \
  "from app.worker.reddit_tasks import dispatch_reddit_digests; dispatch_reddit_digests.apply()"
```

---

## Standalone Scripts

### Scrape only (table output)
```bash
python scripts/scrape_reddit.py r/python
python scripts/scrape_reddit.py r/python --limit 10 --time week --output json
```

### Scrape + post to Slack
```bash
SLACK_BOT_TOKEN=xoxb-... python scripts/send_slack.py r/python --channel sentinel-reddit
```

### Cron/n8n batch runner
```bash
REDDIT_SUBREDDITS=python,worldnews \
SLACK_BOT_TOKEN=xoxb-... \
SLACK_REDDIT_CHANNEL=sentinel-reddit \
python scripts/schedule_reddit.py
```

For n8n: use an **Execute Command** node with the above command, triggered by a Cron node.

---

## Cron Setup (system cron)

```cron
# Daily at 08:00 UTC
0 8 * * * cd /root/sentinel && REDDIT_SUBREDDITS=python,worldnews SLACK_BOT_TOKEN=xoxb-... python scripts/schedule_reddit.py >> /var/log/reddit_digest.log 2>&1
```

---

## Output Format

```
📰 Reddit Digest: r/python | 2026-03-11 08:00 UTC | today
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔥 Top Stories
1. Post title | ⬆️ 4,231 | 💬 312 | https://reddit.com/...
2. ...

🤖 AI Summary
<2-3 sentence narrative>

⚡ Breaking/Viral
• High-signal post title | ⬆️ 12,400 | https://reddit.com/...
```

---

## Error Handling

| Error | Behavior |
|---|---|
| `r/X` not found (404) | Posts error to `#sentinel-reddit`, returns `is_error=True` |
| Private / quarantined (403) | Posts clear message to `#sentinel-reddit` |
| Rate limited (429) | Retries 3x with exponential back-off; on exhaustion posts to `#sentinel-alerts` |
| Slack delivery failure | Logged as error, not re-raised |
| Invalid cron expression | Returns error to user, nothing written to Redis |

---

## Architecture Notes

- **Redis key**: `sentinel:reddit:schedules` — persistent JSON list (no TTL)
- **Celery beat**: `reddit-digest-dispatch` runs every hour (`crontab(minute=0)`)
- **Due check window**: a schedule is "due" if its next cron tick falls within the last 1 hour
- **Static vs dynamic**: static config entries are injected as virtual schedule entries at runtime
- **AI summary**: uses Claude Haiku (`claude-haiku-4-5-20251001`) for 2–3 sentence digest
- **Viral detection**: first hot post with `score > 1000` OR `upvote_ratio > 0.95`
