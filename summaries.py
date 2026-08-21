"""
summaries.py -- cached AI player summaries. See PLAYER_DEV_SPEC.md 9.2 and 9.3.

The rule this module exists to enforce: a player summary costs ONE API call when
new data lands, not one per page view. It is stored against a hash of what it was
written from -- the player's latest session plus the change events and goals in
play -- so an unchanged player returns cached text with no model call at all.

Two halves, deliberately separated so the expensive one is the only one that
needs an API key:

    build_context()    pure, testable, no network. Assembles the compact
                       summary of a player from what the database already
                       computed -- never raw swings or pitches.
    generate()         calls the model once with that context and stores it.

    python summaries.py                weekly run: everyone with new data
    python summaries.py --all          everyone, regardless of new data
    python summaries.py --player 14
    python summaries.py --dry-run      show the context, make no API call
"""

import hashlib
import json
import os
import sys
from datetime import date, timedelta

from sqlalchemy import delete, func, insert, select

import db
import metrics

MODEL = "claude-opus-5"

SYSTEM = """You are the pitching development analyst for Archbishop Moeller High \
School. The coaching staff sees the numbers themselves; your job is to read them \
the way a sharp analyst would and tell the staff what they mean, so they don't \
have to work it out from a table.

You are given a compact summary that the database has already computed: recent \
training sessions, detected changes against the player's own baseline, what he has \
been throwing, his game results, active development goals, and any logged \
interventions. You are NOT given the raw data, and you must not ask for it.

BE AN ANALYST, NOT A REPORT. Anyone can list what moved. What earns your place is \
the interpretation:
- Lead with the READ, then the evidence. "He's built a fastball-only bullpen \
routine that doesn't match how he actually pitches" beats "fastball usage rose \
32.5 points".
- Find the ONE thing that matters most and say why it matters. Everything else is \
supporting detail or gets left out. A coach reading this between bullpens should \
come away knowing what to think about.
- Connect the layers. Training tells you what his stuff is doing; game data tells \
you whether it played. When both are present, say what the pairing implies -- \
that connection is the whole reason these sit in one system.
- Name the decision. End by making clear what question is now in front of the \
staff, so the note sharpens a choice rather than adding to a pile of numbers.
- Distinguish signal from thin ice. Say when a sample is too small to lean on, and \
say when something IS solid. Coaches need to know which is which.

Keep it to 4-6 sentences -- long enough to interpret, short enough to read \
standing up. Rules:
- Every claim rests on a number you were given. Show the number.
- Never invent a number. If something isn't in the context, don't mention it.
- A change is a comparison, not a cause. If an intervention is logged near a change, \
you may note the timing, but do not claim the intervention caused it.
- Say plainly when there isn't enough data to conclude anything. That is a useful \
answer, not a failure.
- What he is THROWING is a finding too. If bullpen_pitch_mix lists a notable shift, \
say so -- a pitcher going from 7% to 28% sliders is doing something different on \
purpose, and it is worth a coach knowing. Report it as a change in usage, never as \
a change in the pitch itself.
- Every detected change is already scoped to one pitch type where that matters, so \
name the pitch when the context does ("his fastball's horizontal break", not \
"his horizontal break"). Do not generalise a single pitch's number to his whole \
arsenal.
- Baseball shorthand is fine. Plain text only -- no markdown, no bullets, no headers.
- Do not address the player. You're writing for coaches about him.

END WITH A SUGGESTION. After the observations, add one or two ideas the staff \
could act on, opened with "Worth" or "Suggest" so it reads as an option rather \
than an instruction. Roadmap section 4 asks for "suggested areas for coach \
investigation" -- so give them, and make them specific to this player's numbers.

Good suggestions are things to CHECK, ASK, MEASURE or WATCH, and conditional \
recommendations tied to what the data would show:
- "Worth asking him whether the release-side move was intentional -- if it wasn't, \
the fastball break gain may not hold."
- "Suggest a checkpoint bullpen inside two weeks; three sessions is thin for \
calling a slot change permanent."
- "Worth logging this as an intervention if it was deliberate, so the next \
comparison has something to measure against."
- "If the slider usage is intentional, worth charting it in a live AB to see \
whether the shape plays against hitters."

Do NOT prescribe mechanics or tell a coach how to fix a pitcher. You do not see \
him throw, you have no video, and you have no biomechanics. Never write "lower his \
arm slot", "shorten his stride", "he should change his grip", or any instruction \
about how to move his body. The staff decides what to do; you point at what is \
worth their attention and say why. If the data is too thin to suggest anything \
useful, say that instead of inventing a suggestion."""


# ---------------------------------------------------------------------------
# What the summary was written from
# ---------------------------------------------------------------------------

