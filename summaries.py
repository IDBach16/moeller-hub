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

SYSTEM = """You write short player-development notes for the coaching staff at \
Archbishop Moeller High School.

You are given a compact summary that the database has already computed: recent \
training sessions, detected changes against the player's own baseline, active \
development goals, and any logged interventions. You are NOT given the raw data, \
and you must not ask for it.

Write 2-4 sentences for the coaching staff. Rules:
- Lead with what actually changed, and by how much. Use the numbers you are given.
- Never invent a number. If something isn't in the context, don't mention it.
- A change is a comparison, not a cause. If an intervention is logged near a change, \
you may note the timing, but do not claim the intervention caused it.
- Say plainly when there isn't enough data to conclude anything. That is a useful \
answer, not a failure.
- Baseball shorthand is fine. Plain text only -- no markdown, no bullets, no headers.
- Do not address the player. You're writing for coaches about him."""


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
        # One row per player: an out-of-date summary is worthless, not history.
        conn.execute(delete(db.ai_summaries)
                     .where(db.ai_summaries.c.player_id == player_id))
        conn.execute(insert(db.ai_summaries).values(
            player_id=player_id, basis=want, summary=text, model=model))


def _call_model(context):
    """The one place this module spends money."""
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=600,
        output_config={"effort": "medium"},
        system=[{"type": "text", "text": SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user",
                   "content": "Write the development note for this player.\n\n"
                              + json.dumps(context, indent=1, default=str)}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


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
