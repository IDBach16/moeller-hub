# Pitching analyst prompt — research beyond PRP and RPP

Gathered 2026-09-22 to extend `SYSTEM_PITCHING` / `PITCHING_KNOWLEDGE` in `summaries.py`.
Every number below is quoted from the source it is attributed to. "Prompt-ready" blocks
are written in the prompt's voice and can be pasted into the section named.


> **Ian's decision, 2026-09-22: Rapsodo-measured numbers only.** College and pro figures
> from Trackman / Hawk-Eye / Statcast come from different systems and are not comparable
> to our unit, so they stay OUT of the prompt. The *reasoning* in §3 is kept. Sections
> marked "context only" below are for reading, not for the prompt. The one exception to
> weigh: Rapsodo's college table in §1 is Rapsodo-unit data (JUCO→D1) — same system as
> ours, so it could be used as a labelled second pool; left out until Ian says otherwise.

Discarded on purpose: Rapsodo's "Data Pitch Summaries (High School Scale)" PDF — it is a
**softball** document (riseball, dropball, "her"); its thresholds do not apply.
Rapsodo's "Pitching Averages by Age" guide is gated behind a form and could not be read;
the college guide below is the closest published Rapsodo benchmark.

---

## 1. Benchmarks — CONTEXT ONLY except the Rapsodo college table (see note above)

The prompt ranks a pitcher only against Moeller arms. These give a second, labelled pool —
the same doctrine as the Blast benchmarks panel: **never a replacement for the Moeller rank,
always named as what it is.**

### Rapsodo college averages (JUCO → D1, "millions of pitches")
Source: rapsodo.com, *College Baseball Pitching Averages and How to Reach Them*.

| Pitch | RHP velo | LHP velo | Spin (RHP/LHP) | Eff (RHP/LHP) | IVB (RHP/LHP) | HB (RHP/LHP) |
|---|---|---|---|---|---|---|
| 4-seam | 85 | 83 | 2055 / 2005 | 91% / 88% | 15.9 / 16.0 | 8.1 / -7.3 |
| 2-seam | 84 | 82 | 1983 / 1933 | 90% / 87% | 13.4 / 14.4 | 8.4 / -7.6 |
| Cutter | 79 | 79 | 2073 / 1988 | 56% / — | 9.8 / 9.8 | 1.8 / -0.9 |
| Slider | 76 | 75 | 2086 / 2036 | 42% / — | 4.1 / 4.4 | -1.4 / 1.4 |
| Changeup | 78 | 77 | 1167 / 1629 | 89% / 87% | 11.4 / 11.8 | 8.8 / -8.4 |
| Splitter | 77 | 75 | 1221 / 1295 | 83% / — | 8.3 / 8.4 | 5.4 / -4.5 |
| Curveball | 73 | 72 | 2056 / 1995 | 55% / — | -0.9 / -0.1 | -2.7 / 3.1 |

Two things worth noticing against our own prompt: the college **curveball averages almost
zero IVB** — most college "curveballs" are slurvy — so the prompt's 12-6 target of -15" is a
pro-shape ideal, not a college norm. And the college **changeup averages 11.4" IVB against a
15.9" fastball: 4.5" of separation**, which is under the prompt's 5-8" ask. Our ask is
right (it is what makes the pitch work) but the analyst should know most college arms fall
short of it too, so a Moeller kid at 4" is not broken.

### High-school velocity by grade (composite of Trackman/PG/PBR/Rapsodo) — context only
Source: topvelocity.org velocity-by-age chart (states its sources).

| | Fr | So | Jr | Sr |
|---|---|---|---|---|
| Average fastball | 65 | ~70–74 | 78 | 81 |

"College prospect" radar number: 85–87 as a junior/senior; D1 commits regularly 88–92; only
the top 5–10% of 17-year-olds reach 90.

### What a D1 signing looked like as a high-school sophomore (Trackman) — context only
Source: PBR Indiana, *Data Dive: Valued Metrics in Evaluating Underclass Arms* (Trackman).

- Fastball velocity: D1 average **87.7** (LHP 86.6, RHP 88.1); Power-4 **89.1**
- Fastball spin: D1 average **2161 rpm**; Power-4 2235; "above average" line **2261 rpm** —
  60% of arms above it signed Power-4, 30% of those below
