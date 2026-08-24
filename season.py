"""
season.py -- the weekly games view and player trend series.

Reads the same two sources the assistant already uses, and nothing else:

  * season_pitches.csv        -- the AWRE pitch-by-pitch master (the postgame
                                 pipeline refreshes it; the hub only reads)
  * the Team Stats app's API  -- official 2026 game log: opponent, W/L, score

The hub deliberately does NOT call the AWRE API itself: Post_Game.bat is the
one ingestion path, and this page updates the same night it runs. A tracked
date with no official result renders as a scrimmage; an official game AWRE
missed renders as untracked. Neither is an error.

Weeks run Monday-Sunday. week_view(None) means "the week containing today",
falling back to the most recent week with games when today's is empty (which
is every week of the off-season) -- pass ?week=YYYY-MM-DD to pin any week,
which is also how a mid-season week is tested from the off-season.
"""

from __future__ import annotations

import datetime
import threading
import time

import requests as rq

import agent

SWINGS = ("Strike Swing and Miss", "Strike Foul", "Strike In Play")
ONBASE_FREE = ("BB", "IBB", "HBP")
HITS = ("1B", "2B", "3B", "HR")

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

_df_cache = None
_df_lock = threading.Lock()


def _df():
    """The master CSV with a real date column, deduplicated.

    The dedup guard exists because the master has shipped with a duplicated
    game before (4/15/2026) -- doubling a game's pitches silently doubles
    every weekly number built on it.
    """
    global _df_cache
    with _df_lock:
        if _df_cache is None:
            import pandas as pd
            df = agent._season_df().copy()
            df = df.drop_duplicates()
            dt = pd.to_datetime(df["Date"], format="mixed", errors="coerce")
            df["D"] = dt.dt.date
            _df_cache = df[df["D"].notna()]
        return _df_cache


_games_cache = {"at": 0.0, "rows": None}


def _official_games():
    """The Team Stats app's game log, cached; stale rows beat an error."""
    now = time.time()
    if _games_cache["rows"] is None or now - _games_cache["at"] > 900:
        try:
            r = rq.get(agent.STATS_BASE + "/api/gcl/games", timeout=20)
            r.raise_for_status()
            _games_cache.update(at=now, rows=r.json())
        except Exception:
            _games_cache["at"] = now
            if _games_cache["rows"] is None:
                _games_cache["rows"] = []
    return _games_cache["rows"]


# ---------------------------------------------------------------------------
# Weeks
# ---------------------------------------------------------------------------

def _monday(d: datetime.date) -> datetime.date:
    return d - datetime.timedelta(days=d.weekday())


def _parse_date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def game_weeks():
    """Every Monday that starts a week containing a game, newest first."""
    dates = {d for d in _df()["D"].unique()}
    for g in _official_games():
        d = _parse_date(g.get("date"))
        if d:
            dates.add(d)
    return sorted({_monday(d) for d in dates}, reverse=True)


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


# ---------------------------------------------------------------------------
# Per-game tracked lines
# ---------------------------------------------------------------------------

def _pitching_line(g):
    """Moeller arms in one game: team line + per-pitcher rows."""
    swings = g["PitchResult"].isin(SWINGS)
    whiffs = g["PitchResult"] == "Strike Swing and Miss"
    strikes = g["PitchResult"] != "Ball"
    velo = g["PitchVelo"].dropna()
    pa = g[g["AtBatResult"].notna() & (g["AtBatResult"] != "")]
    arms = []
    for name, rows in g.groupby("Pitcher"):
        v = rows["PitchVelo"].dropna()
        r_sw = rows["PitchResult"].isin(SWINGS)
        arms.append({
            "name": name, "n": len(rows),
            "velo": round(v.mean(), 1) if len(v) else None,
            "strike_pct": _pct(int((rows["PitchResult"] != "Ball").sum()), len(rows)),
            "whiff_pct": _pct(int((rows["PitchResult"] == "Strike Swing and Miss").sum()),
                              int(r_sw.sum())),
            "k": int((rows["AtBatResult"] == "Strike Out").sum()),
            "bb": int(rows["AtBatResult"].isin(ONBASE_FREE).sum()),
        })
    arms.sort(key=lambda a: -a["n"])
    return {
        "pitches": len(g),
        "strike_pct": _pct(int(strikes.sum()), len(g)),
        "whiff_pct": _pct(int(whiffs.sum()), int(swings.sum())),
        "velo": round(velo.mean(), 1) if len(velo) else None,
        "velo_max": round(velo.max(), 1) if len(velo) else None,
        "k": int((pa["AtBatResult"] == "Strike Out").sum()),
        "bb": int(pa["AtBatResult"].isin(ONBASE_FREE).sum()),
        "arms": arms,
    }


