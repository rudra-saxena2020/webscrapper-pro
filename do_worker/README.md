# DigitalOcean Worker — External Sync Scheduler

A lightweight Python service that fires the Mirage auto-sync pipeline twice
daily (10:00 AM and 10:00 PM IST) even when Replit is sleeping or restarting.
Replit remains the primary dashboard and execution engine; this worker is
purely an external clock and trigger.

---

## How it works

| Time (UTC) | Time (IST) | Action |
|---|---|---|
| 04:30 | 10:00 AM | POST `/api/auto-sync/trigger` → polls status → waits for completion |
| 16:30 | 10:00 PM | POST `/api/auto-sync/trigger` → polls status → waits for completion |
| Every 30 min | — | POST `/api/auto-sync/heartbeat` (keeps the dashboard badge green) |

If Replit is cold-starting or returns a 503, the worker retries the trigger
every 2 minutes for up to 10 minutes before logging a failure.

---

## Prerequisites

- A DigitalOcean account with App Platform access
- Your Replit app **deployed** (not just the dev URL) — or you can use the
  Replit dev URL for testing
- The `SYNC_TOKEN` secret set **identically** in both places (see below)

---

## Step 1 — Generate a shared secret

Run this once on any machine with Python 3:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output. You will paste it in two places.

---

## Step 2 — Set `SYNC_TOKEN` in Replit Secrets

1. Open your Replit project → **Secrets** (padlock icon in the sidebar).
2. Add a secret named `SYNC_TOKEN` with the token you generated above.
3. Restart the Flask backend (`python app.py`) so it picks up the new secret.

Once set, the `/api/auto-sync/trigger` endpoint will **require** the
`X-Sync-Token` header for all callers. The dashboard "Run Now" button calls
the endpoint directly from the browser (no header), so if you want to keep
manual triggers working from the dashboard you can leave `SYNC_TOKEN` unset
— the token auth is entirely optional.

---

## Step 3 — Deploy to DigitalOcean App Platform

### Option A — Deploy from GitHub (recommended)

1. Push this repo (including the `do_worker/` directory) to GitHub.
2. Go to **DigitalOcean → App Platform → Create App**.
3. Connect your GitHub repo.
4. When prompted for a component type, choose **Worker**.
5. Set the **Source Directory** to `do_worker`.
6. DigitalOcean will detect the `Dockerfile` automatically.
7. Under **Environment Variables**, add:

   | Key | Value |
   |---|---|
   | `REPLIT_APP_URL` | `https://your-replit-app.replit.app` |
   | `SYNC_TOKEN` | *(the token from Step 1 — mark as **Encrypted**)* |
   | `WORKER_VERSION` | `1.0.0` |

8. Click **Deploy**.

### Option B — Deploy from local Docker image

```bash
cd do_worker
docker build -t mirage-do-worker .
docker tag mirage-do-worker registry.digitalocean.com/<your-registry>/mirage-do-worker:latest
docker push registry.digitalocean.com/<your-registry>/mirage-do-worker:latest
```

Then create a Worker app in App Platform pointing at your registry image and
set the same environment variables.

---

## Local testing

```bash
cd do_worker
cp .env.example .env
# Edit .env: set REPLIT_APP_URL and SYNC_TOKEN
pip install -r requirements.txt
python scheduler.py
```

The worker will immediately send a heartbeat, then wait for the next scheduled
fire time. To test the trigger immediately, temporarily call the endpoint
directly:

```bash
curl -X POST https://your-replit-app.replit.app/api/auto-sync/trigger \
  -H "X-Sync-Token: <your-token>" \
  -H "Content-Type: application/json" \
  -d '{"run_type": "manual"}'
```

---

## Dashboard integration

When the worker is running, the **Auto Sync Center** tab on the Mirage
dashboard shows:

- **DO Worker** badge — **Online** (green) / **Delayed** (amber) / **Unreachable** (red)
  based on the last heartbeat timestamp.
- **Last triggered by** — "DigitalOcean worker" or "Scheduler (internal)"
  depending on who last fired the trigger.

---

## Environment variables reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `REPLIT_APP_URL` | ✅ | — | Public URL of your Replit app |
| `SYNC_TOKEN` | ✅ | — | Shared auth token |
| `WORKER_VERSION` | ❌ | `1.0.0` | Version label shown in dashboard |
| `POLL_INTERVAL` | ❌ | `60` | Seconds between status polls |
| `SYNC_TIMEOUT_H` | ❌ | `3` | Hours before poll loop gives up |
