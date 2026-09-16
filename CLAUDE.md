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

## Compare within a drill, never across  (hitting)

The same rule as above, other side of the ball. Every Blast swing carries an
**Environment Tag** -- the drill it came from -- and the drill moves the numbers
far more than a swing change does.

Measured on the first real export (2026-09-16, 7,198 swings, 26 hitters), as the
gap between one player's own per-drill means:

| player | metric | drill A | drill B | gap |
|---|---|---|---|---|
| JJ Skeldon | bat speed | tee 53.06 | general practice 64.37 | **11.3 mph** |
| Andy Bennett | on-plane | tee 56.7% | soft toss 68.1% | **11.4 pts** |
| Shane Green | bat speed | machine 57.32 | soft toss 61.97 | 4.7 mph |

`bat_speed`'s minimum meaningful change is **1.5 mph**. An 11.3 mph drill artefact
is seven times the threshold: pooled, it fires as SIGNIFICANT every time a
hitter's cage work shifts from tee to live, and the AI then writes a coherent
explanation of a collapse that never happened.

- `metrics.CONTEXT_SPECIFIC` is **every Blast metric**, built from the registry so
  a metric added later is split by default. Opting one *out* is what should need
  an edit, because pooling is the failure mode.
- **There is no hitting equivalent of release point.** On the pitching side, slot
  is a property of the delivery rather than of a pitch, so release point is
  deliberately pooled. Nothing Blast measures is independent of the drill.
- `swings.context` carries it. NULL = untagged, and an untagged swing contributes
  to **nothing** -- stored, shown, and labelled "Untagged", never folded into a
  real drill. 20% of the first export was untagged. That is a charting-discipline
  problem fixed in the Blast app, not in code.
- `change_events.pitch_type` and `player_baselines.pitch_type` hold **two
  vocabularies**: a pitch code on a pitching row, a drill context on a hitting
  one. One column because the two sides never share a row and a second would put
  a permanent NULL in half of every composite key. **Render with
  `metrics.split_label()`, never `PITCH_TYPE_LABELS` directly.** Both are
  `String(16)` for `"soft_toss"`, not `String(4)` for `"FB"` -- at 4, Postgres
  rejects the write and SQLite silently accepts it, so the bug is production-only.
- The rule holds **all the way to the screen**. Splitting the engine but leaving
  the pages pooled just puts the artefact back where the coach reads it: the
  session log is one row per drill, status tiles match a drill to its own
  baseline, hitter cards report one drill and name it, and a sparkline is drawn
  within one drill (otherwise the line plots the cage plan, not the swing).

`test_blast.py` reproduces the trap and is mutation-tested: setting
`CONTEXT_SPECIFIC = set()` fails five checks.

## Drill usage is a finding in its own right

Exactly as pitch mix is. `summaries.training_drill_mix` computes recent-vs-baseline
drill shares, reported **separately** from swing changes and never merged with them.

It earns its place because a mix shift is invisible to change detection **by
design**: every metric is compared within its own drill, so a hitter who moves
from 87% tee work to 12% fires nothing at all -- correctly, since none of his
drills changed. JJ Skeldon did exactly that between his baseline and recent
windows. Without this report, a real and deliberate change in how a hitter trains
would never reach a coach.

"He is taking live reps now instead of tee work" and "his tee bat speed is up" are
two different statements, and only the second is about his swing.

## Where a swing ranks — Blast percentiles

`percentiles.blast_strip()` puts Blast numbers on the same strip the game and
bullpen percentiles use: **one strip per hitter, drill-adjusted, ranked against
the whole roster.**

### Drill is ADJUSTED FOR here, not SPLIT ON — the opposite of `changes.py`

Read this before "fixing" either one to match the other. They answer different
questions and the right handling genuinely differs:

| | compares | drill is | handling |
|---|---|---|---|
| `changes.py` | a hitter to **himself over time** | a **confound** — his mix shifts, his average moves, his swing didn't | **split**, mandatory |
| `percentiles.py` | a hitter to **other hitters now** | an **offset** — applies to everyone, cancels out of a ranking | **adjust**, then one pool |

**Measured within-player** (a drill's raw mean is confounded by *who* does that
drill — if mostly weak hitters use the tee, its mean is low for roster reasons
and subtracting it over-corrects). Two-way additive fit, player + drill:

```
bat speed        soft toss +1.00   practice +0.79   machine -0.98   tee -2.03
rotational acc   soft toss +1.27   practice +0.27   machine -1.23   tee -1.49
on-plane eff     machine   +1.55   soft toss +0.89  tee      +0.21  practice -1.99
```

