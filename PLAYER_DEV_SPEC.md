# Moeller Player Development System — spec

What we build inside `Moeller_Hub` to turn the roadmap
(`Moeller_Player_Development_AI_Roadmap.docx`, 2026) into a working system.

Written 2026-08-17. This is the document to review **before** any code is written.

---

## 0. The one-paragraph version

The hub stops being a page of links and becomes the product. A Postgres data layer
holds one **Moeller Player ID** per player and every training session that player has
ever had — Blast swings, HitTrax batted balls, Rapsodo pitches, charted bullpens, game
pitches. A nightly job compares each player's recent window to his own prior baseline
and writes the meaningful changes to a small `change_events` table. The Coach Assistant
reads that table — not the raw rows — so a "what changed with this pitcher's fastball"
question costs a few thousand tokens instead of a few hundred thousand. Coaches log
goals and interventions with dates, and the same engine measures pre-vs-post.

---

## 1. Decisions already taken (Ian, 2026-08-17)

| Decision | Choice | Why |
|---|---|---|
| Where the data lives | **New Railway Postgres on the hub service** (`feisty-luck`) | Same `DATABASE_URL` pattern as `Charting_App/db.py`: SQLite locally, Postgres in production. Railway's filesystem is ephemeral — a SQLite file in the repo would lose every goal and intervention a coach entered on the next redeploy. |
| Charting data | Read over the **existing Charting App API**, not by sharing its tables | Keeps the two apps decoupled. The charting Postgres stays the system of record for pitches; the hub caches session summaries. |
| Build order | **Spec first** | Blast / HitTrax / Rapsodo exports aren't in hand yet (Ian is chasing access the week of 2026-08-17). See §3. |
| Stack | Flask + SQLAlchemy Core + Jinja templates | Matches Charting_App, Pitcher_Card, Hitter_Card, Scouting_Agent. |

### 1.1 One structural change to the hub itself

`app.py` is currently 770 lines: one giant HTML string, the login gate, the static
routes, the agent endpoint, and the git-push endpoint all in one file. That does not
survive this build. Proposed layout:

```
Moeller_Hub/
  app.py            # Flask app factory, route registration, auth gate
  db.py             # schema + engine (mirrors Charting_App/db.py)
  metrics.py        # the metric registry (§6) — polarity, units, thresholds
  ingest.py         # CSV upload parsing, column mapping, dedupe
  changes.py        # the What Changed engine (§7)
  agent.py          # Coach Assistant — new tools over the player timeline (§9)
  templates/        # base.html, home.html, player.html, team.html, collect.html, tools.html
  static/           # css, js, images (moved out of the repo root)
```

The existing look — navy `#1a1a2e`, gold `#C5A55A`, glass cards, shield hero — carries
over into `base.html` unchanged. This is a re-plumb, not a re-skin.

---

## 2. Architecture

```
  Blast API / CSV  ─┐
  HitTrax CSV      ─┤
  Rapsodo CSV      ─┼──► ingest.py ──► raw_imports (verbatim JSON, never discarded)
  Charting API     ─┤                       │
  AWRE season CSV  ─┘                       ▼
                                    sessions + swings + pitch_metrics
                                            │
                                            ▼
                                 changes.py  (nightly + on-ingest)
                                            │
                                            ▼
                          player_baselines  +  change_events   ◄── small, pre-computed
                                            │
                        ┌───────────────────┼───────────────────┐
                        ▼                   ▼                   ▼
                 Player Profile      Roster Alerts       Coach Assistant
                    (§8)                 (§8)          (compact context only, §9)
```

The rule from roadmap §9, made concrete: **the LLM never sees a raw swing or pitch
row.** Everything it reads is a summary the database already computed.

---

## 3. What is blocked, and what isn't

