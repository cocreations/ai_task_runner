# Notifications: a webhook when the Claude usage limit is hit

The runner posts to a webhook when the account's Claude usage limit parks its
tasks (`credit_limit`) and again when that hold lifts (`credit_restored`). No
model is involved: the runner detects both itself, so the message arrives even
when nothing can run. The message names the **runner account** (the first label
of the container's URL, e.g. `moment`); the runner cannot know which Claude
account sits behind its token.

## Configure it

**Hosted (ai-task-runner.com):** Setup page → *Notifications* → paste the
webhook URL → *Save*, then *Send test*. The platform writes the file below into
the container's config mount; the runner reads it on the next tick, no restart.

**Self-hosted:** create `config/notifications.yaml`:

```yaml
webhooks:
  - url: https://discord.com/api/webhooks/123/abc…
    events: [credit_limit, credit_restored]   # optional; default = every event
```

Only `https://` URLs are used. A bare string entry is a URL subscribed to every
event.

## Creating a Discord webhook

1. In Discord, open the server → **Server Settings** → **Integrations** →
   **Webhooks** → **New Webhook**.
2. Give it a name (it shows as the poster; the runner also sets its own
   username, "AI Task Runner") and choose the channel the alerts should land in.
3. **Copy Webhook URL**.
4. Paste it on the Setup page's *Notifications* card (hosted) or into
   `notifications.yaml` (self-hosted) and press *Send test*.

Treat the URL as a secret: anyone who has it can post to that channel. The
runner never logs it (only its host).

## What is sent

One `POST` per webhook, JSON, `Content-Type: application/json`:

| key | meaning |
|---|---|
| `content` | the one human line, e.g. `⚠️ Task Runner **moment** hit its Claude session limit — 4 tasks parked, resumes 2026-11-04 17:00 UTC (in 2h 10m). Last task: moment-marketing-pipeline-agents / 20261104_143012_ab12.` |
| `username` | `AI Task Runner` |
| `embeds[0].fields` | the details below as Discord fields |
| `event` | `credit_limit` or `credit_restored` |
| `account` | the runner account name |
| `limit_kind` | `session`, `weekly`, `five_hour`, … (what Claude reported) |
| `reset_at` | ISO time the hold lifts |
| `parked_tasks` | how many tasks are waiting (credit_limit only) |
| `project`, `task_id` | the task that hit the limit (credit_limit only) |
| `sent_at` | ISO time of the post |

Discord ignores the extra top-level keys. A receiver that rejects them with
HTTP 400 gets the body again with only `content`, `username` and `embeds`.

## Delivery rules

- 5 s timeout per request; at most three attempts per webhook (covers one
  shape fallback and one retry). Transport trouble and 5xx are retried; a 4xx
  other than 400 is not.
- A failure is logged to `logs/runner.log` and otherwise ignored — a
  notification is never the reason a task fails.
- **Once per hold.** Up to `MAX_CONCURRENT` tasks can park on the same limit
  within seconds of each other, each in its own runner process; the marker file
  `tasks/.credit_hold_notified` makes the first one post and keeps the rest
  quiet. The lift is posted once, by whichever tick removes the marker, when no
  parked task has a future reset any more — after the reset passes, after the
  Claude token is changed, or after the parked tasks are cancelled.

## Where it lives

- `notify.py` — config loading, message and payload, delivery.
- `credit_hold.py` — the hold lookup shared with `server.py` and the marker.
- `runner.py` — `finalize_awaiting` → `notify_credit_limit`; `main` →
  `maybe_notify_restored`.
- `server.py` — `task_status` adds `Credit hold: until <iso> (<kind>)` to a
  *queued* task while a hold is active, so a client can show "paused until …"
  instead of a queue that is not moving.
