"""
profiles.py -- assembling the unified player profile. See PLAYER_DEV_SPEC.md 8.3.

The roadmap's point is that a coach shouldn't have to know which dashboard holds
the answer. So this module pulls every source we currently have into one shape:

    ingested sessions   swings / pitch_metrics      (Phase B -- Blast/HitTrax/Rapsodo)
    game performance    season_pitches.csv          (AWRE, 2024-2026)
    official stats      Team Stats GCL API          (2026 box score)
    charted bullpens    Charting App API            (live)

Blocks that are genuinely not built yet (What Changed, goals, interventions, the
AI summary, video evidence) return an explicit empty state rather than being
faked. A profile that says "no baseline yet" is useful; one that invents a
number is worse than no page at all.
"""

import threading
import time

from sqlalchemy import distinct, func, select

import db
import metrics

# The Team Stats GCL API is a network call and its data changes rarely. Cache it
# so opening five player pages doesn't make five round trips.
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_TTL = 600  # seconds


def _cached(key, fn):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    try:
        val = fn()
    except Exception as e:
        val = {"error": str(e)}
    with _CACHE_LOCK:
        _CACHE[key] = (now, val)
    return val


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------

def roster(engine, q=None, side=None):
    """Every active player, with enough context to pick one off a grid."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.players.c.id, db.players.c.slug, db.players.c.first_name,
                   db.players.c.last_name, db.players.c.class_year,
                   db.players.c.primary_pos, db.players.c.bats,
                   db.players.c.throws, db.players.c.is_pitcher)
            .where(db.players.c.is_active == True)  # noqa: E712
            .order_by(db.players.c.last_name, db.players.c.first_name)).all()

        # session counts + latest date, in one query rather than one per player
        counts = {r.player_id: (r.n, r.last) for r in conn.execute(
            select(db.sessions.c.player_id,
                   func.count().label("n"),
                   func.max(db.sessions.c.session_date).label("last"))
            .group_by(db.sessions.c.player_id))}
        open_goals = {r.player_id: r.n for r in conn.execute(
            select(db.goals.c.player_id, func.count().label("n"))
            .where(db.goals.c.status == "active")
            .group_by(db.goals.c.player_id))}
        changes = {r.player_id: r.n for r in conn.execute(
            select(db.change_events.c.player_id, func.count().label("n"))
            .where(db.change_events.c.acknowledged == False)  # noqa: E712
            .group_by(db.change_events.c.player_id))}

    out = []
    for r in rows:
        n, last = counts.get(r.id, (0, None))
        out.append({
            "id": r.id, "slug": r.slug,
            "name": f"{r.first_name} {r.last_name}",
            "last_name": r.last_name,
            "class_year": r.class_year, "pos": r.primary_pos,
            "bats": r.bats, "throws": r.throws,
            "is_pitcher": bool(r.is_pitcher),
            "sessions": n, "last_session": str(last) if last else None,
            "open_goals": open_goals.get(r.id, 0),
            "changes": changes.get(r.id, 0),
        })

    if side == "pitching":
        out = [p for p in out if p["is_pitcher"]]
    elif side == "hitting":
        out = [p for p in out if not p["is_pitcher"]]
    if q:
        ql = q.lower().strip()
        out = [p for p in out if ql in p["name"].lower()]
    return out


# The attack-angle scale the grid's band bar is drawn on. Wider than the target
# band so an out-of-band hitter still lands ON the bar instead of pinning.
AA_SCALE = (-10.0, 25.0)


def hitter_cards(engine):
    """Per-hitter visual summary for the players grid, one pass over swings.

    Empty today -- no Blast or HitTrax export has been ingested -- and that is
    the point of building it now: the grid lights up by itself the morning the
    first export lands, the same way the pitcher cards appeared when Rapsodo
    loaded. Until then every hitter keeps the quiet card, which is honest.
    """
    KEYS = ["bat_speed", "attack_angle", "on_plane_efficiency",
            "exit_velocity", "max_exit_velocity"]
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.swings.c.player_id, db.sessions.c.session_date,
                   db.swings.c.metric_key, db.swings.c.value)
            .select_from(db.swings.join(
                db.sessions, db.sessions.c.id == db.swings.c.session_id))
            .where(db.swings.c.metric_key.in_(KEYS))
            .where(db.swings.c.value.isnot(None))).all()
    if not rows:
        return {}

    acc: dict[int, dict] = {}
    for r in rows:
        p = acc.setdefault(r.player_id, {k: [] for k in KEYS})
        p[r.metric_key].append(float(r.value))
        if r.metric_key == "bat_speed":
            p.setdefault("_by_date", {}).setdefault(str(r.session_date), []) \
             .append(float(r.value))

    band = metrics.get("attack_angle").target_band or (5.0, 15.0)
    lo, hi = AA_SCALE
    pct = lambda v: round(100.0 * (min(max(v, lo), hi) - lo) / (hi - lo), 1)

    out = {}
    for pid, d in acc.items():
        bs = d["bat_speed"]
        if len(bs) < 5:                 # a handful of swings isn't a profile
            continue
        card = {
            "bat": round(sum(bs) / len(bs), 1),
            "bat_max": round(max(bs), 1),
            "n_swings": len(bs),
        }
        if d["attack_angle"]:
            aa = sum(d["attack_angle"]) / len(d["attack_angle"])
            card["aa"] = round(aa, 1)
            card["aa_pct"] = pct(aa)
            card["aa_in_band"] = band[0] <= aa <= band[1]
            card["band_lo_pct"], card["band_hi_pct"] = pct(band[0]), pct(band[1])
        if d["on_plane_efficiency"]:
            card["ope"] = int(round(sum(d["on_plane_efficiency"])
                                    / len(d["on_plane_efficiency"])))
        ev = d["exit_velocity"] or []
        if ev:
            card["ev"] = round(sum(ev) / len(ev), 1)
            card["ev_max"] = round(max(d["max_exit_velocity"] or ev), 1)
        # Bat-speed trend, one point per session, drawn server-side so the grid
        # ships no extra JS. Points land in a 74x74 box with a 6px inset.
        by_date = sorted(d.get("_by_date", {}).items())
        if len(by_date) >= 2:
            means = [sum(v) / len(v) for _dt, v in by_date]
            mlo, mhi = min(means), max(means)
            span = (mhi - mlo) or 1.0
            n = len(means)
            card["spark"] = " ".join(
                f"{6 + i * (62 / (n - 1)):.1f},{64 - (m - mlo) / span * 54:.1f}"
                for i, m in enumerate(means))
        out[pid] = card
    return out


# ---------------------------------------------------------------------------
# One player
# ---------------------------------------------------------------------------

def _cached_summary(engine, player_id):
    """The stored development note, only if it is still current for this data."""
    try:
        import summaries
        hit = summaries.cached(engine, player_id)
        if hit:
            # Structured notes render field-by-field; older prose notes leave
            # this None and fall back to a plain paragraph.
            hit["note"] = summaries.parse_note(hit["summary"])
        return hit
    except Exception:
        return None


def _player_row(conn, slug=None, player_id=None):
    q = select(db.players)
    q = q.where(db.players.c.slug == slug) if slug else \
        q.where(db.players.c.id == player_id)
    return conn.execute(q).first()


def _aliases(conn, player_id):
    return {r.source: r.alias for r in conn.execute(
        select(db.player_aliases.c.source, db.player_aliases.c.alias)
        .where(db.player_aliases.c.player_id == player_id))}


def _training(conn, player_id, side):
    """Session-level summaries of ingested device data -- never raw rows.

    Same discipline as the agent tools (spec 9.2): a hitter with 900 Blast
    swings over 12 sessions produces 12 rows here, not 900.
    """
    table = db.swings if side == "hitting" else db.pitch_metrics
    rows = conn.execute(
        select(db.sessions.c.id, db.sessions.c.session_date,
               db.sessions.c.session_type, db.sessions.c.source,
               db.sessions.c.purpose, db.sessions.c.notes,
               table.c.metric_key,
               func.count().label("n"),
               func.avg(table.c.value).label("mean"),
               func.max(table.c.value).label("max"))
        .select_from(db.sessions.join(table, table.c.session_id == db.sessions.c.id))
        .where(db.sessions.c.player_id == player_id)
        .group_by(db.sessions.c.id, db.sessions.c.session_date,
                  db.sessions.c.session_type, db.sessions.c.source,
                  db.sessions.c.purpose, db.sessions.c.notes, table.c.metric_key)
        .order_by(db.sessions.c.session_date.desc())).all()

    # Stored to be plotted, not trended: an average plate location is meaningless
    # as a session stat (a pitcher who misses equally high and low averages to the
    # middle of the zone). The report card charts them instead.
    PLOT_ONLY = {"plate_side", "plate_height"}

    sessions = {}
    for r in rows:
        if r.metric_key in PLOT_ONLY:
            continue
        s = sessions.setdefault(r.id, {
            "id": r.id, "date": str(r.session_date), "type": r.session_type,
            "source": r.source, "purpose": r.purpose, "notes": r.notes,
            "metrics": {},
        })
        m = metrics.get(r.metric_key)
        s["metrics"][r.metric_key] = {
            "label": m.label if m else r.metric_key,
            "unit": m.unit if m else "",
            "n": r.n,
            "mean": round(float(r.mean), m.decimals if m else 2),
            "max": round(float(r.max), m.decimals if m else 2),
        }
    ordered = sorted(sessions.values(), key=lambda s: s["date"], reverse=True)
    keys = sorted({k for s in ordered for k in s["metrics"]})
    return ordered, keys


def _status_tiles(conn, player_id, side, training):
    """The headline metric row. Values come from the most recent session that
    carried each metric; the baseline column stays empty until Phase D computes
    one, and says so rather than showing the same number twice."""
    baselines = {r.metric_key: r for r in conn.execute(
        select(db.player_baselines)
        .where(db.player_baselines.c.player_id == player_id)
        .order_by(db.player_baselines.c.window_end.desc()))}

    tiles = []
    for m in metrics.headline(side):
        latest = None
        for s in training:                      # training is newest-first
            if m.key in s["metrics"]:
                latest = (s["date"], s["metrics"][m.key])
                break
        if latest is None:
            continue
        date, agg = latest
        b = baselines.get(m.key)
        delta = (agg["mean"] - float(b.mean)) if b and b.mean is not None else None
        tiles.append({
            "key": m.key, "label": m.label, "unit": m.unit,
            "value": agg["mean"], "n": agg["n"], "date": date,
            "baseline": round(float(b.mean), m.decimals) if b and b.mean is not None else None,
            "delta": round(delta, m.decimals) if delta is not None else None,
            "favorable": m.favorable(delta) if delta is not None else None,
            "in_band": m.in_band(agg["mean"]),
            "band": list(m.target_band) if m.target_band else None,
        })
    return tiles


def _changes(conn, player_id):
    rows = conn.execute(
        select(db.change_events)
        .where(db.change_events.c.player_id == player_id)
        .order_by(db.change_events.c.detected_on.desc()).limit(20)).all()
    def label_for(row):
        m = metrics.get(row.metric_key)
        base = m.label if m else row.metric_key
        # Name the pitch. "Horizontal break is up" reads as a fact about the
        # pitcher; "Fastball horizontal break is up" is the one a coach can act on.
        if row.pitch_type:
            pitch = metrics.PITCH_TYPE_LABELS.get(row.pitch_type, row.pitch_type)
            return f"{pitch} {base[0].lower()}{base[1:]}"
        return base

    return [{"id": r.id, "metric_key": r.metric_key, "pitch_type": r.pitch_type,
             "label": label_for(r),
             "detected_on": str(r.detected_on), "direction": r.direction,
             "delta": r.delta, "effect_size": r.effect_size,
             "p_value": r.p_value, "severity": r.severity, "favorable": r.favorable,
             "n_recent": r.n_recent, "n_baseline": r.n_baseline,
             "summary": r.summary, "acknowledged": bool(r.acknowledged)}
            for r in rows]


def latest_by_metric(training):
    """Most recent session mean per metric. `training` is newest-first."""
    out = {}
    for s in training:
        for key, agg in s["metrics"].items():
            out.setdefault(key, {"value": agg["mean"], "date": s["date"], "n": agg["n"]})
    return out


def goal_progress(goal, current):
    """How far along a measurable goal is.

    Needs three numbers: where he started, where he is, where he's going. A goal
    with no start_value (or no data yet) reports 'unknown' rather than guessing
    a denominator -- a fake progress bar is worse than an honest blank.
    """
    target = goal.get("target_value")
    start = goal.get("start_value")
    direction = goal.get("direction")
    m = metrics.get(goal.get("metric_key") or "")

    if current is None:
        return {"state": "no_data",
                "note": "no sessions with this metric yet"}
    if target is None:
        return {"state": "narrative", "current": current}

    met = False
    if direction == "increase":
        met = current >= target
    elif direction == "decrease":
        met = current <= target
    elif direction == "target_band" and m and m.target_band:
        met = m.in_band(current)

    pct = None
    if start is not None and direction in ("increase", "decrease"):
        span = target - start
        if span != 0:
            pct = max(0.0, min(1.0, (current - start) / span))
    return {"state": "met" if met else "in_progress",
            "current": current, "start": start, "target": target,
            "pct": round(pct * 100) if pct is not None else None,
            "remaining": round(target - current, m.decimals if m else 2)}


def _development(conn, player_id, training):
    latest = latest_by_metric(training)

    goals = []
    for r in conn.execute(select(db.goals)
                          .where(db.goals.c.player_id == player_id)
                          .order_by(db.goals.c.id.desc())):
        m = metrics.get(r.metric_key or "")
        g = {"id": r.id, "title": r.title, "metric_key": r.metric_key,
             "metric_label": m.label if m else None,
             "unit": m.unit if m else "",
             "direction": r.direction, "target_value": r.target_value,
             "start_value": r.start_value,
             "detail": r.detail, "set_by": r.set_by,
             "set_on": str(r.set_on) if r.set_on else None,
             "review_on": str(r.review_on) if r.review_on else None,
             "status": r.status}
        if r.metric_key:
            cur = latest.get(r.metric_key)
            g["progress"] = goal_progress(g, cur["value"] if cur else None)
            g["current_on"] = cur["date"] if cur else None
        else:
            g["progress"] = {"state": "narrative"}
        goals.append(g)

    interventions = [{"id": r.id, "title": r.title, "category": r.category,
                      "date": str(r.intervention_date), "detail": r.detail,
                      "coach": r.coach, "outcome": r.outcome,
                      "goal_id": r.goal_id,
                      "review_on": str(r.review_on) if r.review_on else None}
                     for r in conn.execute(
                         select(db.interventions)
                         .where(db.interventions.c.player_id == player_id)
                         .order_by(db.interventions.c.intervention_date.desc()))]
    return goals, interventions


def _game_performance(name, is_pitcher):
    """AWRE game pitch data -- the layer that already works today."""
    import agent
    out = {}
    if is_pitcher:
        try:
            r = agent.tool_season_pitching(name)
            out["pitching"] = None if r.get("error") else r
            # Tag each game pitch type with our canonical code so the arsenal is
            # coloured the same as the Rapsodo card and the two can be read side
            # by side. "Breaking Ball" resolves to None on purpose -- it could be a
            # slider or a curve, so it stays grey rather than borrowing a colour.
            if out["pitching"]:
                for t in out["pitching"].get("pitch_types") or []:
                    t["code"] = metrics.normalize_pitch_type(t.get("pitch_type"))
        except Exception as e:
            out["pitching_error"] = str(e)
    try:
        r = agent.tool_season_batting(name)
        out["batting"] = None if r.get("error") else r
    except Exception as e:
        out["batting_error"] = str(e)
    return out


# Columns that identify the row rather than describe performance. Left in, they
# rendered as "pitching - player" and "pitching - class_year" on the profile.
_OFFICIAL_SKIP = {"player", "name", "player_name", "class_year", "class", "year",
                  "jersey", "number", "no", "team", "is_totals", "pos", "position",
                  # Bookkeeping and components of a stat we already show.
                  "updated_at", "ip_full", "ip_partial", "errors", "id"}

# The book's column names, in the order a coach reads a line.
_OFFICIAL_ORDER = ["g", "gs", "w", "l", "sv", "ip", "era", "whip", "h", "r", "er",
                   "bb", "so", "k", "hbp",
                   "avg", "obp", "slg", "ops", "pa", "ab", "2b", "3b", "hr", "rbi",
                   "sb", "cs"]
_OFFICIAL_LABELS = {"g": "G", "gs": "GS", "w": "W", "l": "L", "sv": "SV", "ip": "IP",
                    "era": "ERA", "whip": "WHIP", "h": "H", "r": "R", "er": "ER",
                    "bb": "BB", "so": "K", "k": "K", "hbp": "HBP", "avg": "AVG",
                    "obp": "OBP", "slg": "SLG", "ops": "OPS", "pa": "PA", "ab": "AB",
                    "2b": "2B", "3b": "3B", "hr": "HR", "rbi": "RBI", "sb": "SB",
                    "cs": "CS", "sho": "SHO",
                    # Rate stats. High school games are seven innings, so the book
                    # keeps per-7 alongside the conventional per-9.
                    "h7": "H/7", "bb7": "BB/7", "k7": "K/7", "hr7": "HR/7",
                    "k9": "K/9", "bb9": "BB/9", "bbk": "BB/K"}


# A book line is read at a glance: ERA to hundredths, rate stats to thousandths.
# The source hands back full float precision ("ERA 1.697").
_OFFICIAL_DP = {"ERA": 2, "WHIP": 2, "IP": 1,
                "AVG": 3, "OBP": 3, "SLG": 3, "OPS": 3}


def _fmt_official(label, value):
    dp = _OFFICIAL_DP.get(label)
    if dp is None:
        return value
    try:
        return f"{float(value):.{dp}f}"
    except (TypeError, ValueError):
        return value


def _clean_official(row):
    """Box-score row -> ordered [{label, value}], identity columns dropped.

    Deduped by LABEL, not by column name: some exports carry both `so` and `k`,
    and keying on the column showed strikeouts twice.
    """
    pairs, used_labels = [], set()

    def add(key, orig, v):
        label = _OFFICIAL_LABELS.get(key, str(orig).upper())
        if label in used_labels or v in (None, ""):
            return
        used_labels.add(label)
        pairs.append({"label": label, "value": _fmt_official(label, v)})

    lowered = {str(k).strip().lower(): (k, v) for k, v in row.items()}
    for key in _OFFICIAL_ORDER:
        if key in lowered:
            add(key, lowered[key][0], lowered[key][1])
    # Anything the book has that we didn't anticipate, rather than dropping it.
    for key, (orig, v) in lowered.items():
        if key in _OFFICIAL_SKIP:
            continue
        add(key, orig, v)
    return pairs


def _team_stats_row(name, is_pitcher):
    """This player's row in the official 2026 GCL box-score stats."""
    import agent

    def fetch(kind):
        return agent.tool_team_stats(kind)

    out = {}
    for kind in (["pitching", "batting"] if is_pitcher else ["batting"]):
        data = _cached(f"team_{kind}", lambda k=kind: fetch(k))
        if not isinstance(data, dict) or "rows" not in data:
            continue
        last = name.split()[-1].lower()
        first = name.split()[0].lower()
        for row in data["rows"]:
            label = " ".join(str(v) for v in row.values() if isinstance(v, str)).lower()
            if last in label and (first[:3] in label or first in label):
                out[kind] = _clean_official(row)
                break
    return out


