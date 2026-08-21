"""
changes.py -- the What Changed engine. See PLAYER_DEV_SPEC.md section 7.

Runs on every ingest and nightly. Pure SQL + arithmetic. NO LLM INVOLVEMENT --
that is the whole point of roadmap section 9: the database computes, and the
model only explains what the database already found.

For each (player, metric) it compares a recent window against that player's own
prior baseline, and writes a row to `change_events` only when the move clears
all four gates in section 7.2. Everything downstream -- the home feed, the team
page, the player profile, and eventually the agent's `what_changed` tool -- reads
those small pre-computed rows rather than the underlying swings and pitches.

    python changes.py              detect for everyone, write results
    python changes.py --dry-run    report what would fire, write nothing
    python changes.py --player 14  one player
    python changes.py --explain    show the arithmetic for every candidate
"""

import math
import sys
from datetime import date, timedelta

from sqlalchemy import delete, func, insert, select, update

import db
import metrics


# ===========================================================================
# Statistics
# ===========================================================================
#
# scipy is not a dependency and is far too heavy to add for one p-value, so
# Student's t survival function is computed from the regularized incomplete
# beta function directly. The two-tailed p-value for t with df degrees of
# freedom is exactly I_x(df/2, 1/2) where x = df / (df + t^2).

def _betacf(a, b, x):
    """Continued-fraction expansion for the incomplete beta (Lentz's method)."""
    MAXIT, EPS, FPMIN = 300, 3e-16, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a, b, x):
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_test_p(t, df):
    """Two-tailed p-value for a t statistic. 1.0 when df is unusable."""
    if df is None or df <= 0 or not math.isfinite(t):
        return 1.0
    return _betai(df / 2.0, 0.5, df / (df + t * t))


def mean_sd(values):
    n = len(values)
    if n == 0:
        return None, None, 0
    m = sum(values) / n
    if n < 2:
        return m, 0.0, n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    return m, math.sqrt(var), n


def welch(a, b):
    """Welch's t and degrees of freedom for two unequal-variance samples.

    Note on interpretation: individual swings and pitches within one session are
    not fully independent, so this p-value is optimistic. That is deliberate and
    survivable -- p is the LOOSEST of the four gates in section 7.2, and the
    minimum-meaningful-change and effect-size gates are what actually decide
    whether a change fires. Treat p as a tie-breaker, not as evidence.
    """
    m1, s1, n1 = mean_sd(a)
    m2, s2, n2 = mean_sd(b)
    if n1 < 2 or n2 < 2:
        return 0.0, 0.0
    v1, v2 = (s1 * s1) / n1, (s2 * s2) / n2
    denom = v1 + v2
    if denom <= 0:
        return 0.0, 0.0
    t = (m1 - m2) / math.sqrt(denom)
    num = denom * denom
    den = 0.0
    if n1 > 1:
        den += (v1 * v1) / (n1 - 1)
    if n2 > 1:
        den += (v2 * v2) / (n2 - 1)
    dfree = num / den if den > 0 else 0.0
    return t, dfree


# ===========================================================================
# Reading observations
# ===========================================================================

def _observations(conn, player_id):
    """Every measurement for a player, as {(metric_key, pitch_type): [(date, session_id, value)]}.

    `pitch_type` is None for metrics that are meaningfully pooled (release point,
    everything on the hitting side) and the pitch code for those that are not.
    Comparing a fastball's ride against a slider's is comparing two different
    measurements, and the pooled average then moves whenever usage moves --
    see metrics.PITCH_SPECIFIC for the case that made this necessary.

    Both sides are read; a two-way player has swings and pitches, and the metric
    registry already knows which is which.
    """
    out = {}
    for table in (db.swings, db.pitch_metrics):
        has_pt = table is db.pitch_metrics
        cols = [db.sessions.c.session_date, db.sessions.c.id,
                table.c.metric_key, table.c.value]
        if has_pt:
            cols.append(table.c.pitch_type)
        rows = conn.execute(
            select(*cols)
            .select_from(db.sessions.join(table, table.c.session_id == db.sessions.c.id))
            .where((db.sessions.c.player_id == player_id) & (table.c.value.isnot(None)))
            .order_by(db.sessions.c.session_date)).all()
        for r in rows:
            if has_pt and metrics.is_pitch_specific(r.metric_key):
                # An unlabelled pitch can't be attributed to an arsenal slot, so
                # it contributes to nothing rather than polluting a real one.
                if not r.pitch_type:
                    continue
                key = (r.metric_key, r.pitch_type)
            else:
                key = (r.metric_key, None)
            out.setdefault(key, []).append(
                (r.session_date, r.id, float(r.value)))
    return out