| Area | Status | Unblocked by |
|---|---|---|
| Player identity, schema, ingest framework | **Buildable now** | — |
| Hub redesign, nav, player profile shell | **Buildable now** | — |
| Charting + AWRE game data on the profile | **Buildable now** — both sources already work | — |
| What Changed engine | **Buildable now** against charting/AWRE; thresholds for Blast/Rapsodo metrics need real data to calibrate | A few weeks of real sessions |
| **Blast ingest** | Schema **known** (§5.1) — Ian's 2024 R puller documents every field | A current export, or Blast Connect credentials in env |
| **HitTrax ingest** | Column names **unknown**; the existing `tool_hittrax` guesses | One real CSV export |
| **Rapsodo ingest** | Column names **unknown** | One real CSV export |
| Video linking to change events | **Blocked** — needs a clip-addressing scheme from AWRE | AWRE clip URL/ID investigation |
| Strength / force-plate data | Out of scope (roadmap Phase 6) | — |

**Nothing in §4–§9 depends on having the exports.** The schema is deliberately
vendor-agnostic (§5.3) so a missing export delays *data*, not *design*.

---

## 4. The Moeller Player ID

The roadmap's first named deliverable. Definition:

- **`players.id`** — internal integer primary key. This is the Moeller Player ID.
  Never reused, never changes, survives a name change or a transfer.
- **`players.slug`** — stable URL-safe handle (`matt-ponatoski`) for `/players/<slug>`.
  Human-readable links; the integer stays the join key.
- Every external system maps to it through two tables rather than by name matching:

**`player_aliases`** — every spelling this player appears under, per source. AWRE writes
`Ponatoski, Matt`; GCL writes `Matt Ponatoski`; a Rapsodo export might write
`PONATOSKI M`. Each becomes a row. Ingest resolves a name by exact alias lookup first,
then fuzzy match, and anything unresolved lands in a **review queue** rather than being
silently dropped or guessed.

**`player_vendor_ids`** — where a vendor has its own stable ID, we store it and stop
matching on names entirely. Blast already gives us this: the 2024 R puller
(`2025/Moller Misc/Blast_data_moeller3.0.R`) has 35 Moeller players mapped to Blast IDs.
That table is the seed data for this build:

<details>
<summary>Blast Connect player IDs recovered from the 2024 puller (35 players)</summary>

| Player | Blast ID | | Player | Blast ID |
|---|---|---|---|---|
| Charlie Valencic | 437961 | | Connor Cuozzo | 437967 |
| Alex Lott | 438737 | | Matt Ponatoski | 437966 |
| Noah Goettke | 438070 | | Jackson Porta | 437962 |
| Adam Holstein | 408367 | | Donovan Glosser | 322227 |
| Will Schirmer | 437963 | | Camden Broadnax | 309062 |
| Luke Pappano | 437968 | | Brody Foltz | 287042 |
| Logan Rosenberger | 437969 | | Connor Maupin | 460131 |
| Adam Maybury | 438071 | | CJ Gilpin | 460519 |
| Cooper Ridley | 437959 | | Zak Wittenauer | 460184 |
| Griffin Booth | 437958 | | Will Schlake | 460187 |
| Tyler Willenbrink | 438075 | | Kadin Ward | 460186 |
| Kayde Ridley | 438072 | | William Brenzel | 460516 |
| Carter Christenson | 437960 | | Thomas Zimmerman | 460188 |
| Connor Scoggins | 360763 | | John Stallo | 460183 |
| Gunnar Voellmecke | 356423 | | Ronnie Allen | 460520 |
| Jake Bell | 442478 | | Reggie Watson III | 457040 |
| Athan Bridges | 438147 | | Ricky Maschinot | 460518 |
| Teegan Cumberland | 296412 | | | |

These are 2024-era IDs and need verifying against the current Blast roster — but they
prove the mapping exists and give the ingest something real to test against.
</details>

> ⚠ **Security note:** that same R script has the Blast Connect login email and password
> in plaintext, and it sits in a OneDrive folder. When the Blast puller is ported, the
> credentials move to Railway environment variables and the plaintext copy gets removed
> from the script. Worth rotating that password regardless.

---

## 5. Data model

SQLAlchemy Core, same as `Charting_App/db.py`, so identical DDL runs on SQLite (local)
and Postgres (production).

### 5.1 Identity

