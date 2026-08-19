# Rapsodo pipeline — deployment

## Why this lives in Player_Dev_Hub

`Player_Dev_Hub` is a clone of `IDBach16/moeller-hub` checked out on the
**`player-development-system`** branch. The live hub (`Moeller_Hub`, branch `main`)
is deliberately untouched.

That separation exists because the two branches diverged architecturally and cannot
be merged casually:

- `main` declares routes at module level (`@app.route` at column 0)
- `player-development-system` uses an app-factory with routes nested inside a function
- their `SYSTEM` prompts contradict — `main` permits a markdown subset, the branch
  requires plain text only

Merging them is a real reconciliation job (350-line conflict in `app.py`) and deserves
its own session. **Nothing here requires that merge.** The Rapsodo job only needs
`db.py` and `metrics.py`, which are standalone SQLAlchemy/dataclass modules with no
Flask dependency — and they sit in this repo root, so there is exactly one copy of the
schema and it cannot drift from what the app uses.

## Layout

```
Player_Dev_Hub/
  db.py, metrics.py, ingest.py, ...   the player-dev data layer (branch)
  rapsodo/
    rapsodo_client.py   auto-login + the 3 API calls
    pull.py             walks the chain, archives raw JSON, builds the CSV
    load_db.py          writes into players / sessions / pitch_metrics
    daily.py            scheduled entrypoint (pull -> CSV -> DB)
    RECON.md            endpoint + field reference, and the data-integrity rules
```

`requirements.txt` at the repo root already covers this pipeline
(requests, pandas, SQLAlchemy, psycopg2-binary).

## Credentials

`Player_Dev_Hub/.env` (gitignored):
```
RAPSODO_EMAIL=...
RAPSODO_PASSWORD=...
```
The job logs in for itself via `POST /v3/auth/login` and caches the JWT in
`token.json`, refreshing when it is within 2 days of expiry. **No manual token step.**

## Local run

```
python rapsodo/pull.py --start 2025-08-19 --end 2026-08-19   # backfill -> out/*.csv
python rapsodo/load_db.py --dry-run                          # inspect
python rapsodo/load_db.py --commit                           # write
```

Reaching the Railway Postgres from a laptop needs a TCP proxy enabled on the
Postgres service (there is no CLI command for this — it is a dashboard setting,
Postgres service → Settings → Public Networking). Without it, `DATABASE_URL`
resolves only inside Railway's network and local runs should stay `--dry-run`.

## Railway cron service

Project `feisty-luck` (`3ec152ec-daea-4238-ac38-54f35852db1d`) currently holds:
- `web` — the LIVE hub (`moeller-hub`, branch `main`). **Do not touch.**
- `Postgres` — provisioned 2026-08-19; `web.DATABASE_URL` references it

### DEPLOYED 2026-08-19 — service `rapsodo-cron`

Config lives in **`railway.json` at the repo root**, left as an ordinary **untracked** file.

⚠ **Do NOT add it to `.gitignore` or `.git/info/exclude`.** `railway up` walks the
directory with git's ignore rules — including `.git/info/exclude` — so excluding the file
by either route silently strips it out of the upload. The symptom is a green build that
falls back to the repo's `Procfile` and deploys `gunicorn app:app` instead of the cron job,
with `cronSchedule` empty and nothing ever running. Both mistakes were made here before
landing on "just leave it untracked".

⚠ It must also never be **committed**: on `main` it would override the LIVE hub's start
command with this cron job's. Untracked is the whole balance — it ships, it can't be merged.

```json
{ "deploy": { "startCommand": "python rapsodo/daily.py",
              "cronSchedule": "0 9 * * *",
              "restartPolicyType": "NEVER" } }
```

`restartPolicyType: NEVER` matters: a cron service that restarts on exit would re-run the
whole pull in a loop instead of waiting for the next schedule.

Variables set on `rapsodo-cron`:
- `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` — a *reference*, so no credential is ever
  written into a command or this repo
- `RAPSODO_EMAIL`
- `RAPSODO_BACKFILL_DAYS=365` — **first run only, delete it afterwards** or every nightly
  run re-pulls a full year
- `RAPSODO_PASSWORD` — **must be set by Ian**; the job exits 2 without it

Redeploy with `railway up --service rapsodo-cron` from this directory.

09:00 UTC ≈ 5am ET, chosen to fall after late device uploads.

## Nightly behaviour

Pulls a rolling `RAPSODO_LOOKBACK_DAYS` window (default 3) rather than only
yesterday — devices upload late and can backdate. Re-pulling is idempotent: sessions
dedupe on `(source, source_ref)` and a re-ingest replaces that session's metrics.

**Railway's filesystem is ephemeral**, so `raw/` and `out/` do not survive a redeploy.
That is fine: `load_db.py` also writes each untouched payload into `raw_imports`, which
is what actually persists. Keep the CSV as a convenience, not as the archive.