- Breaking-ball spin: D1 average **2224 rpm**; Power-4 ~2305–2321
- Extension: 84th–98th percentile = 6.8–7.5 ft; 98th+ = 7.6+ (we do not measure extension)
- Release height: extremes at either end "help mitigate velocity deficits"
- Three categories at the 84th percentile or better → 89% signed Power-4

### MLB reference points (Statcast/Hawk-Eye) — context only
Driveline 2021 league averages, RHP: 4-seam **16.5" IVB / 7.5" HB, 2275 rpm**; sinker 9.5 /
~7.5, 2125; cutter 8 / 3, 2375; slider 1.5 / 7, 2425; curveball -11 / 10, 2500;
changeup 5.5 IVB, ~1700 rpm. Spin-to-velo ~24.5 on the 4-seam, just under 23 on the sinker.
Active spin (efficiency) league averages: 4-seam 90%, sinker 87%, changeup 92%, splitter
80%, curveball 69%, cutter 46%, slider 37%.
Baseball America: 18"+ IVB is above average on a 4-seam, 20" is elite; "heavy bore" = 9"+
HB with 17"+ IVB; a sinker is <10" IVB (6–9 typical) with 12"+ run; a slurve is ≤ -8" IVB
with 7"+ sweep; a gyro slider is 83+ mph with minimal break both ways.

---

## 2. Corrections to what the prompt says now

1. **Gyro slider efficiency.** Prompt: "Efficiency under 20, typically 15-30." Rapsodo:
   gyro sliders "lower than 10%"; RPP: "< 10%"; sidespin/sweeping sliders ~15–35% (Rapsodo
   gives ~35% and gyro degree 65–75). RPP names **efficiency above 35% on a slider as a
   fault** (it has become a slurve). Fix the band: gyro < 10, sweeper 15–35, over 35 is not
   a slider.
2. **Cutter efficiency.** Prompt says 40–60; Rapsodo says **45–65%** and gives the failure
   modes on both sides: above the band it "flattens" into a fastball, below it "blends into
   slider territory" and loses its spin-induced break. Keep 45–65 and add the two failure
   modes, because they are what a coach sees.
3. **Sinker targets.** Rapsodo's break-profile guide: lively 2-seam **10–13" IVB, heavy
   lower**, and **at least 18" HB**; it calls a fastball with equal vertical and horizontal
   break the "flat zone" — the same thing PRP calls the dead zone. Worth naming both so a
   coach who has read Rapsodo's material recognises it.
