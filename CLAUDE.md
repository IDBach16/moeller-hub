# Moeller Player Development — project rules

Read this before changing anything here. Most of these rules exist because the
opposite was tried and failed silently.

## What this repo is

`Player_Dev_Hub` is a clone of `IDBach16/moeller-hub` checked out on the
**`player-development-system`** branch. It is **not** the live hub.

- **Live hub** = `Moeller_Hub`, branch `main`, Railway service `web` on project
  `feisty-luck`. Coaches use it. Do not break it.
- **This repo** = the player-development system plus the Rapsodo pipeline.

⚠ **The two branches cannot be merged casually.** `main` declares Flask routes at
module level; this branch uses an app factory with routes nested in a function.
Their agent `SYSTEM` prompts also contradict each other on formatting (main allows
a markdown subset, this branch demands plain text). A merge is a ~350-line
conflict in `app.py` and is its own task — not a step inside another one.

## The two rules that matter most

**1. The database computes. The LLM explains.**
Rolling averages, baselines, deltas, effect sizes, rankings and flags are computed
in SQL or Python and cached. The model receives a compact summary, never raw pitch
rows. A coach question about a fastball retrieves that pitcher's fastball history,
not the season. This is a cost *and* a correctness rule: models are worse at
arithmetic than SQL is.

**2. Nothing is invented.**
- A name that doesn't resolve is **queued in `name_review`**, never attributed to
  a best-guess player.
- A vendor value we can't interpret is **left unmapped and surfaced in QC**, never
  coerced into a plausible-looking one.
- An empty change list means nothing cleared the thresholds. It does **not** mean
  the player isn't improving, and it is not a failure. Say so plainly.
- "Up" is not "good". Some metrics are lower-is-better and some have a target
  band. Trust the registry's `favorable` flag over the sign of the number.
- AI output does not claim causation from co-occurrence, and does not state a
  mechanical change as fact.

## Suggestions are wanted — prescriptions are not

The roadmap (§4) asks the summary for *"suggested areas for coach investigation"*,
so the agent **should** end with one or two ideas. The line is what kind:

**Yes** — things to check, ask, measure or watch, and conditionals tied to the data:
- "Worth asking him whether the release-side move was deliberate; if it wasn't,
  the fastball break gain may not hold."
- "Suggest a checkpoint bullpen inside two weeks; three sessions is thin for
  calling a slot change settled."
- "Worth logging this as an intervention so the next comparison has a reference."

**No** — any instruction about how to move his body: *lower his arm slot, shorten
his stride, change his grip*. The system does not see him throw, has no video and
no biomechanics. It points at what deserves attention and says why; the staff
decides what to do.

If the data is too thin to suggest anything useful, it says so rather than
inventing a suggestion.

## Usage is a finding in its own right

Pitch mix is reported separately from pitch changes, never as one. A pitcher going
7% → 28% sliders is doing something deliberate and a coach should know — but it is
a change in *usage*, not in the slider. `summaries.training_pitch_mix` computes the
recent-vs-baseline comparison so the model is handed the delta rather than counting
pitches itself.

This is also where the bullpen/game cross-reference earns its keep: Glotfelty's
pens ran 82.5% fastball with the sinker dropped entirely, while his game work is
65.6% fastball with 22.1% breaking balls — he wasn't practising the pitches he
throws. No change-detection finding could surface that.

## Compare within a pitch type, never across

A fastball's 15" of ride and a slider's 2" are **different measurements**, not two
samples of one. Pooling them produces a number that moves whenever the pitcher's
*usage* moves, even though no individual pitch changed — and it looks exactly like
a real decline.

Real case (Seth Maybury, 2026-02-24): sliders went 7% → 28% of his work. Pooled,
that fired *"spin efficiency down 17.5 points, SIGNIFICANT"* and *"velocity down
2.7 mph"*. Per pitch type his fastball was flat (velo −0.1, IVB +0.3) and his
slider efficiency had actually **improved** (+3.5). Three of four findings were
artefacts, and the AI summary then built a coherent, wrong story on top of them.

- `metrics.PITCH_SPECIFIC` lists the metrics that must be compared within one
  pitch: velocity, spin rate, IVB, horizontal break, spin efficiency.
- **Release point is deliberately pooled.** Slot is a property of the delivery,
  not of a pitch, and a genuine slot change shows up on every pitch at once —
  which is exactly what Maybury's did (FB +1.16, SL +1.37, CH +2.04 ft).
- Unlabelled pitches contribute to **nothing** rather than polluting a real slot.
- `change_events.pitch_type` and `player_baselines.pitch_type` carry it; baselines
  use `''` for pooled, because NULLs in a composite key don't compare equal.
- Findings must name the pitch. "Horizontal break is up" reads as a fact about the
  pitcher; "Fastball horizontal break is up" is what a coach can act on.

`test_rapsodo.py` has a fixture that reproduces the trap. It is mutation-tested:
setting `PITCH_SPECIFIC = set()` fails it.

## Identity

`players.id` **is** the Moeller Player ID. Never reused, never changed. Everything
joins to it.

