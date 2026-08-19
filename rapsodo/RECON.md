# Rapsodo Cloud — API Recon

Living record for the Rapsodo pull pipeline. Same role as `_pipeline\KNOWLEDGE_BANK.md`.

## Status
- **2026-08-19** — Phase 0 recon COMPLETE. Full 3-call chain mapped and verified 200.
  **No CSV scraping and no browser automation needed** — the JSON API is strictly richer
  than the CSV export (85 fields/pitch incl. full 3D position/velocity/acceleration
  vectors and video URLs).

## Platform
- Host: `https://cloud.rapsodo.com` ("Diamond Sports Cloud Platform")
- Login page: `https://cloud.rapsodo.com/login`
- Account in use: `david.cydrus@gmail.com` / `david cydrus`, userType Coach, coach id `4095621`
- Single-page app. Hitting an API path via normal browser navigation returns the SPA's
  "Page Not Found" shell — the API is XHR-only. Must send the auth header explicitly.
- **API version is mixed**: player/report endpoints are `/v3/`, session + shot endpoints
  are `/v2/`. Don't assume one prefix.

## Auth (CONFIRMED)
- JWT stored in `localStorage["dcv3_token"]` as `{"created": <ms>, "data": "JWT eyJ..."}`
- The `data` value **already carries the `JWT ` prefix** — pass it through verbatim:
  `Authorization: JWT eyJhbGciOiJIUzI1NiIs...`
  (Not `Bearer`. Do not add a second `JWT `.)
- Claims: `_id, userType, email, lpAuth, authType, iat, exp`
- **Token lifetime = 30 days** (observed iat 2026-08-18 → exp 2026-09-17).
  A daily job reuses a cached token and only re-authenticates ~monthly.
- Session cookies present are analytics only (`_ga`, `_hj*`, `__stripe_*`) — **not** auth.
  Cookie-jar-only auth will NOT work.

## The pull chain (all CONFIRMED 200)

### 1. Players with activity in a window
```
GET /v3/reports
  ?startDate=<epoch_sec>&endDate=<epoch_sec>
  &orderBy=lastSessionDate&orderType=desc
  &currentPage=1&pageSize=100
```
- Epoch **SECONDS**. `pageSize` is caller-controlled (UI sends 10; larger works).
- Response: `{ success, data: [...], totalCount }` — paginate off `totalCount`
  (NOT `total`/`totalPages`).
- Each row is player-keyed: `player.{_id, userId, email, firstName, lastName, ballType,
  height, weight, playingLevel, coachIds[], dateOfBirthday, status, highSchoolGradYear,
  highSchoolId, highSchoolName, createdAt}` plus aggregate counts.
- UI "Last 1 Year" = `startDate` exactly 365d back (observed 1755576000 → 1787198399).

### 2. Sessions for one player
```
GET /v2/session/byPlayerId/<playerId>
  ?shotType=pitch|hit
  &beginDate=<epoch_sec>&endDate=<epoch_sec>
  &sessionTypes=&hitPlacements=&deviceName=
```
- ⚠ **GOTCHA: the param is `beginDate` here, not `startDate`.** Using `startDate` at this
  level silently returns the wrong window rather than erroring.
- ⚠ **`shotType` must be pulled TWICE** — `pitch` and `hit` are separate result sets.
  The UI exposes this as the Hitter/Pitcher toggle. Pulling only one silently loses half.
- ⚠ **Envelope is `{success, sessions, dataCount}`** — the list is under **`sessions`**,
  NOT `data` like `/v3/reports`. Reading `data` here returns nothing, silently.
- Empty `sessionTypes` / `hitPlacements` / `deviceName` = no filter.
- As of 2026-08-19 there are **zero `hit` sessions** in the last year — all 132 are
  pitching. The hit path is written but has never returned a row, so its shot schema
  is still unverified.

### 3. Per-shot rows for one session
```
GET /v2/shots/pitch/bySessionId/<sessionId>?playerId=<playerId>
GET /v2/shots/hit/bySessionId/<sessionId>?playerId=<playerId>
```
- Response: `{ success, dataCount, shots: [...] }` — top key is **`shots`**, not `data`.
- **85 fields per pitch.** `_id` = `"<playerId>@<pitch_id>"` → natural primary key,
  makes re-running a day idempotent for free.