def observations_for(obs, metric_key, pitch_type=None):
    """Pick one series out of _observations(), which is keyed by (metric, pitch).

    Returns (rows, resolved_pitch_type). For a pitch-specific metric asked for
    without a pitch, this resolves to the slot he throws most rather than pooling
    incomparable pitches -- and tells the caller which one it picked, so nothing
    reports a fastball number as though it covered the whole arsenal.
    """
    if (metric_key, pitch_type) in obs:
        return obs[(metric_key, pitch_type)], pitch_type
    if pitch_type is None:
        candidates = {k[1]: v for k, v in obs.items() if k[0] == metric_key}
        if candidates:
            best = max(candidates.items(), key=lambda kv: len(kv[1]))
            return best[1], best[0]
    return [], pitch_type


def available_pitch_types(obs, metric_key):
    return sorted(k[1] for k in obs if k[0] == metric_key and k[1])


def _baseline_sessions(conn, player_id):
    """Sessions the coach explicitly marked as a baseline, newest first."""
    return [r.id for r in conn.execute(
        select(db.sessions.c.id)
        .where((db.sessions.c.player_id == player_id) &
               (db.sessions.c.purpose == "baseline"))
        .order_by(db.sessions.c.session_date.desc()))]


def _as_date(v):
    if isinstance(v, date):
        return v
    if v is None:
        return None
    from datetime import datetime
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


# ===========================================================================
# The engine
# ===========================================================================

def evaluate_metric(observations, metric, baseline_session_ids=(),
                    k=None, baseline_days=None, pitch_type=None):
    """Compare a recent window with a prior baseline for one metric.

    Returns a verdict dict whether or not it fires, so the caller can explain
    why something DIDN'T -- "insufficient_data" is a useful answer for a coach,
    and the profile shows it honestly.
    """
    k = k or metrics.RECENT_SESSIONS
    baseline_days = baseline_days or metrics.BASELINE_DAYS

    by_session = {}
    for d, sid, v in observations:
        by_session.setdefault(sid, {"date": _as_date(d), "values": []})["values"].append(v)
    if not by_session:
        return {"status": "no_data"}

    ordered = sorted(by_session.items(), key=lambda kv: kv[1]["date"])
    recent_ids = [sid for sid, _ in ordered[-k:]]
    recent_start = by_session[recent_ids[0]]["date"]
    recent_end = by_session[recent_ids[-1]]["date"]
    cutoff = recent_start - timedelta(days=baseline_days)

    recent_vals, base_vals, base_ids = [], [], []
    for sid, rec in ordered:
        if sid in recent_ids:
            recent_vals += rec["values"]
        elif cutoff <= rec["date"] < recent_start:
            base_vals += rec["values"]
            base_ids.append(sid)

    # A session the coach marked 'baseline' is the reference point by name, even
    # if it has aged out of the rolling window.
    for sid in baseline_session_ids:
        if sid in by_session and sid not in base_ids and sid not in recent_ids:
            base_vals += by_session[sid]["values"]
            base_ids.append(sid)

    n_recent, n_base = len(recent_vals), len(base_vals)
    verdict = {
        "metric_key": metric.key, "label": metric.label, "unit": metric.unit,
        "recent_sessions": len(recent_ids), "baseline_sessions": len(base_ids),
        "n_recent": n_recent, "n_baseline": n_base,
        "window_start": str(recent_start), "window_end": str(recent_end),
        "baseline_start": str(cutoff), "min_n": metric.min_n,
    }

    if n_recent < metric.min_n or n_base < metric.min_n:
        verdict["status"] = "insufficient_data"
        verdict["reason"] = (
            f"needs {metric.min_n} in each window; have {n_recent} recent, {n_base} baseline")
        return verdict

    rm, rsd, _ = mean_sd(recent_vals)
    bm, bsd, _ = mean_sd(base_vals)
    delta = rm - bm
    effect = (delta / bsd) if bsd and bsd > 0 else 0.0
    t, dfree = welch(recent_vals, base_vals)
    p = t_test_p(t, dfree)

    verdict.update({
        "recent_mean": round(rm, metric.decimals), "baseline_mean": round(bm, metric.decimals),
        "baseline_sd": round(bsd, 3) if bsd is not None else None,
        "delta": round(delta, metric.decimals),
        "effect_size": round(effect, 3), "p_value": round(p, 4),
        "direction": "up" if delta > 0 else "down",
    })

    # --- the four gates (spec 7.2) -----------------------------------------
    gates = {
        "sample": True,
        "mmc": abs(delta) >= metric.mmc,
        "effect": abs(effect) >= metrics.MIN_EFFECT_SIZE,
        "p": p < metrics.MAX_P_VALUE,
    }
    verdict["gates"] = gates
    if not all(gates.values()):
        verdict["status"] = "no_change"
        failed = [g for g, ok in gates.items() if not ok]
        verdict["reason"] = "did not clear: " + ", ".join(failed)
        return verdict

    verdict["status"] = "change"
    verdict["severity"] = ("significant"
                           if abs(effect) >= metrics.SIGNIFICANT_EFFECT
                           and p < metrics.SIGNIFICANT_P else "notable")
    verdict["favorable"] = favorable(metric, bm, rm)
    verdict["summary"] = summarize(metric, rm, bm, delta, len(recent_ids), pitch_type)
    return verdict