```
players
  id            int PK            -- the Moeller Player ID
  slug          str  unique
  first_name    str  not null
  last_name     str  not null
  class_year    str               -- '2027'
  primary_pos   str
  bats          char(1)           -- R / L / S
  throws        char(1)           -- R / L
  is_pitcher    bool
  is_active     bool
  created_at    timestamp

player_aliases
  id            int PK
  player_id     int FK -> players.id
  source        str               -- 'awre' | 'gcl' | 'charting' | 'blast' | 'hittrax' | 'rapsodo'
  alias         str               -- the exact string that source uses
  UNIQUE (source, alias)

player_vendor_ids
  id            int PK
  player_id     int FK -> players.id
  vendor        str               -- 'blast' | 'hittrax' | 'rapsodo' | 'charting'
  vendor_id     str
  UNIQUE (vendor, vendor_id)
```

### 5.2 Sessions

One row per *training or competition event*. Everything measured hangs off a session —
the lesson learned in the Charting App, where a date alone couldn't tell two bullpens
on the same day apart.

```
sessions
  id            int PK
  player_id     int FK -> players.id
  session_date  date not null
  session_type  str  not null     -- see vocabulary below
  source        str  not null     -- 'blast' | 'hittrax' | 'rapsodo' | 'charting' | 'awre'
  source_ref    str               -- vendor session id / game key, for dedupe
  purpose       str               -- 'baseline' | 'development' | 'checkpoint' | 'intervention' | 'competition'
  notes         text
  import_id     int FK -> raw_imports.id
  created_at    timestamp
  UNIQUE (source, source_ref)     -- re-uploading the same export is idempotent
```

`session_type` vocabulary: `bullpen`, `live_ab`, `cage`, `tee`, `front_toss`,
`machine`, `scrimmage`, `intrasquad`, `game`.

`purpose` is what makes the roadmap's protocols (§10) measurable — a `baseline` session
is what everything else is compared against.

### 5.3 Measurements — the vendor-agnostic part

This is the answer to "we don't have the exports yet." Two layers:

**Layer 1 — `raw_imports`.** Every uploaded file is stored whole, before any parsing:
filename, vendor, uploaded-by, SHA-256 (so the same file can't be double-counted), the
detected header row, and the row count. Nothing is ever thrown away, so when a column
turns out to have been misread six weeks later, we re-parse rather than re-collect.

**Layer 2 — typed measurement tables.** Two tables, one per side of the ball, both
narrow (long-format) rather than wide. A new Rapsodo or Blast metric is a **row**, not a
schema migration:

```
swings                          pitch_metrics
  id           int PK             id           int PK
  session_id   int FK             session_id   int PK
  player_id    int FK             player_id    int FK
  seq          int                seq          int
  ts           timestamp          ts           timestamp
  metric_key   str                pitch_type   str        -- normalized (§6.3)
  value        float              metric_key   str
                                  value        float
```

Why long format: we do not know HitTrax's or Rapsodo's column list. A wide table would
need a migration for every surprise column; a long table absorbs them. The **metric
registry** (§6) decides which keys are meaningful, and unknown keys are stored but not
surfaced until someone registers them.

Indexes: `(player_id, metric_key, ts)` on both — that's the access pattern for every
baseline and trend query.

### 5.4 The mapping table — why the missing CSVs don't block the build

```
column_maps
  id            int PK
  vendor        str
  source_column str               -- exactly as it appears in the export header
  metric_key    str               -- our canonical key, or 'player'/'date'/'session'/'ignore'
  unit          str
  scale         float default 1   -- unit conversion (e.g. m/s -> mph)
  confirmed_by  str
  confirmed_at  timestamp
  UNIQUE (vendor, source_column)
```

**Column mapping is data, not code.** When Ian's first HitTrax export lands, the
Data Collection page shows every unrecognized header with a dropdown of canonical
metrics; a coach (or Ian) confirms them once and the mapping persists. No redeploy, no
code change, no waiting on me. The `hittrax.csv` guess-the-column logic currently in
`agent.py` is replaced by this.

Blast ships pre-seeded because we already know its schema from the R puller:

| Blast field | `metric_key` | Unit |
|---|---|---|
| `swing_speed.value` | `bat_speed` | mph |
| `peak_hand_speed.value` | `peak_hand_speed` | mph |
| `bat_path_angle.value` | `attack_angle` | deg |
| `vertical_bat_angle.value` | `vertical_bat_angle` | deg |
| `planar_efficiency.value` | `on_plane_efficiency` | % |
| `rotational_acceleration.value` | `rotational_acceleration` | g |
| `early_connection.value` | `early_connection` | deg |
| `connection.value` | `connection_at_impact` | deg |
| `body_rotation.value` | `body_rotation` | % |
| `body_tilt_angle.value` | `body_tilt` | deg |
| `power.value` | `power` | kW |
| `time_to_contact.value` | `time_to_contact` | s |
| `commit_time.value` | `commit_time` | s |
| `on_plane.value` | `on_plane_pct` | % |
| `created_at.date` | *(session date)* | — |
| `player_id` | *(vendor id)* | — |

### 5.5 Development records

```
goals
  id            int PK
  player_id     int FK
  metric_key    str               -- optional: a goal can be measurable or narrative
  direction     str               -- 'increase' | 'decrease' | 'target_band'
  target_value  float
  title         str  not null
  detail        text
  set_by        str               -- coach name
  set_on        date
  review_on     date
  status        str               -- 'active' | 'met' | 'abandoned' | 'superseded'

interventions
  id            int PK
  player_id     int FK
  intervention_date date not null
  category      str               -- 'grip' | 'pitch_shape' | 'bat_path' | 'drill' | 'approach' | 'strength' | 'mechanical_cue'
  title         str  not null
  detail        text
  coach         str
  goal_id       int FK -> goals.id   -- nullable
  review_on     date
  outcome       str               -- 'pending' | 'working' | 'no_change' | 'reverted'
```

Roadmap §5's requirement — *date, player, coach, goal, intervention, review date* — is
exactly these columns. The `intervention_date` is what lets §7.4 cut the data into
pre/post windows automatically.

### 5.6 Computed tables (what the AI actually reads)

```
player_baselines                      change_events
  player_id    int                      id           int PK
  metric_key   str                      player_id    int
  window_start date                     metric_key   str
  window_end   date                      detected_on  date
  n            int                       direction    str    -- 'up' | 'down'
  mean         float                     recent_mean  float
  sd           float                     baseline_mean float
  p25/p50/p75  float                     delta        float
  computed_at  timestamp                 effect_size  float  -- delta / baseline sd
  PK (player_id, metric_key, window_end) severity     str    -- 'notable' | 'significant'
                                          favorable    bool
                                          n_recent     int
                                          summary      str   -- one plain-English line
                                          acknowledged bool
                                          intervention_id int FK  -- nullable
```

`change_events.summary` is pre-written by the engine, e.g.
`"FB velo 86.4 vs 84.9 baseline (+1.5 mph) over 3 sessions"` — which is precisely the
compact context the roadmap's §9 example asks for.

---

## 6. The metric registry (`metrics.py`)

A single Python dict — the source of truth for how every metric behaves. Nothing
downstream hard-codes a threshold.

```python
Metric(
  key="fb_velocity",
  label="Fastball velocity",
  unit="mph",
  side="pitching",
  polarity="higher_better",   # higher_better | lower_better | target_band | neutral
  target_band=None,
  mmc=0.8,          # minimum meaningful change — below this it's noise
  min_n=15,         # pitches/swings needed before a window counts
  sources=["rapsodo", "charting", "awre"],
)
```

### 6.1 Polarity matters more than it sounds

Not every metric is "up good." `attack_angle` wants a **band** (roughly 5–15°, to be
calibrated on our own hitters, not on internet numbers). `time_to_contact` is
lower-better. `vertical_bat_angle` is a band that depends on pitch height. The engine
must not congratulate a hitter for an attack angle that climbed from 12° to 22°.

**Every band value in the first build is a placeholder** and gets calibrated once we
have a season of our own data. The spec's commitment is that the numbers live in one
file and are trivially editable — not that the initial numbers are right.

### 6.2 Minimum meaningful change (`mmc`)

The guard against a dashboard that cries wolf. Starting placeholders, to be revised:

| Metric | `mmc` | Notes |
|---|---|---|
| `fb_velocity` | 0.8 mph | Rapsodo/AWRE agreement is roughly ±0.5 |
| `induced_vertical_break` | 1.0 in | |
| `horizontal_break` | 1.0 in | |
| `spin_rate` | 100 rpm | |
| `bat_speed` | 1.5 mph | Blast session-to-session noise is real |
| `on_plane_efficiency` | 5 pts | |
| `attack_angle` | 2.0 deg | |
| `exit_velocity` | 1.5 mph | HitTrax, avg over session |
| `max_exit_velocity` | 2.0 mph | |
| `strike_pct` | 5 pts | |

### 6.3 Pitch-type normalization

Rapsodo, the Charting App, and AWRE all name pitches differently, and the roadmap's
protocol section specifically asks for consistent pitch-type labels. One canonical
vocabulary — `FB, SI, CT, SL, CB, CH, SP` — with a mapping table per source. Charting's
`Fastball/Sinker/Curveball/Slider/Changeup/Splitter` and AWRE's labels both fold into it.
Anything unmapped shows up in the Data Collection QC list.

---

## 7. The What Changed engine (`changes.py`)

Runs on every ingest and nightly. Pure SQL + pandas. **No LLM involvement.**

### 7.1 Windows

- **Recent window** — the player's last `k` sessions for that metric (default `k = 3`),
  requiring at least `min_n` observations total.
- **Baseline window** — sessions in the 120 days *before* the recent window starts, with
  the same `min_n` floor. If the player has a session marked `purpose = 'baseline'`, that
  session is weighted in and reported as the reference point by name.
- Windows are per *(player, metric)*, not global — a pitcher who threw twice in six weeks
  and one who threw twelve times get compared to their own histories, not to a calendar.

### 7.2 Firing a change

A `change_event` is written only when **all** of these hold:

1. `n_recent >= min_n` **and** `n_baseline >= min_n` — otherwise `insufficient_data`,
   which the profile shows honestly rather than hiding.
2. `abs(delta) >= mmc` — bigger than known measurement noise.
3. `abs(effect_size) >= 0.5` where `effect_size = delta / baseline_sd` — bigger than this
   player's own variability. Guards against a metric whose `mmc` is too generous.
4. Welch's t-test `p < 0.10` — a loose bar on purpose. This is a coach's attention
   queue, not a paper.

`severity = 'significant'` when `effect_size >= 0.8` and `p < 0.05`; `'notable'`
otherwise. `favorable` is derived from the metric's polarity, not from the sign.

### 7.3 Suppressing repeats

A player whose velo genuinely jumped shouldn't generate the same alert for six weeks.
Once a change fires, the baseline is **re-anchored** to include the recent window, so the
next comparison is against the new normal. Acknowledged events drop off the roster feed
but stay on the player's timeline as history.

### 7.4 Intervention pre/post

Given `interventions.intervention_date`, the engine runs the same comparison with hard
edges: `pre` is the sessions in the 90 days before the date, `post` is everything on or
after it. It reports on the intervention's linked goal metric plus any metric that moved,
and writes the result back to `interventions.outcome` — `working`, `no_change` or
`reverted`, judged on the goal metric where one is linked. This is the roadmap's
*"measure whether the development work worked."*

> **Implemented deviation (2026-08-17):** this section originally specified a 30-day
> washout — "everything ≥30 days before the date is pre". At high-school session
> frequency that usually discards *all* the pre-intervention data, leaving nothing to
> compare against. `changes.PRE_WINDOW_DAYS` is the knob; add a washout if sessions ever
> get dense enough to afford one.

The engine reports the comparison. It does **not** claim causation, and the UI wording
reflects that — coaches own the interpretation.

### 7.5 A note on the p-value

Individual swings and pitches within one session are not independent, so a t-test over
raw observations gives an optimistic p. That is why p is the **loosest** of the four
gates: the minimum-meaningful-change and effect-size gates are what actually decide
whether a change fires, and p only breaks ties. Treat it as a filter, not as evidence.

The t-distribution is computed in `changes.py` from the regularized incomplete beta
function rather than by adding scipy for one p-value. It is verified against published
t-tables and a worked Welch example in `test_changes.py`.

---

## 8. Hub redesign

### 8.1 Navigation (roadmap §4)

| Nav item | Route | Contents |
|---|---|---|
| **Players** | `/players`, `/players/<slug>` | Roster grid → unified player profile |
| **Team Development** | `/team` | Roster-wide trends, the alert feed, protocol compliance |
| **Game Prep** | `/prep` | Scouting Agent, Synergy Agent, Umpire Cards, Team Stats |
| **Video** | `/video` | AWRE Video Search, Pitch Overlays |
| **Data Collection** | `/collect` | Uploads, column mapping, session entry, QC queue |
| **Tools** | `/tools` | The current 8 cards, unchanged, moved here |

The existing cards are not deleted or rebuilt — roadmap §1 is explicit that the current
tools become infrastructure. They move from the front page to `/tools`.

### 8.2 New home page

Replaces the current hero-then-cards layout:

1. Shield hero, shortened — it currently eats 70vh before anything useful appears.
2. **Player search** as the primary control. Type-ahead over the roster; picking a
   player goes straight to his profile.
3. **Ask the Player Development AI** box, directly beneath.
4. **What changed this week** — the unacknowledged `change_events` feed, most severe
   first, each linking to the player and the evidence.
5. **Needs review** — players with sessions ingested since a coach last opened them, and
   interventions whose `review_on` has passed.
6. Tool tiles drop below the fold or move entirely to `/tools`.

### 8.3 The unified player profile — `/players/<slug>`

Roadmap §4's eight blocks, in order:

1. **Header** — name, class, B/T, position, latest session date, days since last data.
2. **Current status** — a metric tile row appropriate to the player's side of the ball;
   each tile shows the current value, the baseline, and the delta with polarity coloring.
3. **What Changed** — the player's `change_events`, newest first, each with its window,
   sample size, effect size, and plain-English summary. This is the headline block.
4. **Training history** — a session timeline (Blast/HitTrax for hitters,
   Rapsodo/bullpen for pitchers) with per-session sparklines per registered metric.
5. **Game performance** — the existing AWRE season data and Team Stats numbers, embedded
   from what already works today.
6. **Video evidence** — clips tied to sessions and change events. *Blocked; see §3.*
   Ships as a placeholder panel that links out to AWRE Video Search filtered by player.
7. **Development goals** — active goals with progress against `target_value`.
8. **Interventions** — the log, with pre/post results attached once §7.4 has enough data.
9. **AI summary** — a cached, on-demand paragraph. Regenerated only when new data
   arrives (§9.3), never on every page load.

### 8.4 Data Collection — `/collect`

The page that makes the roadmap's protocols actually happen:

- **Upload** — drag a Blast / HitTrax / Rapsodo export in. The parser reports what it
  recognized, what it didn't, and how many rows are new vs. already imported.
- **Column mapping** — unresolved headers with dropdowns. Confirm once, stored forever.
- **Name review queue** — export names that didn't resolve to a Moeller Player ID, with
  suggested matches to accept or reject. Nothing is auto-guessed into the database.
- **Session entry** — log a session's `purpose` and notes when the export doesn't carry
  it, plus intervention logging.
- **Protocol compliance** — who is missing a baseline, whose last Rapsodo bullpen is
  older than the protocol's 1–2 weeks. Roadmap §5 becomes a checklist, not a hope.

### 8.5 API

```
GET  /api/players                        roster (search, filters)
GET  /api/players/<id>                   profile header + current status
GET  /api/players/<id>/timeline          sessions, paged
GET  /api/players/<id>/changes           change_events
GET  /api/players/<id>/metric/<key>      series for a chart
GET  /api/changes                        roster-wide alert feed
POST /api/import                         file upload -> raw_imports
POST /api/import/<id>/map                confirm column mappings
POST /api/import/<id>/commit             parse into sessions/swings/pitch_metrics
POST /api/goals            /api/goals/<id>
POST /api/interventions    /api/interventions/<id>
GET  /api/protocol-status                compliance view
```

Write endpoints require the password gate to be on — see §11.

---

## 9. The Coach Assistant, rebuilt

Today's `agent.py` has six tools over three sources and answers lookup questions. The
roadmap wants it analyzing the connected history instead.

### 9.1 New tools

| Tool | Returns | Approx. tokens |
|---|---|---|
| `find_player` | Resolves a name to a Moeller Player ID + basics | ~100 |
| `player_snapshot` | Current status: key metrics, baselines, deltas, last session | ~600 |
| `what_changed` | That player's `change_events` for a window | ~400 |
| `metric_history` | One metric, session-level means — **not** raw rows | ~500 |
| `compare_windows` | Arbitrary two-window comparison, computed in SQL | ~300 |
| `goals_and_interventions` | Active goals, recent interventions, pre/post results | ~400 |
| `roster_alerts` | Team-wide changes needing review | ~800 |
| `protocol_status` | Who's missing baselines / overdue sessions | ~400 |

Existing `season_pitching`, `season_batting`, `charting_report`, `team_stats` stay —
they're the game-performance layer.

### 9.2 The token discipline (roadmap §9)

Rules built into the tools, not left to the model's judgment:

- **No tool returns raw rows.** Every one returns aggregates the database computed. The
  ceiling is enforced in code: results are truncated to 30 KB before reaching the model,
  as they are today.
- **Session-level, not pitch-level.** `metric_history` returns one row per session with
  n / mean / sd. A pitcher with 900 Rapsodo pitches over 12 sessions returns 12 rows.
- **The system prompt stays cached** (`cache_control: ephemeral`, already in place).
- **Conversation trimming stays** at the last 16 messages, already in place.
- **Cached summaries.** A player's AI summary is stored with the hash of his latest
  session id; unchanged data returns the cached text with no API call at all.

Worked example, the roadmap's own — **measured, not estimated** (2026-08-17, against a
fixture holding 4,800 individual pitch measurements for one pitcher):