About **3 mph end to end** on bat speed. The 11.3 mph figure that justifies the
split in `changes.py` is **one hitter's** (JJ Skeldon's) own tee-vs-live gap; the
other four measured on both drills sit at +2.3, −1.3, +0.4 and +0.3. A single
player's spread is a real fact about *him* — which is exactly why change
detection splits — but it is **not** the population drill effect, and this panel
originally used it as one. That was wrong and is fixed.

Splitting the pool also cost more than it bought: per-drill fields are 10, 10, 6
and 5 hitters, producing ranks like Shane Green's *"0th of 5"* and leaving Ricky
Maschinot's 313 machine swings unrankable. A 5-man percentile is coarser than a
3 mph offset is large.

`MIN_LINKED` guards the estimate: fewer than three hitters spanning two drills
and the adjustment is **skipped and said out loud**, because an offset taken from
drill means alone is confounded by who does them. `test_blast.py` mutation-tests
this — estimating from drill means recovers 2.25 mph against a built-in 6.0.

**Per-drill numbers are still on the page.** The status tiles and the session log
are both per drill, because there a hitter is compared to himself.

### Two pools, never merged

Each hitter's page carries **two separate panels**, and they answer different questions:

| panel | question | population |
|---|---|---|
| **In the cage — Blast** | how does he compare to the guys next to him? | Moeller, drill-adjusted |
| **Against Blast's benchmarks** | is he where Blast says a hitter at his level should be? | the vendor's published bands |

A single blended number would answer neither. `percentiles.blast_benchmark()`
drives the second; `metrics.BLAST_BENCHMARKS` holds the table.

Three rules in that second panel, all mutation-tested:

- **The band follows his level.** Varsity gets the varsity column; **a freshman
  gets the JV column, not Middle School** — that one is for younger players. An
  unknown level falls back to JV, the more forgiving band: better to understate a
  shortfall than invent one against a player whose level we don't actually know.
- **What a miss MEANS depends on the metric, not which side it fell.** Below the
  band is a shortfall on bat speed and *better than the band* on time to contact.
  Target-band metrics are never "short", only "outside".
- **Averaged over ALL his swings, tagged and untagged** — unlike everywhere else
  here — because Blast's bands aren't drill-specific either. That makes this
  figure drill-mix dependent, so a hitter who is >= 50% tee gets a caveat saying
  his numbers read low for that reason and not his swing.

Two caveats are surfaced *in the UI*, not buried in a comment:

- **`BLAST_WIDE`** — attack angle (0–15°) and vertical bat angle (−10 to −40°) are
  wide enough that all 19 measured hitters clear them. The page says passing is
  not evidence of much, so nobody reads 19/19 as good news.
- **`BLAST_PROVISIONAL`** — Blast prints the JV bat-speed band as **"55–56 mph"**,
  not a credible one-mph range. JV duplicates their "Amateur All Levels" column
  exactly for hand speed and power, so that column's 55–65 is used and the bar is
  marked provisional.

`blastconnect.com` now redirects to **WIN Reality** — Blast has been absorbed,
which is the likely reason their blog (varsity 57–71, JV 53–67) disagrees with
the product page (60–70) this table is taken from. **Re-read the table before the
spring**, and if it has moved, `BLAST_BENCHMARKS` is the one place to change.

Blast's **Rotation Score** is on their table and absent here on purpose: it is a
Blast composite the export does not carry, and rotational acceleration is a
different measurement, not a stand-in for it.

### The pool is Moeller, not "Blast standards"

**They are in the repo now** (`metrics.BLAST_BENCHMARKS`, transcribed
2026-09-16), and they sit in their own panel rather than replacing the teammate
ranking. The rule that got them there still stands: a benchmark is used only when
it is sourced. Inventing plausible ones would be worse than having none — a kid told he is
"below the high-school standard" against an unsourced number is being told
something untrue with authority. Real benchmarks go in as a **second, labelled
pool**, never a replacement. Ian's own 2024 Blast Shiny app ranked within Moeller
too. ⚠ `2025\Moeller_Blast\Moeller_Blast.sqlite` holds historical Blast data that
would widen the pool, but it is an unhydrated OneDrive placeholder.

### Seven of thirteen rank; six never will

Reliability was measured first (`blast/reliability.py`: session odd/even split,
Spearman-Brown, at the 25-swing floor actually shipped) and **everything cleared
0.60** — 0.62 to 0.98. Expected, not lucky: these are **measurements of a swing,
not rates over outcomes**, the same reason fastball velocity is decisive on the
pitching side. **Reliability is not the binding constraint here; direction is:**