def _batting_line(g):
    """Moeller bats in one game: swing decisions + PA outcomes."""
    swings = g["PitchResult"].isin(SWINGS)
    whiffs = g["PitchResult"] == "Strike Swing and Miss"
    out_zone = g["AttackZone"].isin(("Chase", "Waste"))
    chase_sw = int((swings & out_zone).sum())
    pa = g[g["AtBatResult"].notna() & (g["AtBatResult"] != "")]
    res = pa["AtBatResult"]
    hitters = []
    for name, rows in pa.groupby("Batter"):
        r = rows["AtBatResult"]
        h = int(r.isin(HITS).sum())
        if len(rows):
            hitters.append({"name": name, "pa": len(rows), "h": h,
                            "xbh": int(r.isin(("2B", "3B", "HR")).sum()),
                            "bb": int(r.isin(ONBASE_FREE).sum()),
                            "k": int((r == "Strike Out").sum())})
    hitters.sort(key=lambda x: (-x["h"], -x["bb"]))
    return {
        "pitches_seen": len(g),
        "whiff_pct": _pct(int(whiffs.sum()), int(swings.sum())),
        "chase_pct": _pct(chase_sw, int(out_zone.sum())),
        "h": int(res.isin(HITS).sum()),
        "xbh": int(res.isin(("2B", "3B", "HR")).sum()),
        "bb": int(res.isin(ONBASE_FREE).sum()),
        "k": int((res == "Strike Out").sum()),
        "hitters": hitters[:6],
    }


# ---------------------------------------------------------------------------
# The week view
# ---------------------------------------------------------------------------

def week_view(week=None, today=None):
    """Everything the /season page renders for one week."""
    today = today or datetime.date.today()
    wanted = _parse_date(week) if week else None
    weeks = game_weeks()
    pinned = wanted is not None

    if wanted:
        start = _monday(wanted)
    else:
        start = _monday(today)
    fell_back = False
    if start not in weeks and weeks:
        # Off-season (or a bye week): land on the nearest earlier game week.
        past = [w for w in weeks if w <= start]
        start = past[0] if past else weeks[-1]
        fell_back = not pinned
    end = start + datetime.timedelta(days=6)

    df = _df()
    wdf = df[(df["D"] >= start) & (df["D"] <= end)]
    tracked_dates = set(wdf["D"].unique())

    official = {}
    for g in _official_games():
        d = _parse_date(g.get("date"))
        if d and start <= d <= end:
            official.setdefault(d, []).append(g)

    games, wl = [], {"W": 0, "L": 0, "T": 0, "rf": 0, "ra": 0}
    for d in sorted(tracked_dates | set(official)):
        day = wdf[wdf["D"] == d]
        moe_p = day[day["PitcherTeam"] == "Moeller"]
        moe_b = day[day["BatterTeam"] == "Moeller"]
        opponents = [t for t in day["BatterTeam"].dropna().unique() if t != "Moeller"] + \
                    [t for t in day["PitcherTeam"].dropna().unique() if t != "Moeller"]
        for g in official.get(d) or [None]:
            # Official games deep-link into the Team Stats app's own box score
            # (the accordion at /coach#gcl-game-<id>); a tracked scrimmage has
            # no page there, so it keeps the hub's tracked-data box.
            gid = (g or {}).get("gcl_game_id")
            if gid and g.get("result"):
                box_url = f"{agent.STATS_BASE}/coach#gcl-game-{gid}"
            elif len(day):
                box_url = f"/season/game/{d}"
            else:
                box_url = None
            entry = {
                "date": str(d), "weekday": d.strftime("%a"),
                "opponent": (g or {}).get("opponent") or (opponents[0] if opponents else "Unknown"),
                "result": (g or {}).get("result"),
                "score": (g or {}).get("score"),
                "home_away": (g or {}).get("home_away"),
                "official": g is not None,
                "tracked": len(day) > 0,
                "box_url": box_url,
                "external_box": bool(gid and g and g.get("result")),
                "pitches": len(day),
                "pitching": _pitching_line(moe_p) if len(moe_p) else None,
                "batting": _batting_line(moe_b) if len(moe_b) else None,
            }
            games.append(entry)
            if g:
                res = g.get("result")
                if res in wl:
                    wl[res] += 1
                try:
                    us, them = (g["home_r"], g["away_r"]) if g.get("home_team") == "Moeller" \
                        else (g["away_r"], g["home_r"])
                    wl["rf"] += int(us or 0)
                    wl["ra"] += int(them or 0)
                except (KeyError, TypeError, ValueError):
                    pass

    idx = weeks.index(start) if start in weeks else -1
    return {
        "start": str(start), "end": str(end),
        "label": f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}",
        "fell_back": fell_back,
        "prev": str(weeks[idx + 1]) if 0 <= idx < len(weeks) - 1 else None,
        "next": str(weeks[idx - 1]) if idx > 0 else None,
        "weeks": [str(w) for w in weeks],
        "games": games,
        "record": wl,
    }