> *"What has changed with this pitcher's fastball over the last three sessions?"*
> → `find_player` → `what_changed` → **372 characters, ~93 tokens**:
> `Fastball velocity 86.4 vs 85.0 baseline (+1.4 mph) over 3 sessions — significant,
> favorable, effect size 1.43, 900 recent vs 1500 baseline observations.`
>
> Sending the raw rows instead would be roughly **48,000 tokens**. That is a **516×**
> reduction, and the model never touches a single pitch.

`test_agent.py` asserts a size ceiling on every tool's payload, so this property is
regression-tested rather than assumed. Measured payloads: `player_snapshot` 328 chars,
`what_changed` 264, `metric_history` 575 (nine sessions standing in for 900 pitches),
`compare_windows` 567, `goals_and_interventions` 576, `roster_alerts` 325,
`protocol_status` 229.

### 9.3 Weekly summaries

A scheduled job (same pattern as the existing pipeline tasks) runs Sunday night: for
each player with new data that week, feed the pre-computed change events through the
model once and store the paragraph. Coaches read cached text; nobody pays per view.

Rate limits stay as they are (8/min, 80/day per IP) — they're the only thing standing
between an open endpoint and a surprise bill.

---

## 10. Protocols, as enforced by the system (roadmap §5)

The protocols are Ian's and the staff's to set; the system's job is to make compliance
visible. What `/collect` tracks:

| Protocol | System check |
|---|---|
| Every pitcher has a preseason Rapsodo baseline | Player has a `purpose='baseline'` pitching session in the current cycle |
| Structured Rapsodo every 1–2 weeks in development | Days since last `rapsodo` session vs. threshold |
| Increased frequency during an active intervention | Open intervention with fewer than N post-sessions logged |
| In-season checkpoints | Sessions with `purpose='checkpoint'` per month |
| Consistent labels | Pitch types that failed normalization (§6.3) surface in the QC list |
| Hitters have Blast + HitTrax baselines | Same baseline check, hitting side |
| Blast and HitTrax collected together | Sessions on the same date for the same player, flagged when only one exists |

None of this nags a coach mid-session. It's a page you open, not a notification.

---

## 11. Security and access

- The password gate (`HUB_PASSWORD`) is **off** as of 2026-08-14. That's fine for a page
  of links; it is **not** fine for a page holding player development records and coach
  notes. **Recommendation: turn the gate back on before any write endpoint ships.**
  Roster names and metrics on an open URL is a different thing from a link directory.
- The `/api/git-push` endpoint already self-disables when the gate is off. Every new
  write endpoint follows the same rule.
- Blast credentials → Railway env vars, never the repo (§4).
- `raw_imports` retains uploaded files; they contain minor-athlete performance data.
  Retention and access should be a deliberate decision, not a default.