def profile(engine, slug):
    with engine.connect() as conn:
        p = _player_row(conn, slug=slug)
        if not p:
            return None
        name = f"{p.first_name} {p.last_name}"
        side = "pitching" if p.is_pitcher else "hitting"
        aliases = _aliases(conn, p.id)
        training, metric_keys = _training(conn, p.id, side)
        # A two-way player has swings as well as pitches.
        other_training, other_keys = ([], [])
        if p.is_pitcher:
            other_training, other_keys = _training(conn, p.id, "hitting")
        tiles = _status_tiles(conn, p.id, side, training)
        if p.is_pitcher and other_training:
            tiles += _status_tiles(conn, p.id, "hitting", other_training)
        changes = _changes(conn, p.id)

    all_training = training + other_training
    all_training.sort(key=lambda s: s["date"], reverse=True)
    with engine.connect() as conn:
        goals, interventions = _development(conn, p.id, all_training)

    # Pre/post for each intervention, read-only -- the page must not rewrite
    # outcomes just because someone opened it. changes.py owns those writes.
    import changes as change_engine
    for iv in interventions:
        try:
            res = change_engine.evaluate_intervention(engine, iv["id"], write=False)
            iv["evaluation"] = res["metrics"][:4] if res and res["metrics"] else []
            iv["computed_outcome"] = res["outcome"] if res else None
        except Exception:
            iv["evaluation"] = []
            iv["computed_outcome"] = None

    awre_name = aliases.get("awre", name)
    game = _game_performance(awre_name, bool(p.is_pitcher))
    official = _team_stats_row(name, bool(p.is_pitcher))

    all_sessions = all_training

    return {
        "player": {
            "id": p.id, "slug": p.slug, "name": name,
            "class_year": p.class_year, "pos": p.primary_pos,
            "bats": p.bats, "throws": p.throws,
            "is_pitcher": bool(p.is_pitcher), "side": side,
        },
        "aliases": aliases,
        "status": tiles,
        "changes": changes,
        "training": all_sessions,
        "metric_keys": sorted(set(metric_keys) | set(other_keys)),
        "game": game,
        "official": official,
        "goals": goals,
        "interventions": interventions,
        # Spec section 3: video linking is blocked on an AWRE clip-addressing
        # scheme. Until then the profile links out rather than pretending.
        "video_search_url": "https://web-production-12b79.up.railway.app/",
        # Read from cache only. Generating here would put an API call on every
        # page view, which is exactly what roadmap section 9 is about avoiding.
        "ai_summary": _cached_summary(engine, p.id),
        "last_session": all_sessions[0]["date"] if all_sessions else None,
    }