def basis(engine, player_id):
    """A hash of everything the summary depends on.

    If this is unchanged, the stored summary is still current and no model call
    is needed. If any of it moves -- a new session, a new detected change, a new
    goal -- the hash changes and the summary is regenerated on the next run.
    """
    with engine.connect() as conn:
        latest = conn.execute(
            select(func.max(db.sessions.c.id))
            .where(db.sessions.c.player_id == player_id)).scalar()
        change_ids = [r[0] for r in conn.execute(
            select(db.change_events.c.id)
            .where(db.change_events.c.player_id == player_id)
            .order_by(db.change_events.c.id))]
        goal_ids = [f"{r.id}:{r.status}" for r in conn.execute(
            select(db.goals.c.id, db.goals.c.status)
            .where(db.goals.c.player_id == player_id)
            .order_by(db.goals.c.id))]
        iv_ids = [f"{r.id}:{r.outcome}" for r in conn.execute(
            select(db.interventions.c.id, db.interventions.c.outcome)
            .where(db.interventions.c.player_id == player_id)
            .order_by(db.interventions.c.id))]
    raw = f"{latest}|{','.join(map(str, change_ids))}|{','.join(goal_ids)}|{','.join(iv_ids)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# A pitch's share of the work moving by this much is worth a coach's attention.
# Below it, it's just the normal shape of a bullpen.
MIX_SHIFT_PP = 8.0


def training_pitch_mix(engine, player_id):
    """What he actually threw recently vs before, as usage percentages.

    Usage is a development fact in its own right -- a pitcher going from 7% to 28%
    sliders is doing something different on purpose. It is deliberately NOT a
    change in any pitch, and change detection now compares within a pitch type so
    a mix shift can no longer masquerade as one (see metrics.PITCH_SPECIFIC).
    Computed here so the model is handed the comparison rather than doing
    arithmetic on raw counts.
    """
    with engine.connect() as conn:
        dates = [r[0] for r in conn.execute(
            select(db.sessions.c.session_date).distinct()
            .where((db.sessions.c.player_id == player_id) &
                   (db.sessions.c.source == "rapsodo"))
            .order_by(db.sessions.c.session_date.desc()))]
        if len(dates) < metrics.RECENT_SESSIONS + 1:
            return None
        recent, baseline = dates[:metrics.RECENT_SESSIONS], dates[metrics.RECENT_SESSIONS:]

        def counts(window):
            rows = conn.execute(
                select(db.pitch_metrics.c.pitch_type, func.count())
                .select_from(db.pitch_metrics.join(
                    db.sessions, db.sessions.c.id == db.pitch_metrics.c.session_id))
                .where((db.pitch_metrics.c.player_id == player_id) &
                       (db.pitch_metrics.c.metric_key == "velocity") &
                       (db.sessions.c.session_date.in_(window)))
                .group_by(db.pitch_metrics.c.pitch_type)).all()
            total = sum(n for _pt, n in rows) or 1
            return {(pt or "unlabelled"): round(100.0 * n / total, 1) for pt, n in rows}

        # Inside the connection block -- counts() closes over `conn`.
        now, before = counts(recent), counts(baseline)
    shifts = []
    for pt in sorted(set(now) | set(before)):
        a, b = before.get(pt, 0.0), now.get(pt, 0.0)
        if abs(b - a) >= MIX_SHIFT_PP:
            label = metrics.PITCH_TYPE_LABELS.get(pt, pt)
            shifts.append(f"{label} {a}% -> {b}% of his pitches "
                          f"({'+' if b > a else ''}{round(b - a, 1)} pts)")
    return {
        "recent_window": f"last {len(recent)} bullpens",
        "recent": {metrics.PITCH_TYPE_LABELS.get(k, k): f"{v}%" for k, v in now.items()},
        "baseline": {metrics.PITCH_TYPE_LABELS.get(k, k): f"{v}%" for k, v in before.items()},
        "notable_shifts": shifts,
    }