---

## 12. Build order

**Status as of 2026-08-17: A–F built and tested, not yet committed or deployed.**
Five suites, all green (389 checks) — `smoke_test.py` (74), `test_ingest.py` (71),
`test_changes.py` (63), `test_development.py` (68), `test_agent.py` (113).
Everything that isn't blocked on vendor data or AWRE clip addressing is built.

| Phase | Work | Status | Depends on |
|---|---|---|---|
| **A** | Repo restructure (§1.1); `db.py` + schema; roster + Blast vendor IDs seeded | **done** | Ian still to add Postgres to `feisty-luck` |
| **B** | Ingest framework: `raw_imports`, `column_maps`, name review queue, `/collect` | **done** | A |
| **C** | Nav + home redesign; `/players` roster; unified player profile | **done** | A |
| **D** | What Changed engine + `player_baselines` / `change_events`; alert feeds | **done** | real sessions to calibrate |
| **E** | Goals + interventions UI; pre/post comparison | **done** | A, C |
| **F** | Coach Assistant retooled onto the timeline (§9); cached summaries; weekly job | **done** | D |
| **G** | Video linking | blocked | AWRE clip-addressing investigation |
| **H** | Blast API puller in Python; scheduled refresh | blocked | Blast credentials in env |

D and E are built but only become *useful* once real sessions exist — both are
deliberately silent rather than speculative when the data isn't there yet.