def metric_series(engine, player_id, metric_key):
    """Per-session series for one metric -- what a sparkline needs."""
    m = metrics.get(metric_key)
    side = "hitting" if (m and m.side == "hitting") else "pitching"
    table = db.swings if side == "hitting" else db.pitch_metrics
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.sessions.c.session_date,
                   func.count().label("n"),
                   func.avg(table.c.value).label("mean"))
            .select_from(db.sessions.join(table, table.c.session_id == db.sessions.c.id))
            .where((db.sessions.c.player_id == player_id) &
                   (table.c.metric_key == metric_key))
            .group_by(db.sessions.c.id, db.sessions.c.session_date)
            .order_by(db.sessions.c.session_date)).all()
    return [{"date": str(r.session_date), "n": r.n,
             "mean": round(float(r.mean), m.decimals if m else 2)} for r in rows]


# ---------------------------------------------------------------------------
# Team development
# ---------------------------------------------------------------------------

def team_overview(engine):
    """The roster-wide view: what changed, who needs review, protocol gaps."""
    with engine.connect() as conn:
        recent_changes = conn.execute(
            select(db.change_events, db.players.c.slug, db.players.c.first_name,
                   db.players.c.last_name)
            .select_from(db.change_events.join(
                db.players, db.change_events.c.player_id == db.players.c.id))
            .where(db.change_events.c.acknowledged == False)  # noqa: E712
            .order_by(db.change_events.c.detected_on.desc()).limit(40)).all()

        counts = {
            "players": conn.execute(select(func.count()).select_from(db.players)
                                    .where(db.players.c.is_active == True)).scalar(),  # noqa: E712
            "sessions": conn.execute(select(func.count()).select_from(db.sessions)).scalar(),
            "measurements": (
                conn.execute(select(func.count()).select_from(db.swings)).scalar() +
                conn.execute(select(func.count()).select_from(db.pitch_metrics)).scalar()),
            "open_reviews": conn.execute(
                select(func.count()).select_from(db.name_review)
                .where(db.name_review.c.status == "open")).scalar(),
            "active_goals": conn.execute(
                select(func.count()).select_from(db.goals)
                .where(db.goals.c.status == "active")).scalar(),
        }

        # Protocol compliance (spec section 10): who has no baseline session,
        # and how long since anyone's last session.
        with_baseline = {r[0] for r in conn.execute(
            select(distinct(db.sessions.c.player_id))
            .where(db.sessions.c.purpose == "baseline"))}
        last_seen = {r.player_id: r.last for r in conn.execute(
            select(db.sessions.c.player_id,
                   func.max(db.sessions.c.session_date).label("last"))
            .group_by(db.sessions.c.player_id))}

    people = roster(engine)
    no_baseline = [p for p in people if p["id"] not in with_baseline]
    no_data = [p for p in people if p["id"] not in last_seen]

    return {
        "counts": counts,
        "changes": [{
            "id": r.id,
            "player": f"{r.first_name} {r.last_name}", "slug": r.slug,
            "metric_key": r.metric_key,
            "label": (metrics.get(r.metric_key).label
                      if metrics.get(r.metric_key) else r.metric_key),
            "detected_on": str(r.detected_on), "severity": r.severity,
            "favorable": r.favorable, "summary": r.summary,
        } for r in recent_changes],
        "no_baseline": no_baseline,
        "no_data": no_data,
        "roster": people,
    }