def build_context(engine, player_id):
    """The compact context the model is given. No raw rows, ever.

    A pitcher with 2,400 tracked pitches produces a few hundred tokens here --
    that is the whole point of roadmap section 9.
    """
    import profiles
    with engine.connect() as conn:
        p = conn.execute(select(db.players)
                         .where(db.players.c.id == player_id)).first()
        if not p:
            return None
    prof = profiles.profile(engine, p.slug)

    ctx = {
        "player": prof["player"]["name"],
        "role": "pitcher" if prof["player"]["is_pitcher"] else "position player",
        "bats": prof["player"]["bats"], "throws": prof["player"]["throws"],
        "last_session": prof["last_session"],
        "training_sessions": len(prof["training"]),
    }

    # Only unacknowledged changes -- acknowledged ones are already old news.
    ctx["changes"] = [
        {"summary": c["summary"], "severity": c["severity"],
         "favorable": c["favorable"], "detected_on": c["detected_on"],
         "effect_size": c["effect_size"],
         "observations": f"{c['n_recent']} recent vs {c['n_baseline']} baseline"}
        for c in prof["changes"] if not c["acknowledged"]][:6]

    ctx["recent_sessions"] = [
        {"date": s["date"], "type": s["type"], "purpose": s["purpose"],
         "metrics": {k: f"{m['mean']}{m['unit']} (n={m['n']})"
                     for k, m in s["metrics"].items()}}
        for s in prof["training"][:4]]

    ctx["goals"] = [
        {"title": g["title"], "status": g["status"],
         "metric": g["metric_label"], "target": g["target_value"],
         "progress": g["progress"].get("state"),
         "current": g["progress"].get("current"),
         "pct_of_the_way": g["progress"].get("pct")}
        for g in prof["goals"] if g["status"] == "active"][:5]

    ctx["interventions"] = [
        {"title": i["title"], "date": i["date"], "category": i["category"],
         "outcome": i["outcome"],
         "before_after": [f"{m['label']} {m['pre_mean']}->{m['post_mean']}{m['unit']}"
                          for m in (i.get("evaluation") or [])[:3]]}
        for i in prof["interventions"][:4]]

    if prof["player"]["is_pitcher"]:
        mix = training_pitch_mix(engine, player_id)
        if mix:
            ctx["bullpen_pitch_mix"] = mix

    game = prof.get("game") or {}
    if game.get("pitching"):
        g = game["pitching"]
        ctx["game_pitching"] = {
            "tracked_pitches": g["pitches"], "seasons": g["years"],
            "strike_pct": g["strike_pct"], "whiff_pct": g["whiff_pct"],
            "pitch_mix": [f"{t['pitch_type']} {t['usage_pct']}% at {t['avg_velo']}"
                          for t in (g.get("pitch_types") or [])[:4]]}
    if game.get("batting"):
        b = game["batting"]
        ctx["game_hitting"] = {"pitches_seen": b["pitches_seen"],
                               "whiff_pct": b["whiff_pct"]}
    return ctx


def has_anything_to_say(ctx):
    """Don't spend a call on a player we know nothing about."""
    if not ctx:
        return False
    return bool(ctx.get("changes") or ctx.get("recent_sessions") or
                ctx.get("goals") or ctx.get("interventions"))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def cached(engine, player_id, current_basis=None):
    """The stored summary IF it is still current. None means regenerate."""
    want = current_basis or basis(engine, player_id)
    with engine.connect() as conn:
        row = conn.execute(
            select(db.ai_summaries)
            .where((db.ai_summaries.c.player_id == player_id) &
                   (db.ai_summaries.c.basis == want))).first()
    if not row:
        return None
    return {"summary": row.summary, "model": row.model,
            "created_at": str(row.created_at), "basis": row.basis}


def store(engine, player_id, want, text, model=MODEL):
    with engine.begin() as conn:
        # One row per player PER KIND -- the full note and the header line are
        # cached independently (a headline basis starts with 'hl-'), and an
        # out-of-date one is worthless, not history.
        kind = db.ai_summaries.c.basis.like("hl-%")
        conn.execute(delete(db.ai_summaries)
                     .where(db.ai_summaries.c.player_id == player_id)
                     .where(kind if want.startswith("hl-") else ~kind))
        conn.execute(insert(db.ai_summaries).values(
            player_id=player_id, basis=want, summary=text, model=model))