Pitch fields:
`_id, pitch_id, speed, spin, trueSpin, pitchType, spinEfficiency, releaseHeight,
releaseSide, spinAxisDegree, horizontalBreak, horizontalSSWBreak, verticalBreak,
verticalSSWBreak, weatherInput{Elevation,Temperature,Pressure,Humidity}, launchAngle,
horizontalAngle, mode, gyroDegree, strikeZoneX, strikeZoneY, pitcherPovStrikeZoneX,
speedConfidence, note, isValidatedByUser, hardwareModel, position{X,Y,Z},
speed{X,Y,Z}, acceleration{X,Y,Z}, timeStart, spinAxis{X,Y,Z}, seamOrientation,
drawTrajectory, ballType, environment, shotID, timestamp, appName, appVersion,
isDiamond, sessionID, lpVideoUrl, lpIsVideoExist, hasVideoFile, packageVersion,
spinConfidence, debugInfo.*Confidence (18 of them), isVideoCaptured, isVideoAvailable,
spinAxis, totalBreak, strikeZoneBreakdown, isStrike, strike, isValidForStrike,
confidences, videoInfo`

### Supporting
- `GET /v2/session/types` — session-type taxonomy lookup
- `GET /v3/player/<playerId>` — player detail
- `GET /v3/user/contact-info` — logged-in user

## ⚠ The session-LIST payload is NOT the session-DETAIL payload
This bit us on the first load — every session resolved to `player=None`.

`/v2/session/byPlayerId/...` (the list) returns:
`_id, id, object_id, playerId, date, startedAt, finishedAt, sessionName, sessionType,
shotType, pitchCount, hitCount, shotCount, videoCount, shotIds, shots, stats, tags,
deviceName, appName, mode, isLiveOnLive, isCertified, distance, fieldSize, ...`

- the player key is **`playerId` (camelCase)** — NOT `player_id`
- there is **no nested `player` object at all** — no name, no email

`/v3/session/pitch/<id>` (the detail) uses `player_id` AND a nested `player{}`. Different
shapes, same concept. `pull.py` therefore stashes the `/v3/reports` player record at the
top level of each archived file (`{"player":…, "session":…, "shots":…}`) so the archive is
self-describing and can be re-loaded without re-querying the roster.

## Session metadata (from `/v3/session/pitch/<id>?playerId=<id>`)
`_id, objectID, date (epoch sec), total_records, total_videos, sessionName, sessionType,
shotType, startedAt, player_id, coaches[], isLiveOnLive, isCertified, appName,
deviceName, distance, mode, fieldSize, pitchingMoundDistance, player{...}`
- NOTE: this endpoint returns metadata ONLY (`total_records: 0`, no shot array).
  Shots must come from the `/v2/shots/...` call above.

## Session type taxonomy (from the Filters UI)
Pitching Machine, HomeRunChallenge, BattingPractice, Ladder, Drill, Live On Live,
Showcase, Scripted Session, Live, Live on Live BP with a Pitching Machine, Tee Session,
Live Batting Practice, Soft Toss/Front Flips, Rehab, Flat Ground, Pitch Design,
Low Intent, High Intent.
Hit Placements: BP, Soft Toss, Tee, Live.

## Backfill inventory (measured live 2026-08-19, window 2025-08-19 → 2026-08-19)
Baseline to check the first real load against — if the numbers come back wildly
different, something is wrong with the pull, not with Rapsodo.

| | |
|---|---|
| Players | 28 |
| Sessions | 132 — **all `pitch`, zero `hit`** |
| Actual date span | 2025-09-25 → 2026-03-24 |
| Shots returned | 2,512 |
| Valid shots (speed non-null) | **2,402** |
| Dropped failed tracks | **110 (4.4%)** |
| `pitch_metrics` rows expected | **~18,176** (incl. 1,692 `fb_velocity`) |
| Session types | High Intent 120, Pitch Design 12 |

Pitch mix: FB 1,692 · SL 322 · CH 292 · CB 59 · SI 23 · unmapped 14 (codes 1 and 2).
Top workloads: Shane Green 190, Jonathan Sommers 190, Miles Bessenbach 183,
William Brenzel 161, Seth Maybury 157, CJ Gilpan 150.

QC to expect on first load:
- **`david cydrus` (Rapsodo id 735213)** appears as a player with 1 session — that's the
  coach account. It will queue as unresolved rather than create a player.
- **Miles Bessenbach is grad year 2024** (alumni) with 183 pitches — needs a roster call.
- `player_vendor_ids` has no `rapsodo` rows yet, so all 28 resolve by name on the first
  load. Seed the 28 Rapsodo ids → Moeller Player IDs so name matching never runs again.

## Data observed
- Moeller roster, real: Eli Singer, Colton Smith, Jireh Garnica, Shane Green,
  Jonathan Sommers, Miles Bessenbach, ...
- highSchoolName "Moeller", highSchoolId `5e85ade7955148a12482d30c`
- Sessions cluster Feb–Mar 2026 (spring). Device `pitching2.0`.
- Sample session `69c3d44c1f745d8548daa3ab`: Eli Singer, Mar 24 2026, 19 pitches,
  avg 80.0 mph / 2027 rpm, sessionType "High Intent".

