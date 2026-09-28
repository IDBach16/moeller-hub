# Pitching development analyst as an investigating agent — plan for confirmation

Drafted 2026-09-28; **built the same day** (`pitching_agent.py`, `test_pitching_agent.py`).
Ian's confirmation: Rapsodo + in-game data, wrap existing functions, Rewrite button + chat first, Opus 5.5.

## 0. What already exists, honestly

| Surface | Today | Is it an agent? |
|---|---|---|
| The note on a pitcher's page (`SYSTEM_PITCHING`, `summaries.generate`) | ONE model call over a precomputed context (`build_context`), cached by a hash of his latest session + change ids | No — one shot, by design ("the database computes, the LLM explains") |
| "Ask the analyst" chat under the note (`agent.answer_for_player`) | Tool-use loop, up to 6 rounds, 20+ tools, his context in the system prompt | **Yes** |
| Coordinator chat on the Players tab (`agent.answer`) | Same loop, group scope | Yes |

So the job is not "build an agent"; it is **give the note the chat's loop, with an
investigate-then-report procedure and a fixed report contract.** Almost every tool
below wraps a function that already exists and already carries the doctrine
(compare within a pitch type, circular spin axis, sample floors, no invented
players). Writing fresh SQL for those would re-open the exact traps the tests
guard.

## 1. The prompt split

Current prompt = `<what_you_are_given>` + `PITCHING_KNOWLEDGE` (rapsodo_metrics,
which_shape_should_he_chase, cues, plain_english) + `<how_to_read_a_pitcher>` +
`<output>` + `<rules>`.

### TOOLS — retrieval, filtering, calculation, baselines, benchmarks