- attack angle, vertical bat angle, early connection, connection at impact are
  **target-band** metrics. The 99th percentile of attack angle is *bad*. Ranking
  by distance-from-band is worse still: the bands are calibrated on Moeller, so
  it would rank hitters by how average they are and print it as a grade.
- hinge angle and body tilt are **neutral**. No defensible direction, and a gold
  bar is an opinion.

Shown as plain chips with one caption, exactly as release height is. Run
`python blast/reliability.py` before adding a bar — it sweeps four candidate
floors and flags anything shipped under the bar. It splits by **session**, not by
swing: interleaving swings inside one session shares that day's bat, drill and
coach between the halves and inflates r, which is how two game-strip bars came to
read 0.72/0.76 instead of their honest 0.51/0.39.

## Blast specifics

Full loader: **`blast/load_export.py`** (dry-run by default, `--commit` to
write). Exports live in `blast/exports/`, which is gitignored -- ingest keeps
the whole file in `raw_imports`, and that is the copy that survives a redeploy.

⚠ The folder is lowercase **`blast/`**. Windows is case-insensitive, so
`mkdir blast` next to an existing `Blast/` silently reuses it and the import
path only breaks once it reaches Railway's Linux. Check the case, don't assume
the `mkdir` did what it said.

- **There are TWO Blast schemas and they share no column names.**
  `metrics.BLAST_COLUMNS` is the v3 API, from the 2024 R puller
  (`swing_speed.value`); `metrics.BLAST_CSV_COLUMNS` is the "All swings by player"
  CSV export (`Bat Speed (MPH)`). Both are seeded into `column_maps`. Mapping one
  file with the other's keys leaves every metric column unmapped and commits
  nothing -- a silent no-op.
- **`On Plane Efficiency (%)` ships as a 0-1 FRACTION despite the `(%)` header.**
  Its `scale` is 100. Unscaled it lands near 0.7, its 5-point mmc is never cleared
  by anything, and the metric silently never fires for anyone -- which is
  indistinguishable from "nothing changed", the worst failure mode here because an
  empty change list is a legitimate result.
- The export splits the name over `first_name` + `last_name`, hence the
  `player_first` / `player_last` roles in `db.COLUMN_ROLES`.
- `Hinge Angle at Impact` is in the CSV and not the API; `body_rotation` and
  `on_plane_pct` are the reverse. Both registries are right about their own
  source -- do not "reconcile" them.
- **`actiontype` includes `air Swing`** -- a sensor reading with no ball, which
  Blast leaves out of its own averages. Dropped via `ingest.store(row_filter=...)`.
- The `timezone` field contains **embedded newlines**, so the file looks like
  21,594 lines for 7,198 swings. `csv.reader` handles it; `wc -l` does not.
- **Blast sends no session id**, so sessions fall back to `(player, date)`. This
  matters less than it looks because context lives on the *swing*, so a mixed day
  still compares tee-to-tee.
- **Two Blast ID namespaces** live under vendor `blast`: the 2024 puller's
  `insights/{id}` ids (287042-460520) and the CSV's `user_id` account ids
  (810838+). The ranges do not overlap, which is what makes sharing the vendor
  safe; `seed.learn_blast_user_ids` asserts that rather than trusting it.
- **Target bands are calibrated on our own hitters** (Ian's call, 2026-09-16) --
  the IQR of player means over tagged swings. A band calibrated this way says
  "typical for Moeller", **not** "good": it cannot show the team is collectively
  short in a metric, because the middle of whatever we do is in band by
  construction. Useful relative marker, bad absolute one. Replace with an external
  benchmark when there is one worth trusting.

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
python test_blast.py        # Blast / hitting pipeline + swing percentiles
python test_percentiles.py  # the percentile doctrine
python test_rapsodo.py      # Rapsodo pipeline
python test_ingest.py       # CSV ingest
python test_changes.py      # change detection
python test_development.py  # goals / interventions
python smoke_test.py
```

`test_rapsodo.py` and `test_blast.py` are mutation-tested: each check corresponds
to a bug that really happened. If you change pipeline behaviour deliberately, update the test and say
why in the commit — don't delete the check.

## Style

Match the surrounding code. Comments explain *why*, especially where a line looks
arbitrary but is load-bearing (fixed chart axes, `restartPolicyType: NEVER`,
`display:block` on a bar fill). Those are the ones someone will otherwise "clean
up" and quietly break.
