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

Player-dev runs in project **`wonderful-abundance`**: `web` (this branch),
`Postgres`, `rapsodo-cron`, and `blast-cron`. The coaches' hub is a different
project, **`feisty-luck`** — do not deploy this job there.

### Service `rapsodo-cron`

Start command is **`python nightly.py`** (the Rapsodo pull is step 1 of that
job). Schedule `0 9 * * *` (09:00 UTC ≈ 5am ET, after late device uploads).
`restartPolicyType: NEVER` so a finished run waits for the next schedule instead
of looping.

`railway.json` at the repo root, when present, is an ordinary **untracked**
file. Do not commit it, and do not add it to `.gitignore` or `.git/info/exclude`
— `railway up` walks git's ignore rules, and excluding the file makes the build
fall back to the `Procfile` (`gunicorn app:app`, no cron). The schedule and
start command that actually run are the service-instance settings.

Variables on `rapsodo-cron`:
- `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` — a *reference*, so no credential is ever
  written into a command or this repo
- `RAPSODO_EMAIL` / `RAPSODO_PASSWORD` — the job exits 2 without the password
- `RAPSODO_LOOKBACK_DAYS` — optional. Unset, `nightly.py` passes **7** into
  `rapsodo/daily.py`. A direct `python rapsodo/daily.py` still defaults to 3.
  Set this on the service to pin the overnight window (set `7` to make it
  visible; another integer overrides the injected default).
- `RAPSODO_STALE_AFTER_DAYS` — optional, read by `nightly.py`. Unset means
  lookback + 2 (**9** when the overnight lookback is 7). When the more
  recent of the newest session date and the last landed file is older than
  that, the overnight subject is `ATTENTION` and the process exits non-zero. An empty
  Cloud pull is still exit 0 from `daily.py`; this is what keeps a long quiet
  stretch from looking green. Raise it in the off-season if an empty Cloud
  should stay quiet.
- `RAPSODO_BACKFILL_DAYS` — **first historical load only, then delete it**, or
  every night re-pulls that many days
- `GMAIL_APP_PASSWORD`, `ALERT_TO` — the nightly heartbeat

09:00 UTC ≈ 5am ET, chosen to fall after late device uploads.

## Nightly behaviour

The overnight job pulls a rolling window of **7 days** when
`RAPSODO_LOOKBACK_DAYS` is unset (`nightly.py` injects it). A direct
`python rapsodo/daily.py` still defaults to 3. Devices upload late and can
backdate. Re-pulling is idempotent: sessions dedupe on `(source, source_ref)`
and a re-ingest replaces that session's metrics.

An empty window exits 0 — Cloud may legitimately have nothing, including in the
off-season. `nightly.py` still marks the run `ATTENTION` once the more
recent of the newest session date and the last landed file is older than
`RAPSODO_STALE_AFTER_DAYS` (default: that lookback plus 2 days). The subject
also carries the pull's `sessions_new` and `players_active` (or `0 players`).

**Railway's filesystem is ephemeral**, so `raw/` and `out/` do not survive a redeploy.
That is fine: `load_db.py` also writes each untouched payload into `raw_imports`, which
is what actually persists. Keep the CSV as a convenience, not as the archive.