4. **The coach-vs-pitcher voice contradiction** (rules say "write for coaches about him,
   never to him"; overview and watch are written to him). Settle it one way.

---

## 3. New concepts worth adding

### 3a. Expected ride for his slot — quantifies the release-height gate
Source: basetunnel.substack.com, *Introducing Fastball+* (MLB regression).
Expected IVB rises **~2 inches per foot of release height** and falls ~1 inch per foot of
release side. Worked figures quoted in the search results: a 6.0 ft release "expects" ~17"
of IVB, a 5.0 ft release ~14.5". Derived at the MLB level, so use it as a slope, not a
table: judge a fastball's ride **relative to what his slot allows**, and call out the arm
whose ride is above what his slot predicts — that is the rare, valuable one.

Prompt-ready (for `<which_shape_should_he_chase>`):
> Ride follows slot. As a rule of thumb ride rises about two inches for every foot of
> release height, so a 5.5-foot release "expects" roughly 2 inches less ride than a
> 6.5-foot one. Judge his fastball's ride against what his own slot allows, not against
> the staff's best rider, and say so when he beats his slot — that is the arm to protect.

### 3b. A low slot is not a handicap at the top of the zone — approach angle
Sources: FanGraphs VAA primer; PBR *Using VAA to Identify Draft League Prospects*
(4,000 amateur fastballs up in the zone, 2.83–3.5 ft):

| VAA on a fastball up | Whiff |
|---|---|
| -4.0° or flatter (elite) | 36.7% |
| -4.0 to -4.5 (above average) | 22.9% |
| -4.5 to -5.0 (average; college average -4.9) | 17.1% |
| -5.0 to -5.5 (below) | 10.3% |
| -5.5 or steeper | 4.4% |

A flat angle comes from **a low release, extension, ride, and throwing it up** — a low-slot
arm gets part of it for free. Driveline's updated Stuff+ notes low release points with long
extension grade above average "despite modest induced vertical break". We do not measure
VAA or extension, so the prompt cannot cite a number for him, but it can stop treating
"no ride" as "cannot play up".

Prompt-ready:
> A fastball plays at the top of the zone by arriving flat, and flatness comes from a low
> release as much as from ride. So a low-slot arm with modest ride can still own the top of
> the zone; what he cannot do is chase ride he does not have. We do not measure approach
> angle, so say this as a principle, never as a number.

### 3c. The changeup, updated
- Driveline: 8–12 mph off the fastball; RHP axis **1:30–2:30**, past 3:00 for more side;
  grip cue "index finger to the side of the ball"; intent cues "roll over the ball", "swipe
  the inside of the ball". Driveline Stuff+: the best big-league changeups carry **15+
  inches of vertical separation** off the fastball.
- **Kick change** (Tread Athletics, 2024–26; ESPN, FanGraphs): for **supinators** — arms
  that naturally cut the ball and cannot pronate a conventional changeup. Spike the middle
  finger so it is the last thing on the ball; ring finger kills efficiency. Profile: near-
  zero or negative IVB with big arm-side run, thrown hard (Holmes 88 mph, -10 IVB; Martin's
  best "-1 IVB, 20 run at 90"). Whiff rates 38–50% for the Mets' three. The data signature
  of a candidate: a fastball that cuts or has little run, and a changeup that keeps backing
  up (positive IVB, small axis tilt) no matter the grip.

Prompt-ready (for the changeup profile and `<cues>`):
> If his changeup will not tilt — axis within an hour of his fastball and ride that will
> not come down — and his fastball shows cut rather than run, he may be a supinator, and the
> conventional circle change is fighting his hand. The kick change (middle finger spiked so
> it leaves the ball last) is the modern option for that arm: it kills ride and adds run
> without asking him to pronate. Raise it as a question for the coach, not a prescription.

### 3d. Slider faults, and which slider off which fastball (RPP, Tread)
- Sweeper: 5–10 mph off the fastball, efficiency 15–35%, **10–15+ inches** of sweep; tunnels
  off a two-seam or a changeup. Cues: "come around the ball", "3 o'clock to 9 o'clock",
  "show the back of your hand to the catcher".
- Gyro: efficiency **< 10%**, 1–5" of sweep, can be thrown harder; tunnels off a four-seam.
  Tread's targets: IVB between +5 and -5 (past -5 it is a curve), HB 0 to -5 (RHP). Cues:
  "come off the side of the ball", "throw it like a dart / football spiral", "twist the
  doorknob", "throw the U".
- Faults with data signatures: efficiency above 35% (slurve), velocity gap over 10 mph
  (loopy, easy to read), **axis wandering pitch to pitch** — which shows as command loss
  before it shows as movement.
- The **vertical slider** (Driveline 2026): within 10 mph of the fastball, ≤ 4" HB, real
  downward break, gyro-dominant. The top whiff shape in MLB (36.9% in 2026) and a strike
  pitch as well. For a Moeller arm this is simply "a hard gyro slider with depth" — worth
  naming when a kid already has it.

### 3e. Spin mirroring — a check the analyst can run from spin_axis
Source: FanGraphs, *Taking a Look at Spin Mirroring*; Cleveland/Cardinals follow-ups.
A fastball and curveball whose axes sit **~180° apart** look identical out of the hand and
then go opposite ways; the best mirrored curveballs sit **within 8° of 180**. Slider and
changeup can pair the same way (Romo: 2:45 and 8:30). Sliders sit ~90° off either.
Prompt-ready (new step in `<how_to_read_a_pitcher>`, after separation):
> Mirror check. Add 180 degrees to his fastball axis and compare it with his curveball's
> (his changeup's, for a slider). Within about 10 degrees is a true mirror — the two pitches
> are indistinguishable until they break — and it is worth telling him he has it. Read both
> axes from his own pitches; never from the staff.

### 3f. Sinker arms are judged on contact, not whiffs
Source: Pitcher List, *Sink or Spin*: sinkers "collectively have the lowest whiff% of any
pitch"; their value is launch angle (2020: 95+ mph sinkers, LA 3.6°; sub-91, 5.9°). Spin
rate barely relates to sinker movement (r = .19 vertical, .01 horizontal). Our
`game_pitching` carries strike% and whiff% only, so:
Prompt-ready (for `<rules>`):
> A runner or sinker arm is not supposed to miss bats with the fastball. Read his fastball's
> game results by strike% and, when we have it, contact quality, and never call a low
> fastball whiff% a fault on a sink-and-run profile.

### 3g. Seam-shifted wake — know it, do not infer it
Sources: Driveline (two articles), RPP explainer.
Non-Magnus movement from seam orientation: league-average sinker gains **3"+ of run and
~4" of depth** beyond what its spin predicts; cutters ~3" glove-side and 2" drop; changeups
extra drop; four-seams modest; sliders and curveballs essentially none. Detected by
comparing spin-based to observed movement (or an axis deviation; 2020 average sinker 2D
axis deviation ~17.6°). One-seam grips dominate among the best sinkers. Rapsodo does not
report the spin-based/observed split, so the analyst cannot see it in our data.
Prompt-ready (for `<rapsodo_metrics>`):
> Some run and sink comes from seam orientation rather than spin, which is why a sinker
> can out-move its axis. Our data does not separate the two, so never claim seam effects;
> if a two-seam runs more than its axis suggests, say that and leave the why to the coach.

### 3h. Location moves IVB — a caveat for change detection
Source: FanGraphs, *It's Release Angles All the Way Down*: vertical release angle explains
more of fastball carry than release height or velocity; **fastballs aimed lower carry
more, fastballs aimed up carry less**, at the same slot. So an IVB shift on a bullpen
where he was working a different part of the zone can be a location artefact.
Prompt-ready (for `<rules>`):
> Ride moves with where he was throwing. A fastball worked low shows more ride than the
> same fastball worked up, so before calling a ride change real, ask whether the bullpen
> was working a different part of the zone. When you cannot tell, say so.

### 3i. What actually drives pitch quality (Driveline Stuff+)
Velocity first (~6 Stuff+ points per mph, exponential at the top), then movement, then
release traits; and **differentials from the fastball matter more than raw movement** —
velocity gap, movement gap, axis gap. Gyro / low-efficiency pitches are undervalued by
raw-movement reads. This is the published justification for two things the prompt already
does: compare within the pitcher, and treat the fastball as the reference for every other
pitch. A four-seam whiff study (FanGraphs community, 2017–21) found "slow-but-rising" beat
"fast-but-sinking" — ride matters at 78 mph too.
Prompt-ready (one line for `<rapsodo_metrics>`):
> A secondary pitch is graded by its distance from his fastball — in velocity, in movement,
> in axis — more than by its own numbers. A slider that sweeps 8 inches is a different
> pitch behind a 16-inch runner than behind a 17-inch rider.

### 3j. Rapsodo's own spin-direction bands
| Pitch | RHP | LHP |
|---|---|---|
| Fastballs / changeups | 12:00–2:00 | 10:00–12:00 |
| Curveballs | 6:00–8:00 | 4:00–6:00 |
| Sliders | 9:00–12:00 | 12:00–3:00 |
Gyro degree and efficiency are inverse (0° = 100%); "cutting" the ball raises gyro degree
and kills movement (Rapsodo gyro explainer). Useful for step 3, "is each pitch behaving like
its own type", and for the handedness-unknown case: a fastball at 10:40 is a left-hander.

---

## 4. What went into the prompt (2026-09-22) — concepts and Rapsodo-sourced numbers only

1. Gyro-slider band corrected to Rapsodo/RPP (< 10%; over 35–40 is a slurve); cutter band
   45–65 with both failure modes; lively two-seam 10–13" ride with 18"+ run (Rapsodo).
2. "Ride follows slot" and the approach-angle principle, as principles without the
   Statcast coefficients (§3a, §3b).
3. Mirror check as a reading step (§3e) — geometry, device-independent.
4. Slider fault signatures (§3d, RPP) and the sweep/gyro intent cues (RPP, Tread).
5. Kick-change candidate signature for supinators (§3c), as a question for the coach.
6. Three rules: sinker arms and whiff%, ride and location, no seam-effect claims
   (§3f, §3h, §3g).
7. The fastball-differential principle (§3i), without Stuff+ numbers.
NOT added: every Trackman / Hawk-Eye / Statcast number (§1 MLB, PBR, TopVelocity; VAA
tiers; expected-IVB coefficients; kick-change Statcast profiles).

---

## Sources
- Rapsodo — College Pitching Averages: https://rapsodo.com/blogs/baseball/college-pitching-averages-and-how-to-reach-them
- Rapsodo — Spin Rate & Efficiency Profile (Fastball): https://rapsodo.com/blogs/baseball/understanding-rapsodo-pitching-data-spin-rate-efficiency-profile-fastball/
- Rapsodo — Spin Rate & Efficiency Profile (CB/SL/CH): https://rapsodo.com/blogs/baseball/understanding-rapsodo-pitching-data-spin-rate-efficiency-profile-curveball-slider-changeup
- Rapsodo — Break Profile (Fastball): https://rapsodo.com/blogs/baseball/understanding-rapsodo-pitching-data-break-profile-fastball
- Rapsodo — Spin Profile: https://rapsodo.com/blogs/baseball/understanding-rapsodo-pitching-data-spin-profile
- Rapsodo — Gyro Spin & Gyro Degree: https://rapsodo.com/blogs/baseball/understanding-gyro-spin-gyro-degree-the-hidden-forces-behind-pitch-movement
- Rapsodo — Pitching Averages by Age (gated PDF): https://rapsodo.com/pages/baseball-pitching-averages-by-age
- Driveline — Basics of Baseball Pitch Movement: https://drivelinebaseball.com/blogs/blog/basics-of-baseball-pitch-movement
- Driveline — Intro to Seam-Shifted Wakes (sinkers): https://drivelinebaseball.com/blogs/blog/more-than-what-it-seams-an-introduction-to-seam-shifted-wakes-and-their-effect-on-sinkers
- Driveline — Impact of SSW on Pitch Quality: https://www.drivelinebaseball.com/2021/03/the-impact-of-seam-shifted-wakes-on-pitch-quality/
- Driveline — Vertical Slider: https://drivelinebaseball.com/blogs/blog/vertical-slider-pitch-design
- Driveline — What is Stuff+: https://drivelinebaseball.com/blogs/blog/what-is-stuff-quantifying-pitches-with-pitch-models
- Driveline — Revisiting Stuff+: https://drivelinebaseball.com/blogs/blog/revisiting-stuff-plus
- Driveline — How to Throw a Changeup: https://www.drivelinebaseball.com/2020/06/how-to-throw-a-changeup/
- Driveline — How to Throw a Four-Seam: https://www.drivelinebaseball.com/2020/06/how-to-throw-a-four-seam-fastball/
- RPP — How to Throw a Slider or Sweeper: https://rocklandpeakperformance.com/how-to-throw-a-slider/
- RPP — What is a Seam-Shifted Wake Pitch: https://rocklandpeakperformance.com/what-is-a-seam-shifted-wake-pitch/
- Tread Athletics — gyro slider cues (X): https://x.com/TreadHQ/status/1889688659160342842
- ESPN — the kick change: https://www.espn.com/mlb/story/_/id/45007922/mlb-2025-kick-change-changeup-new-york-mets-holmes-canning-megill
- FanGraphs — Davis Martin & Matt Bowman on the kick change: https://blogs.fangraphs.com/davis-martin-and-matt-bowman-break-down-the-kick-change/
- FanGraphs — VAA primer: https://blogs.fangraphs.com/a-visualized-primer-on-vertical-approach-angle-vaa/
- PBR — VAA and Draft League prospects: https://www.prepbaseballreport.com/news/PBR/Using-Vertical-Approach-Angle-to-Identify-MLB-Draft-League-Prospects-0485619723
- PBR — Valued Metrics in Evaluating Underclass Arms: https://www.prepbaseballreport.com/news/IN/data-dive--valued-metrics-in-evaluating-underclass-arms
- FanGraphs — Spin Mirroring: https://blogs.fangraphs.com/taking-a-look-at-spin-mirroring/
- FanGraphs — Release Angles All the Way Down: https://blogs.fangraphs.com/its-release-angles-all-the-way-down/
- FanGraphs community — What Makes a Good Four-Seamer Good: https://community.fangraphs.com/what-makes-a-good-four-seamer-good/
- Basetunnel — Introducing Fastball+ (expected IVB regression): https://basetunnel.substack.com/p/introducing-fastball
- Pitcher List — Sink or Spin: https://pitcherlist.com/sink-or-spin-what-makes-sinkers-effective/
- Baseball America — Every MLB Pitch Type in the Tracking Era: https://www.baseballamerica.com/stories/understanding-pitch-classification-in-the-pitch-tracking-era/
- TopVelocity — Pitching Velocity by Age: https://topvelocity.org/pitching-velocity-by-age/
- MLB Glossary — Active Spin: https://www.mlb.com/glossary/statcast/active-spin
- Baseball Development Group — Pitch Design Resource: https://baseballdevelopmentgroup.com/pitch-design-resource/