### 12.1 Still outstanding for Ian

1. **Add the Postgres plugin to `feisty-luck`.** Until `DATABASE_URL` exists, the hub
   falls back to a local SQLite file that Railway wipes on redeploy. `/api/health`
   reports which backend is live.
2. **Decide on the password gate** (§11). Write endpoints self-disable in production
   while it's off, so `/collect` and the goal/intervention forms are read-only until
   `HUB_PASSWORD` is set.
3. **Get the three exports** (§13) — still the gate on everything data-shaped.
4. **Calibrate the thresholds in `metrics.py`** once a season of our own data exists.
   Every `mmc` and both target bands are placeholders.
5. **Schedule two jobs** alongside the existing pipeline tasks:
   `python changes.py` nightly, and `python summaries.py` Sunday night (§9.3).
   Both are safe to run repeatedly — detection suppresses repeats, and summaries
   return cached text without an API call when nothing moved.

---

## 13. What Ian needs to get this week

Concrete asks, in priority order:

1. **One real HitTrax CSV export** — full history if the app allows it, one session if
   not. What it settles: exact column names, whether exports are cumulative or
   incremental, how players are identified, whether session IDs exist. The support email
   draft in `HitTrax/support_email_draft.txt` already asks the right questions.
2. **One real Rapsodo CSV export** — ideally a bullpen with several pitch types. Settles
   the same four questions plus Rapsodo's pitch-type labels for §6.3.
3. **One current Blast export**, or the Blast Connect credentials for env vars.
   Settles whether the 2024 player IDs are still valid and whether the v3 API shape has
   changed.
4. **Confirm the roster** — who's on the 2027 team, class years, B/T, positions. This
   seeds `players` and everything hangs off it.
5. **A decision on the password gate** (§11).

Items 1–3 unblock ingest. Item 4 unblocks everything. None of them block phases A–C.

---

## 14. Open questions

1. **Session granularity for HitTrax and Rapsodo.** If exports carry no session ID, we
   fall back to grouping by (player, date) — which reintroduces the exact two-bullpens-in-
   one-day ambiguity the Charting App was built to avoid. Worth checking whether the
   exports carry a session or round identifier.
2. **Which metrics coaches actually want on the profile's status row.** The registry can
   hold thirty; the tile row should show five or six. Ian and the staff pick.
3. **How far back to backfill.** Blast has 2024 data. Is a 2024 season worth importing
   for players still on the roster, or do we start clean at the 2027 preseason?
4. **Who can write.** Does every coach log interventions, or a smaller group? Determines
   whether we need per-user accounts rather than one shared password.
5. **Target bands** (§6.1) — calibrate on our own hitters, or start from published
   ranges and adjust? Recommend our own, once a season of data exists.
6. **Does AWRE expose stable per-pitch clip URLs?** Determines whether §8.3's video block
   is a real feature or a filtered link-out. Needs an hour of investigation against the
   AWRE Event Payload endpoint.