# ---------------------------------------------------------------------------
# Trends worth noting -- a detector, not an agent. Same philosophy as
# changes.py: the page computes what moved against the player's own prior
# weeks and shows only what clears a threshold. Nothing here is generated.
# ---------------------------------------------------------------------------

# A week must carry a real sample before it can say anything.
MIN_WK_PITCHES = 15    # pitcher: pitches that week
MIN_WK_SEEN = 10       # hitter: pitches seen that week
MIN_WK_SWINGS = 10     # for whiff rates on either side
MIN_PRIOR_WEEKS = 2

# What clears the bar. Sized so a normal week stays quiet.
TH_VELO = 1.5          # mph, fastballs only -- a mix shift can't fake it
TH_STRIKE = 8.0        # percentage points
TH_WHIFF = 10.0
TH_CHASE = 8.0


def _delta(now, prior):
    prior = [p for p in prior if p is not None]
    if now is None or len(prior) < MIN_PRIOR_WEEKS:
        return None
    base = sum(prior) / len(prior)
    return now - base, base


def notable_trends(week_start, limit=10):
    """What moved for the selected week, against each player's prior weeks.

    Historical weeks show what was notable THEN -- the baseline only uses
    weeks before the one on screen, so browsing April in August replays what
    a coach would have seen in April.
    """
    start = _parse_date(week_start)
    if start is None:
        return []
    end = start + datetime.timedelta(days=6)
    df = _df()
    # Same season only: a junior's baseline is his junior year, not his
    # freshman self two seasons back.
    df = df[(df["D"] <= end) & (df["Year"] == start.year)]
    wk = df["D"].map(_monday)

    findings = []

    def add(name, side, metric, now, base, unit, up_is_good, weeks_n):
        d = now - base
        good = (d > 0) == up_is_good
        findings.append({
            "player": name, "side": side, "metric": metric,
            "now": round(now, 1), "base": round(base, 1),
            "delta": round(d, 1), "unit": unit, "good": good,
            "weeks": weeks_n,
            # sort key: how far past its own threshold the move is
            "_score": abs(d) / {"mph": TH_VELO}.get(unit, TH_STRIKE
                       if metric == "strike%" else (TH_CHASE if metric == "chase%" else TH_WHIFF)),
        })

    # ---- pitchers --------------------------------------------------------
    p = df[df["PitcherTeam"] == "Moeller"].copy()
    p["W"] = wk
    fast = p["PitchType"].fillna("").str.contains("Fast", case=False)
    for name, rows in p.groupby("Pitcher"):
        this = rows[rows["W"] == start]
        if len(this) < MIN_WK_PITCHES:
            continue
        prior_weeks = sorted(w for w in rows["W"].unique() if w < start)
        if len(prior_weeks) < MIN_PRIOR_WEEKS:
            continue

        def wk_vals(fn):
            now = fn(this)
            prior = [fn(rows[rows["W"] == w]) for w in prior_weeks]
            return _delta(now, prior)

        r = wk_vals(lambda g: g.loc[fast.reindex(g.index, fill_value=False),
                                    "PitchVelo"].dropna().mean()
                    if fast.reindex(g.index, fill_value=False).sum() >= 5 else None)
        if r and abs(r[0]) >= TH_VELO:
            add(name, "pitching", "FB velo", r[0] + r[1], r[1], "mph", True,
                len(prior_weeks))

        r = wk_vals(lambda g: _pct(int((g["PitchResult"] != "Ball").sum()), len(g)))
        if r and abs(r[0]) >= TH_STRIKE:
            add(name, "pitching", "strike%", r[0] + r[1], r[1], "%", True,
                len(prior_weeks))

        r = wk_vals(lambda g: _pct(
            int((g["PitchResult"] == "Strike Swing and Miss").sum()),
            int(g["PitchResult"].isin(SWINGS).sum()))
            if g["PitchResult"].isin(SWINGS).sum() >= MIN_WK_SWINGS else None)
        if r and abs(r[0]) >= TH_WHIFF:
            add(name, "pitching", "whiff%", r[0] + r[1], r[1], "%", True,
                len(prior_weeks))

    # ---- hitters ---------------------------------------------------------
    b = df[df["BatterTeam"] == "Moeller"].copy()
    b["W"] = wk
    for name, rows in b.groupby("Batter"):
        this = rows[rows["W"] == start]
        if len(this) < MIN_WK_SEEN:
            continue
        prior_weeks = sorted(w for w in rows["W"].unique() if w < start)
        if len(prior_weeks) < MIN_PRIOR_WEEKS:
            continue

        def wk_vals(fn):
            now = fn(this)
            prior = [fn(rows[rows["W"] == w]) for w in prior_weeks]
            return _delta(now, prior)

        r = wk_vals(lambda g: _pct(
            int((g["PitchResult"] == "Strike Swing and Miss").sum()),
            int(g["PitchResult"].isin(SWINGS).sum()))
            if g["PitchResult"].isin(SWINGS).sum() >= MIN_WK_SWINGS else None)
        if r and abs(r[0]) >= TH_WHIFF:
            add(name, "hitting", "whiff%", r[0] + r[1], r[1], "%", False,
                len(prior_weeks))

        r = wk_vals(lambda g: _pct(
            int((g["PitchResult"].isin(SWINGS)
                 & g["AttackZone"].isin(("Chase", "Waste"))).sum()),
            int(g["AttackZone"].isin(("Chase", "Waste")).sum()))
            if g["AttackZone"].isin(("Chase", "Waste")).sum() >= 8 else None)
        if r and abs(r[0]) >= TH_CHASE:
            add(name, "hitting", "chase%", r[0] + r[1], r[1], "%", False,
                len(prior_weeks))

    findings.sort(key=lambda f: -f["_score"])
    for f in findings:
        del f["_score"]
    return findings[:limit]