def favorable(metric, baseline_mean, recent_mean):
    """Is this move good for this player? Derived from polarity, never the sign.

    A target-band metric has no simple direction, so 'favorable' means it moved
    TOWARD the band -- a hitter whose attack angle climbed from 12 to 22 degrees
    has not improved, and must not be told he has.
    """
    verdict = metric.favorable(recent_mean - baseline_mean)
    if verdict is not None:
        return verdict
    if metric.polarity == metrics.TARGET_BAND and metric.target_band:
        lo, hi = metric.target_band

        def distance(v):
            return 0.0 if lo <= v <= hi else min(abs(v - lo), abs(v - hi))

        return distance(recent_mean) < distance(baseline_mean)
    return None


def summarize(metric, recent_mean, baseline_mean, delta, n_sessions, pitch_type=None):
    """The one plain-English line stored on the row.

    This is the compact context the roadmap's section 9 example asks for -- what
    the agent reads instead of the underlying pitches. The pitch type is named
    when there is one, because "induced vertical break is down" reads as a
    problem with the pitcher while "his changeup's induced vertical break is
    down" is the thing a coach can actually act on.
    """
    fmt = metrics.format_value
    sign = "+" if delta > 0 else ""
    what = metric.label
    if pitch_type:
        label = metrics.PITCH_TYPE_LABELS.get(pitch_type, pitch_type)
        what = f"{label} {metric.label[0].lower()}{metric.label[1:]}"
    return (f"{what} {fmt(metric.key, recent_mean)} vs "
            f"{fmt(metric.key, baseline_mean)} baseline "
            f"({sign}{fmt(metric.key, delta)} {metric.unit}) "
            f"over {n_sessions} session{'' if n_sessions == 1 else 's'}")


# ===========================================================================
# Running it
# ===========================================================================

def compute_player(engine, player_id, write=True, explain=False):
    """Detect changes for one player. Returns every verdict, fired or not."""
    with engine.connect() as conn:
        obs = _observations(conn, player_id)
        baseline_ids = _baseline_sessions(conn, player_id)
        # Suppression (spec 7.3): a change already reported for a window that
        # overlaps this one must not be reported again every week.
        already = {}
        for r in conn.execute(
                select(db.change_events.c.metric_key,
                       db.change_events.c.pitch_type,
                       func.max(db.change_events.c.detected_on).label("last"))
                .where(db.change_events.c.player_id == player_id)
                .group_by(db.change_events.c.metric_key,
                          db.change_events.c.pitch_type)):
            already[(r.metric_key, r.pitch_type)] = _as_date(r.last)

    verdicts = []
    for (key, pitch_type), rows in obs.items():
        metric = metrics.get(key)
        if metric is None:
            continue                     # stored but not registered -- not surfaced
        v = evaluate_metric(rows, metric, baseline_ids, pitch_type=pitch_type)
        v["player_id"] = player_id
        v["pitch_type"] = pitch_type
        verdicts.append(v)

    fired = []
    for v in verdicts:
        if v.get("status") != "change":
            continue
        # Suppression is per pitch as well: a fastball finding must not silence a
        # slider one for the same metric.
        last = already.get((v["metric_key"], v.get("pitch_type")))
        if last is not None and last >= _as_date(v["window_start"]):
            v["status"] = "suppressed"
            v["reason"] = f"already reported on {last} for an overlapping window"
            continue
        fired.append(v)

    if write and fired:
        with engine.begin() as conn:
            for v in fired:
                conn.execute(insert(db.change_events).values(
                    player_id=player_id, metric_key=v["metric_key"],
                    pitch_type=v.get("pitch_type"),
                    detected_on=_as_date(v["window_end"]),
                    direction=v["direction"], recent_mean=v["recent_mean"],
                    baseline_mean=v["baseline_mean"], delta=v["delta"],
                    effect_size=v["effect_size"], p_value=v["p_value"],
                    severity=v["severity"], favorable=v["favorable"],
                    n_recent=v["n_recent"], n_baseline=v["n_baseline"],
                    summary=v["summary"], acknowledged=False))
                # Re-anchor: fold the recent window into the stored baseline so
                # the next run compares against the NEW normal, not the old one.
                _store_baseline(conn, player_id, v)

    if explain:
        for v in sorted(verdicts, key=lambda x: x.get("metric_key", "")):
            print(f"    {v.get('metric_key','?'):<26} {v.get('status','?'):<18} "
                  f"{v.get('reason', v.get('summary', ''))}")
    return verdicts, fired