def _call_model(context):
    """The one place this module spends money."""
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        # 600 truncated the analyst-length note mid-sentence. Still small: this
        # runs once per player per week, only when their data has moved.
        max_tokens=1100,
        output_config={"effort": "medium"},
        system=[{"type": "text", "text": SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user",
                   "content": "Write the development note for this player.\n\n"
                              + json.dumps(context, indent=1, default=str)}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


# The one-line read at the top of a pitcher's profile. Same analyst, much
# shorter leash: it states who he is on the mound right now, and nothing else.
HEADLINE_SYSTEM = """You are the pitching development analyst for Archbishop \
Moeller High School, writing the single line under a pitcher's name at the top \
of his page.

ONE sentence, at most 28 words, plain text. Say who he is on the mound right \
now: arsenal identity (slot, fastball velo, what he leans on), plus the one \
current development headline if the context has one.

Rules: every number comes from the context; name the pitch, never generalise one \
pitch's number to the arsenal; no advice, no hedging boilerplate, no "the data \
shows"; do not repeat his name -- it is printed directly above this line."""


def generate_headline(engine, player_id, force=False, call_model=None):
    """The header line, cached exactly like the full note."""
    want = "hl-" + basis(engine, player_id)[:32]
    if not force:
        hit = cached(engine, player_id, want)
        if hit:
            return {**hit, "cached": True}

    ctx = build_context(engine, player_id)
    if not has_anything_to_say(ctx):
        return {"summary": None, "cached": False, "skipped": "no data yet"}

    if call_model is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {"summary": None, "cached": False,
                    "skipped": "no ANTHROPIC_API_KEY on the server"}

        def call_model(context):
            import anthropic
            client = anthropic.Anthropic()
            resp = client.messages.create(
                # The model thinks by default and max_tokens caps thinking AND
                # text together -- 120 truncated a headline mid-word.
                model=MODEL, max_tokens=500,
                output_config={"effort": "low"},
                system=[{"type": "text", "text": HEADLINE_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user",
                           "content": "Write the header line.\n\n"
                                      + json.dumps(context, indent=1, default=str)}])
            if resp.stop_reason == "max_tokens":
                return ""   # never cache a cut-off line
            return "".join(b.text for b in resp.content if b.type == "text").strip()

    text = call_model(ctx)
    if not text:
        return {"summary": None, "cached": False, "skipped": "model returned nothing"}
    store(engine, player_id, want, text)
    return {"summary": text, "model": MODEL, "basis": want, "cached": False}


def generate(engine, player_id, force=False, call_model=None):
    """Return a current summary, calling the model only if one is needed."""
    want = basis(engine, player_id)
    if not force:
        hit = cached(engine, player_id, want)
        if hit:
            return {**hit, "cached": True}

    ctx = build_context(engine, player_id)
    if not has_anything_to_say(ctx):
        return {"summary": None, "cached": False,
                "skipped": "no training data, changes, goals or interventions yet"}

    if call_model is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {"summary": None, "cached": False,
                    "skipped": "no ANTHROPIC_API_KEY on the server"}
        call_model = _call_model

    text = call_model(ctx)
    if not text:
        return {"summary": None, "cached": False, "skipped": "model returned nothing"}
    store(engine, player_id, want, text)
    return {"summary": text, "model": MODEL, "basis": want, "cached": False}


# ---------------------------------------------------------------------------
# The weekly job  (spec 9.3)
# ---------------------------------------------------------------------------

def players_with_new_data(engine, since):
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(
            select(db.sessions.c.player_id).distinct()
            .where(db.sessions.c.session_date >= since))]


def run_weekly(engine, days=7, everyone=False, call_model=None, dry_run=False,
               on=None):
    """For each player with new data this week, refresh the cached summary.

    Coaches then read stored text; nobody pays per view.
    """
    today = on or date.today()
    if everyone:
        with engine.connect() as conn:
            ids = [r[0] for r in conn.execute(
                select(db.players.c.id)
                .where(db.players.c.is_active == True))]  # noqa: E712
    else:
        ids = players_with_new_data(engine, today - timedelta(days=days))

    out = {"considered": len(ids), "written": 0, "cached": 0, "skipped": 0,
           "results": []}
    for pid in ids:
        if dry_run:
            ctx = build_context(engine, pid)
            out["results"].append({"player_id": pid,
                                   "would_write": has_anything_to_say(ctx),
                                   "context": ctx})
            continue
        res = generate(engine, pid, call_model=call_model)
        if res.get("cached"):
            out["cached"] += 1
        elif res.get("summary"):
            out["written"] += 1
        else:
            out["skipped"] += 1
        out["results"].append({"player_id": pid, **res})
        # The header line rides along -- tiny output, same cache discipline.
        generate_headline(engine, pid, call_model=call_model)
    return out


def main(argv):
    engine = db.get_engine()
    dry = "--dry-run" in argv
    everyone = "--all" in argv
    pid = None
    if "--player" in argv:
        pid = int(argv[argv.index("--player") + 1])

    if pid:
        res = generate(engine, pid) if not dry else {
            "context": build_context(engine, pid)}
        print(json.dumps(res, indent=2, default=str))
        return

    print("\nWeekly player summaries" + ("  (dry run -- no API calls)" if dry else ""))
    res = run_weekly(engine, everyone=everyone, dry_run=dry)
    print(f"  considered {res['considered']} player(s)")
    if dry:
        for r in res["results"]:
            print(f"    #{r['player_id']:<4} would_write={r['would_write']}")
    else:
        print(f"    written  {res['written']}")
        print(f"    cached   {res['cached']}  (no API call needed)")
        print(f"    skipped  {res['skipped']}")
        for r in res["results"]:
            if r.get("summary") and not r.get("cached"):
                print(f"\n  #{r['player_id']}: {r['summary']}")
    print()


if __name__ == "__main__":
    main(sys.argv[1:])