# ---------------------------------------------------------------------------
# One game's box score -- built from the tracked pitches themselves. The Team
# Stats app's per-game endpoint exists but carries no box rows for GCL games,
# and the tracked data has every pitch anyway, BOTH sides of it.
# ---------------------------------------------------------------------------

def game_box(date_str):
    d = _parse_date(date_str)
    if d is None:
        return None
    df = _df()
    day = df[df["D"] == d]
    if day.empty:
        return None

    moe_p = day[day["PitcherTeam"] == "Moeller"]   # our arms vs their hitters
    moe_b = day[day["BatterTeam"] == "Moeller"]    # our hitters vs their arms

    opponents = [t for t in day["BatterTeam"].dropna().unique() if t != "Moeller"] +                 [t for t in day["PitcherTeam"].dropna().unique() if t != "Moeller"]
    opponent = opponents[0] if opponents else "Unknown"

    official = None
    for g in _official_games():
        if _parse_date(g.get("date")) == d:
            official = g
            break

    def bat_table(g):
        rows = []
        for name, r in g.groupby("Batter"):
            pa = r[r["AtBatResult"].notna() & (r["AtBatResult"] != "")]
            res = pa["AtBatResult"]
            sw = r["PitchResult"].isin(SWINGS)
            rows.append({
                "name": name, "pa": len(pa), "seen": len(r),
                "h": int(res.isin(HITS).sum()),
                "db": int((res == "2B").sum()), "tr": int((res == "3B").sum()),
                "hr": int((res == "HR").sum()),
                "bb": int(res.isin(ONBASE_FREE).sum()),
                "k": int((res == "Strike Out").sum()),
                "whiff_pct": _pct(int((r["PitchResult"] == "Strike Swing and Miss").sum()),
                                  int(sw.sum())),
            })
        rows.sort(key=lambda x: (-x["pa"], x["name"]))
        return rows

    def arm_table(g):
        rows = []
        for name, r in g.groupby("Pitcher"):
            pa = r[r["AtBatResult"].notna() & (r["AtBatResult"] != "")]
            res = pa["AtBatResult"]
            v = r["PitchVelo"].dropna()
            sw = r["PitchResult"].isin(SWINGS)
            rows.append({
                "name": name, "n": len(r),
                "velo": round(v.mean(), 1) if len(v) else None,
                "velo_max": round(v.max(), 1) if len(v) else None,
                "strike_pct": _pct(int((r["PitchResult"] != "Ball").sum()), len(r)),
                "whiff_pct": _pct(int((r["PitchResult"] == "Strike Swing and Miss").sum()),
                                  int(sw.sum())),
                "k": int((res == "Strike Out").sum()),
                "bb": int(res.isin(ONBASE_FREE).sum()),
                "h_allowed": int(res.isin(HITS).sum()),
            })
        rows.sort(key=lambda x: -x["n"])
        return rows

    return {
        "date": str(d), "weekday": d.strftime("%A"), "opponent": opponent,
        "week": str(_monday(d)),
        "result": (official or {}).get("result"),
        "score": (official or {}).get("score"),
        "home_away": (official or {}).get("home_away"),
        "line": official and {
            "innings": official.get("innings"),
            "us_r": official.get("home_r") if official.get("home_team") == "Moeller" else official.get("away_r"),
            "us_h": official.get("home_h") if official.get("home_team") == "Moeller" else official.get("away_h"),
            "us_e": official.get("home_e") if official.get("home_team") == "Moeller" else official.get("away_e"),
            "them_r": official.get("away_r") if official.get("home_team") == "Moeller" else official.get("home_r"),
            "them_h": official.get("away_h") if official.get("home_team") == "Moeller" else official.get("home_h"),
            "them_e": official.get("away_e") if official.get("home_team") == "Moeller" else official.get("home_e"),
        },
        "pitches": len(day),
        "moe_batting": bat_table(moe_b),
        "moe_pitching": arm_table(moe_p),
        "opp_batting": bat_table(moe_p),    # their hitters faced our arms
        "opp_pitching": arm_table(moe_b),   # their arms faced our hitters
    }