def _store_baseline(conn, player_id, verdict):
    """Write the re-anchored baseline for a metric that just changed."""
    n = verdict["n_recent"] + verdict["n_baseline"]
    combined = ((verdict["recent_mean"] * verdict["n_recent"] +
                 verdict["baseline_mean"] * verdict["n_baseline"]) / n) if n else None
    end = _as_date(verdict["window_end"])
    # '' rather than NULL for the pooled case -- see db.player_baselines.
    pt = verdict.get("pitch_type") or ""
    conn.execute(delete(db.player_baselines).where(
        (db.player_baselines.c.player_id == player_id) &
        (db.player_baselines.c.metric_key == verdict["metric_key"]) &
        (db.player_baselines.c.pitch_type == pt) &
        (db.player_baselines.c.window_end == end)))
    conn.execute(insert(db.player_baselines).values(
        player_id=player_id, metric_key=verdict["metric_key"], pitch_type=pt,
        window_end=end, window_start=_as_date(verdict["baseline_start"]),
        n=n, mean=combined, sd=verdict.get("baseline_sd")))


def compute_all(engine, write=True, explain=False, player_id=None):
    with engine.connect() as conn:
        q = select(db.players.c.id, db.players.c.first_name, db.players.c.last_name)
        if player_id:
            q = q.where(db.players.c.id == player_id)
        else:
            q = q.where(db.players.c.is_active == True)  # noqa: E712
        people = conn.execute(q).all()

    total_fired, examined, blocked = [], 0, {}
    for person in people:
        verdicts, fired = compute_player(engine, person.id, write=write, explain=False)
        examined += len(verdicts)
        for v in verdicts:
            blocked[v.get("status", "?")] = blocked.get(v.get("status", "?"), 0) + 1
        if fired and explain:
            print(f"  {person.first_name} {person.last_name}")
            for v in fired:
                print(f"    {v['severity']:<12} {v['summary']}")
        total_fired += [dict(v, player=f"{person.first_name} {person.last_name}")
                        for v in fired]

    return {"players": len(people), "metrics_examined": examined,
            "fired": len(total_fired), "by_status": blocked, "events": total_fired}


def acknowledge(engine, event_id, acknowledged=True):
    """Acknowledged events drop off the roster feed but stay on the player's
    timeline as history (spec 7.3)."""
    with engine.begin() as conn:
        conn.execute(update(db.change_events)
                     .where(db.change_events.c.id == event_id)
                     .values(acknowledged=acknowledged))


# ===========================================================================
# Intervention pre/post  (spec 7.4)
# ===========================================================================
#
# The engine reports the comparison. It does NOT claim causation, and the
# wording it writes back reflects that -- coaches own the interpretation.

PRE_WINDOW_DAYS = 90