## ⚠ Two data-integrity rules (both verified against the UI)

### 1. Drop shots where `speed` is null
Failed radar tracks come back as full rows with `speed: null`, `spin: null`,
`spinConfidence: 0`, `isValidForStrike: false`. They are NOT excluded by the API.
In sample session `69c3d44c1f745d8548daa3ab`, 3 of 16 fastballs were failed tracks —
keeping them drops Eli Singer's average fastball from **82.5 mph to 67.0**.
The Rapsodo UI excludes them; the loader does too (`is_valid_shot`).

### 2. `pitchType` is an integer enum — six codes are in use
Codes 0 and 4 were decoded by reproducing the UI's own per-type aggregates from the
raw shots of session `69c3d44c1f745d8548daa3ab`:

| code | pitch | n | avg velo | max | avg spin | VB | HB | eff | UI row |
|------|-------|---|----------|-----|----------|----|----|-----|--------|
| 0 | **FB** | 13 valid | 82.5 | 83.8 | 2015.4 | 14.5 | 9.6 | 95.3 | 82.5 / 83.8 / 2015 / 14.6 / 9.6 / 95.3 ✓ |
| 4 | **SL** | 3 | 69.0 | 69.6 | 2077.9 | −5.0 | −5.2 | 32.6 | 69.0 / 69.6 / 2078 / −5.0 / −5.2 / 32.6 ✓ |

Codes 6, 3 and 5 were confirmed by Ian on 2026-08-19 from their pitch profiles across
an 873-pitch sample:

| code | pitch | n | velo | spin | VB | HB | eff | gyro | signature |
|---|---|---|---|---|---|---|---|---|---|
| 6 | **CH** | 66 | 75.0 | **1177** | +7.8 | +9.9 | 86% | 17° | low spin, armside, ~6 off the FB |
| 3 | **CB** | 28 | 72.6 | 1983 | **−9.4** | −3.7 | 57% | 49° | most downward break in the set |
| 5 | **SI** | 11 | 79.8 | 1874 | +13.2 | +13.3 | 93% | 16° | FB shape with more run |

**Codes 1 (n=13) and 2 (n=1) are deliberately LEFT UNMAPPED** — too few pitches and too
ambiguous a shape to call. Code 1 profiles at 72.2 mph / 1642 rpm / +4.7 VB / 42% eff /
65° gyro, which could be a cutter but is not established. They resolve to None and route
to QC — consistent with the spec's rule that unmapped values surface for a human rather
than being silently coerced. Together they are ~0.6% of pitches.

**Add codes here only after confirming them**, either against UI aggregates or by a
coach reading the pitch profile.

## Metric mapping (into metrics.REGISTRY keys)
| Rapsodo field | metric_key | unit |
|---|---|---|
| `speed` | `velocity` (+ `fb_velocity` when pitch_type = FB) | mph |
| `spin` | `spin_rate` | rpm |
| `verticalBreak` | `induced_vertical_break` | in |
| `horizontalBreak` | `horizontal_break` | in |
| `spinEfficiency` | `spin_efficiency` | % |
| `releaseHeight` | `release_height` | ft |
| `releaseSide` | `release_side` | ft |

The other ~78 fields stay in the raw JSON archive and can be promoted by adding a
line to `PITCH_METRIC_MAP`.

NOTE: the registry has an `extension` metric sourced from rapsodo, but **this account's
Pitching 2.0 does not report extension** — it is absent from both the API payload and the
UI table. Left unmapped rather than derived from `positionY` on a guess.

## OPEN
1. Login endpoint for auto-refreshing the JWT every ~30 days — not yet captured
   (needs a logged-out session to observe). Until then the token is pasted manually.
2. Hit-side shot schema not yet dumped (no hitting sessions in the sample player).
   Pull one once a hitter session exists; `swings` is the destination table.
3. Confirm whether `ballType` (baseball/softball) needs to be a request param.
4. Remaining `pitchType` codes (see table above).
5. `player_vendor_ids` needs a row per player mapping `vendor='rapsodo'` to the
   Rapsodo `player._id`. Until seeded, resolution falls back to name matching and
   anything unmatched is queued rather than guessed.

## Gotchas
- Chrome extension host permission for `cloud.rapsodo.com` drops when the MCP tab is
  recreated — re-grant in the extension side panel before more browser recon.
- The extension blocks JS that batches fetches with query strings (reads as
  cookie/query exfiltration). Capture endpoints by navigating + reading the network log
  instead of probing candidate URLs in a loop.
- Google Analytics calls from this page return 503; irrelevant, ignore.