# ---------------------------------------------------------------------------
# Program development -- the team-wide views: what a Moeller arm looks like by
# class, who got better year over year, and where the program line is moving.
# All from the game CSV; class years come from the roster (class as of the
# 2026 season, shifted back for earlier seasons). Graduated players aren't on
# the roster, so class views skip them -- the year-over-year table doesn't,
# because it only needs the same name in consecutive seasons.
# ---------------------------------------------------------------------------

CLASS_IDX = {"Freshman": 0, "Sophomore": 1, "Junior": 2, "Senior": 3}
CLASS_NAMES = ["Freshman", "Sophomore", "Junior", "Senior"]
ROSTER_CLASS_SEASON = 2026          # the season the scraped class_year describes

MIN_SEASON_PITCHES = 40             # a pitcher-season below this says nothing
MIN_SEASON_FASTBALLS = 15


def _pitcher_seasons():
    """One row per (pitcher, season) with enough of a sample to mean something."""
    df = _df()
    moe = df[df["PitcherTeam"] == "Moeller"].copy()
    fast = moe["PitchType"].fillna("").str.contains("Fast", case=False)
    out = []
    for (name, year), g in moe.groupby(["Pitcher", "Year"]):
        if len(g) < MIN_SEASON_PITCHES:
            continue
        fb = g[fast.reindex(g.index, fill_value=False)]
        v = fb["PitchVelo"].dropna()
        sw = g["PitchResult"].isin(SWINGS)
        out.append({
            "name": name, "year": int(year), "n": len(g),
            "fb_n": len(v),
            "fb_velo": round(v.mean(), 1) if len(v) >= MIN_SEASON_FASTBALLS else None,
            "fb_max": round(v.max(), 1) if len(v) >= MIN_SEASON_FASTBALLS else None,
            "strike_pct": _pct(int((g["PitchResult"] != "Ball").sum()), len(g)),
            "whiff_pct": _pct(int((g["PitchResult"] == "Strike Swing and Miss").sum()),
                              int(sw.sum())),
        })
    return out


def _roster_classes():
    """name(lower) -> class index as of ROSTER_CLASS_SEASON, from the hub DB."""
    try:
        import db as hubdb
        from sqlalchemy import select
        engine = hubdb.get_engine()
        with engine.connect() as conn:
            rows = conn.execute(select(hubdb.players.c.first_name,
                                       hubdb.players.c.last_name,
                                       hubdb.players.c.class_year)).all()
        return {f"{r.first_name} {r.last_name}".lower(): CLASS_IDX[r.class_year]
                for r in rows if r.class_year in CLASS_IDX}
    except Exception:
        return {}