def evaluate_intervention(engine, intervention_id, write=True):
    """Compare every metric before and after an intervention's date.

    Deviation from the spec's first draft, recorded deliberately: the spec said
    'everything >=30 days before the date is pre', which implies a 30-day
    washout. At high-school session frequency that usually discards ALL the
    pre-intervention data, so pre is simply the sessions before the date within
    PRE_WINDOW_DAYS. Set a washout here if the data ever gets dense enough.
    """
    with engine.connect() as conn:
        iv = conn.execute(select(db.interventions)
                          .where(db.interventions.c.id == intervention_id)).first()
        if not iv:
            return None
        obs = _observations(conn, iv.player_id)
        goal_metric = None
        if iv.goal_id:
            g = conn.execute(select(db.goals.c.metric_key)
                             .where(db.goals.c.id == iv.goal_id)).first()
            goal_metric = g.metric_key if g else None

    when = _as_date(iv.intervention_date)
    start = when - timedelta(days=PRE_WINDOW_DAYS)

    results = []
    for (key, pitch_type), rows in obs.items():
        metric = metrics.get(key)
        if metric is None:
            continue
        pre = [v for d, _s, v in rows if start <= _as_date(d) < when]
        post = [v for d, _s, v in rows if _as_date(d) >= when]
        if len(pre) < metric.min_n or len(post) < metric.min_n:
            continue
        pm, psd, _ = mean_sd(pre)
        qm, _qsd, _ = mean_sd(post)
        delta = qm - pm
        effect = (delta / psd) if psd and psd > 0 else 0.0
        t, dfree = welch(post, pre)
        p = t_test_p(t, dfree)
        moved = abs(delta) >= metric.mmc and abs(effect) >= metrics.MIN_EFFECT_SIZE
        label = metric.label
        if pitch_type:
            label = (f"{metrics.PITCH_TYPE_LABELS.get(pitch_type, pitch_type)} "
                     f"{metric.label[0].lower()}{metric.label[1:]}")
        results.append({
            "metric_key": key, "pitch_type": pitch_type,
            "label": label, "unit": metric.unit,
            "is_goal_metric": key == goal_metric,
            "pre_mean": round(pm, metric.decimals), "post_mean": round(qm, metric.decimals),
            "delta": round(delta, metric.decimals), "effect_size": round(effect, 3),
            "p_value": round(p, 4), "n_pre": len(pre), "n_post": len(post),
            "moved": moved, "favorable": favorable(metric, pm, qm) if moved else None,
        })

    results.sort(key=lambda r: (not r["is_goal_metric"], -abs(r["effect_size"])))

    # Write an outcome back, judged on the goal metric where there is one.
    outcome = "pending"
    judged = next((r for r in results if r["is_goal_metric"]), None) or \
        (results[0] if results else None)
    if judged:
        if not judged["moved"]:
            outcome = "no_change"
        elif judged["favorable"]:
            outcome = "working"
        else:
            outcome = "reverted"
    if write and results:
        with engine.begin() as conn:
            conn.execute(update(db.interventions)
                         .where(db.interventions.c.id == intervention_id)
                         .values(outcome=outcome))

    return {"intervention_id": intervention_id, "date": str(when),
            "title": iv.title, "outcome": outcome, "metrics": results}


def evaluate_all_interventions(engine):
    with engine.connect() as conn:
        ids = [r.id for r in conn.execute(select(db.interventions.c.id))]
    return [r for r in (evaluate_intervention(engine, i) for i in ids) if r]


# ===========================================================================
# CLI
# ===========================================================================

def main(argv):
    engine = db.get_engine()
    write = "--dry-run" not in argv
    explain = "--explain" in argv
    pid = None
    if "--player" in argv:
        pid = int(argv[argv.index("--player") + 1])

    print(f"\nWhat Changed{'  (dry run -- nothing written)' if not write else ''}")
    print(f"  recent window   last {metrics.RECENT_SESSIONS} sessions")
    print(f"  baseline window {metrics.BASELINE_DAYS} days before that")
    print(f"  gates           |delta| >= mmc, |effect| >= {metrics.MIN_EFFECT_SIZE}, "
          f"p < {metrics.MAX_P_VALUE}\n")

    result = compute_all(engine, write=write, explain=explain, player_id=pid)
    print(f"  players examined   {result['players']}")
    print(f"  metrics examined   {result['metrics_examined']}")
    for status, n in sorted(result["by_status"].items()):
        print(f"    {status:<20} {n}")
    print(f"  changes fired      {result['fired']}\n")
    for e in result["events"]:
        print(f"    {e['player']:<22} {e['severity']:<12} {e['summary']}")

    ivs = evaluate_all_interventions(engine)
    if ivs:
        print(f"\n  interventions evaluated {len(ivs)}")
        for iv in ivs:
            print(f"    {iv['date']}  {iv['title'][:40]:<40} -> {iv['outcome']}")
    print()


if __name__ == "__main__":
    main(sys.argv[1:])