- Resolution order: `player_vendor_ids` (stable vendor id) → `player_aliases` →
  normalised name → queue for review.
- **The school roster wins over vendor demographics.** `letsgobigmoe.com` is the
  source of truth for level, class year and position. Rapsodo's self-entered
  fields are unreliable — it had one player's graduation year off by four years.
- **Level lives in `player_seasons`, per season.** Not a column on the player. A
  kid who was JV as a sophomore and varsity as a junior is exactly the progression
  this system exists to show, and a single field would overwrite it irrecoverably.
- **Staff accounts are not players.** `seed_roster.NOT_PLAYERS` denylists the
  Rapsodo coach login, which otherwise appears as the hardest thrower on the team.
- Players on no current roster are created **inactive with no season row**, so
  their history stays queryable without showing up on a team page.

## Rapsodo specifics

Full reference: **`rapsodo/RECON.md`**. The parts that bite:

- The session **LIST** payload (`/v2/session/byPlayerId`) uses `playerId` and has
  **no player object**. The session **DETAIL** payload uses `player_id` and a
  nested `player`. Coding against the wrong one resolves every session to nobody.
- The sessions envelope key is **`sessions`**, not `data` like `/v3/reports`.
- The date parameter is **`beginDate`** at session level, `startDate` at report
  level.
- `shotType` must be pulled for **both** `pitch` and `hit`, or half the data
  disappears without an error.
- **Failed radar tracks come back as ordinary rows with `speed: null`.** Keeping
  them dropped one pitcher's average fastball from 82.5 to 67.0. Always filter.
- `pitchType` is an int enum: `0 FB · 3 CB · 4 SL · 5 SI · 6 CH`. Codes **1 and 2
  are deliberately unmapped** — too few pitches, too ambiguous a shape. Add a code
  only after confirming it against the vendor UI's own aggregates or with a coach.

## Known data limitation — don't try to code around it

About **21% of tracked game pitches are logged only as "Breaking Ball"**, which
could be a slider or a curveball. They cannot be matched to a specific Rapsodo
pitch type after the fact. `normalize_pitch_type` returns `None` for them and the
UI renders them grey.

This is a **charting-input** problem, not an architecture problem. No database
change creates information that was never recorded. Fixing it means charters
tagging Slider vs Curveball, and it only helps data collected afterwards.

## Deployment

Project `feisty-luck` runs three services: `web` (live hub, from GitHub `main`),
`Postgres`, and `rapsodo-cron` (`python rapsodo/daily.py`, `0 9 * * *`).

- **`railway.json` must stay an ordinary untracked file.** `railway up` walks the
  directory with git's ignore rules and honours **both** `.gitignore` and
  `.git/info/exclude` — listing it either way silently strips the deploy config
  from the upload, and the build then falls back to the `Procfile` and deploys
  `gunicorn app:app` with no cron. That failure looks like a green SUCCESS build.
  It must also never be **committed**: on `main` it would override the live hub's
  start command.
- **`railway up` ships the working directory, not a commit.** Verify what you're
  deploying is what's committed.
- The Postgres has **no public endpoint**. Local runs should stay `--dry-run`;
  production loads run inside Railway.
- **A green build is not a working deploy.** Read the job's own log output.

## Running it locally

```
railway run --service web -- sh -c "DATABASE_URL= RAILWAY_ENVIRONMENT= PORT=5055 python app.py"
```

`railway run` injects the `web` service's environment, so `ANTHROPIC_API_KEY`
comes from Railway and never needs copying to a dev machine. The two overrides
are both load-bearing:

- `DATABASE_URL=` — Railway's value points at the **private** Postgres domain,
  which does not resolve off-platform. Empty falls back to local SQLite.
- `RAILWAY_ENVIRONMENT=` — `writes_enabled()` disables every write endpoint when
  this is set, because an open production URL must not accept writes without the
  password gate. Injected unmodified, it silently turns local development
  read-only ("Uploads are disabled because the hub is public").

`app.py` also reads a local `.env` via `_load_dotenv()` (setdefault, never
overwrite — a no-op on Railway), so credentials can live there instead if you
prefer.

⚠ Stopping the wrapper does **not** stop the server: `railway run` spawns Python
as a child, and Flask's debug reloader spawns another. Orphans keep port 5055
bound and the next start silently fails to bind. Kill `python app.py` processes
directly.

## Before you commit

```
python test_rapsodo.py      # Rapsodo pipeline
python test_ingest.py       # CSV ingest
python test_changes.py      # change detection
python test_development.py  # goals / interventions
python smoke_test.py
```

`test_rapsodo.py` is mutation-tested: each check corresponds to a bug that really
happened. If you change pipeline behaviour deliberately, update the test and say
why in the commit — don't delete the check.

## Style

Match the surrounding code. Comments explain *why*, especially where a line looks
arbitrary but is load-bearing (fixed chart axes, `restartPolicyType: NEVER`,
`display:block` on a bar fill). Those are the ones someone will otherwise "clean
up" and quietly break.