def _median(vals):
    vals = sorted(vals)
    n = len(vals)
    if not n:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else round((vals[mid - 1] + vals[mid]) / 2, 1)


VELO_LO, VELO_HI = 68.0, 94.0       # the dot-strip scale, program-wide


def program_development():
    seasons = _pitcher_seasons()
    classes = _roster_classes()

    # ---- class benchmarks + dot strips -----------------------------------
    # One dot per PLAYER, not per pitcher-season: his most recent tracked
    # season, in the class he was that season. Ponatoski's junior and senior
    # years otherwise both plot, and a kid appearing in two class rows reads
    # as a bug, not a benchmark (Ian, 2026-08-24).
    latest = {}
    for s in seasons:
        if s["fb_velo"] is None:
            continue
        cur = latest.get(s["name"])
        if cur is None or s["year"] > cur["year"]:
            latest[s["name"]] = s
    by_class = {i: [] for i in range(4)}
    for s in latest.values():
        idx = classes.get(s["name"].lower())
        if idx is None:
            continue
        then = idx - (ROSTER_CLASS_SEASON - s["year"])
        if 0 <= then <= 3:
            by_class[then].append(s)

    benchmarks = []
    for i in range(4):
        rows = by_class[i]
        velos = [r["fb_velo"] for r in rows]
        benchmarks.append({
            "cls": CLASS_NAMES[i], "n": len(rows),
            "fb_velo": _median(velos),
            "fb_lo": min(velos) if velos else None,
            "fb_hi": max(velos) if velos else None,
            "strike_pct": _median([r["strike_pct"] for r in rows
                                   if r["strike_pct"] is not None]),
            "whiff_pct": _median([r["whiff_pct"] for r in rows
                                  if r["whiff_pct"] is not None]),
            # dot strip: each pitcher-season as a percent position on the scale
            "dots": sorted([{
                "x": round(100 * (r["fb_velo"] - VELO_LO) / (VELO_HI - VELO_LO), 1),
                "velo": r["fb_velo"], "name": r["name"], "year": r["year"],
            } for r in rows], key=lambda d: d["x"]),
        })

    # ---- year-over-year deltas (same name, consecutive seasons) ----------
    by_name = {}
    for s in seasons:
        by_name.setdefault(s["name"], {})[s["year"]] = s
    yoy = []
    for name, ys in by_name.items():
        for year in sorted(ys):
            a, b = ys.get(year), ys.get(year + 1)
            if not a or not b or a["fb_velo"] is None or b["fb_velo"] is None:
                continue
            idx = classes.get(name.lower())
            then = None
            if idx is not None:
                t = idx - (ROSTER_CLASS_SEASON - (year + 1))
                if 0 <= t <= 3:
                    then = CLASS_NAMES[t]
            yoy.append({
                "name": name, "pair": f"{year} \u2192 {year + 1}",
                "to_year": year + 1, "cls": then,
                "velo_from": a["fb_velo"], "velo_to": b["fb_velo"],
                "delta": round(b["fb_velo"] - a["fb_velo"], 1),
                "strike_from": a["strike_pct"], "strike_to": b["strike_pct"],
                "strike_delta": round(b["strike_pct"] - a["strike_pct"], 1)
                    if a["strike_pct"] is not None and b["strike_pct"] is not None else None,
            })
    yoy.sort(key=lambda r: (-r["to_year"], -r["delta"]))

    # ---- the program line, season over season ----------------------------
    by_year = {}
    for s in seasons:
        by_year.setdefault(s["year"], []).append(s)
    line = []
    for year in sorted(by_year):
        rows = by_year[year]
        velos = [r["fb_velo"] for r in rows if r["fb_velo"] is not None]
        line.append({
            "year": year, "arms": len(rows),
            "fb_velo": _median(velos),
            "strike_pct": _median([r["strike_pct"] for r in rows
                                   if r["strike_pct"] is not None]),
            "whiff_pct": _median([r["whiff_pct"] for r in rows
                                  if r["whiff_pct"] is not None]),
        })

    return {"benchmarks": benchmarks, "yoy": yoy, "line": line,
            "scale": {"lo": VELO_LO, "hi": VELO_HI}}