| In the prompt today | Becomes |
|---|---|
| `<what_you_are_given>` — the list of context fields | tool descriptions (the model discovers data by calling, not by being told the shape) |
| "spin_axis ... divide by 30 to read it as a clock" | tool returns degrees AND clock |
| "Bauer Units = spin_rate / velocity" | tool computes it per pitch |
| FASTBALL SHAPE table (ride / ride-run / runner / sinker / dead zone thresholds) | tool classifies; prompt keeps what each shape *means* |
| SEPARATION test (1" both breaks AND 5° axis; changeup 30°, sinker 15°) | tool computes per pair and returns verdicts |
| MIRRORING (fastball axis + 180 vs curveball within 10°) | same tool |
| "his percentile against the arms he is pooled with" | tool returns percentiles + pool name |
| "changes ... with effect_size and counts" | `what_changed` / `compare_windows` |
| "bullpen_pitch_mix ... and the shift" | `training_pitch_mix` |
| "game_pitching per pitch: usage, avg velo, strike%, whiff%" | `game_pitching` |
| release_height / release_side "properties of the whole delivery" | `metric_history` pooled (pitch_type omitted) |
| "Release height on this staff runs 5.1 to 6.7 feet, median 5.9" | returned by `pitch_shape_report` (pool stats), not hard-coded in prose |

### JUDGMENT — stays in the system prompt

- Units and *meaning* of every metric; the ten pitch profiles (what a good slider looks like); why Bauer Units are the fair read for a high school arm; MLB 20" ride as direction of travel not pass mark
- What each fastball shape *means* and the dead-zone conversation (shape/axis problem vs spin problem)
- The release-height gate: ride follows slot; never tell a low-slot arm to chase ride; flat arrival as a principle
- Everything is relative to his fastball; command gates all of it; sinker arms are judged on strikes and contact, not whiffs
- The supinator / kick-change signature; the slider fault signatures; cutter failure modes
- Cues (one per note, as an option, never body mechanics); plain-English translations
- The reading order (becomes the investigation procedure — see §3)
- The report contract (`read`, `overview`, `findings`, `watch`, `caveat`) and every rule in `<rules>`
- "Where throws is null, do not name a side"; "ride moves with location"; no seam-effect claims

## 2. The tools (read-only, concise, structured)

Cap: **15 calls per run.** Every call and result logged. Tools that name teammates or
draft coach actions (`compare_pitchers`, `staff_leaderboard`, `propose_*`) are NOT
offered to this agent — same fence as the player chat.

| # | Tool | When the model should call it | Wraps | New code? |
|---|---|---|---|---|
| 1 | `get_pitcher_context` | **Always first.** Identity, throws, last 4 sessions, arsenal with percentiles and axis, mix, game splits, goals, interventions, detected changes | `summaries.build_context` | no |
| 2 | `pitch_shape_report` | Before naming a fastball shape or judging any pitch's type | `percentiles.by_pitch` + `rapsodo_card` (adds Bauer Units, shape class, pool release-height stats) | small |
| 3 | `separation_and_mirror` | Before saying two pitches blend, a changeup lacks tilt, or two pitches mirror | new arithmetic over #2 (circular axis diff) | **yes** (~40 lines) |
| 4 | `what_changed` | Right after #1, to see what detection already fired and what was suppressed | `agent.tool_what_changed` | no |
| 5 | `metric_history` | A trend question ("is velo up?"), or release consistency (pooled) | `agent.tool_metric_history` **+ pitch_type param** (today it pools; PITCH_SPECIFIC metrics must require one) | small |
| 6 | `compare_windows` | To confirm an anomaly with an effect size before calling it real | `agent.tool_compare_windows` + pitch_type param | small |
| 7 | `training_pitch_mix` | Usage questions; "is he practicing what he pitches" | `summaries.training_pitch_mix` | no |
| 8 | `game_pitching` | "Can he land it / did it play": per-pitch usage, velo, strike%, whiff% by season | the game block of `build_context` (season CSV) | small |
| 9 | `goals_and_interventions` | "Is the plan working"; whether a logged intervention sits near a change | `agent.tool_goals_and_interventions` | no |
| 10 | `benchmark` | "Where he stands" against class and program, beyond the pool percentile | `agent.tool_benchmark` | no |
| 11 | `previous_note` | Continuity: what the last note said, so this one can say what moved | `agent.tool_player_summary` | no |
| 12 | `submit_report` | **Last call, exactly once.** Takes the note JSON (read/overview/findings/watch/caveat) and ends the run | new; validates against `note_schema` | **yes** |

`submit_report` is how the loop ends with a guaranteed-valid report: the final
answer is a tool call with the schema as its input, not free text we hope parses.

## 3. The system prompt, reframed

Same judgment, minus the mechanics above, and the reading order becomes a
procedure:

> You are investigating this pitcher, not summarizing a table. Start with
> `get_pitcher_context` and `what_changed`. Say to yourself what kind of arm he
> is. Then look for anything that does not fit — a shape that has moved, a pitch
> that reads like another, a game number that disagrees with the pen — and
> **confirm it before you conclude**: `pitch_shape_report` before naming a shape,
> `separation_and_mirror` before saying two pitches blend, `compare_windows`
> before calling a change real, `game_pitching` before saying whether it played.
> Stop investigating when the evidence runs out or when you are confident; do
> not call a tool to decorate a conclusion you already hold. Finish with
> `submit_report`. You have at most 15 calls; most pitchers need five.

Output contract unchanged. Voice question (about him vs to him) still needs
Ian's call; the agent version is the moment to settle it.

## 4. The loop (to build after confirmation)

- `pitching_agent.investigate(engine, player_id) -> note dict`
- model **`claude-opus-5-5`** ($4 / $20 per MTok; cache hits 5%) — cheaper than
  the `claude-opus-5` the note uses today
- tool_use → execute → tool_result, repeat; stop on `submit_report` or 15 calls
  (on the cap: force a final `submit_report` with what it has, caveat says so)
- log: JSONL per run (`logs/pitching_agent/<player>_<ts>.jsonl`) with every
  tool call, args, result size, latency, tokens; plus a `job_runs`-style row
- cached exactly as today (same basis hash), so a Monday run costs nothing for
  players whose data has not moved

## 5. Cost and behaviour — the real trade

| | One-shot note (today) | Investigating agent |
|---|---|---|
| Model calls | 1 | 5–15 |
| Cost per note | ≈ $0.11 (Opus 5) | ≈ $0.35–0.90 (Opus 5.5, cached prefix) |
| Monday, ~30 arms with new data | ≈ $3 | ≈ $10–27 |
| Latency | 8–20 s | 45–120 s |
| Reproducible | yes, same context → same inputs | no — the path varies run to run |
| Can pull more when the context is thin | no | **yes** (the actual gain) |

**Recommendation:** seed the agent with the full context (tool #1 returns exactly
what the note sees today), so a simple pitcher costs one or two calls and a
complicated one earns its extra calls. Run the agent on the **Rewrite button**
and in the **chat**; keep the Monday batch on the one-shot until a month of
side-by-side notes shows the agent's are better, not just longer.

## 6. Questions to confirm

1. Tool list above — anything missing (e.g. a `game_contact` tool once we have
   contact quality), anything you would not expose?
2. Agree to wrap existing functions rather than write new SQL?
3. Where the agent runs: Rewrite button + chat first, Monday batch later — or
   everything at once?
4. `submit_report` as the ending — fine?
5. Voice: written *about* him (coach reads it) or *to* him (he reads it)?
6. Model: `claude-opus-5-5` as you specified.
