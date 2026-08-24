"""
agent.py -- the hub's Coach Assistant.

Claude (claude-opus-5) with tool use over three data sources:

  1. The Charting App's open API   (off-season bullpens / live ABs, live Postgres)
  2. season_pitches.csv            (AWRE pitch-by-pitch, 2024-2026, bundled in this repo)
  3. The Team Stats app's GCL API  (2026 record, batting, pitching, game log)

The model decides which tools a question needs, reads the results, and answers
with real numbers. One entry point: answer(history, ip).

Needs ANTHROPIC_API_KEY in the environment. Without it the endpoint degrades to
a polite error rather than crashing the hub.
"""

import json
import os
import threading
import time
from collections import deque

import requests as rq

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(APP_DIR, "season_pitches.csv")
HITTRAX_PATH = os.path.join(APP_DIR, "hittrax.csv")

CHARTING_BASE = "https://moeller-charting-production.up.railway.app"
# Overridable so local dev can point the Season page's box-score deep links
# at a locally running copy of the stats app.
STATS_BASE = os.environ.get(
    "STATS_BASE", "https://moeller-2026-stats-production.up.railway.app")

MODEL = "claude-opus-5"


# ---------------------------------------------------------------------------
# Rate limiting -- the password gates are off, and this endpoint spends real
# API credits, so each IP gets a small per-minute and per-day allowance.
# ---------------------------------------------------------------------------

class RateLimited(Exception):
    pass


_rl_lock = threading.Lock()
_rl_minute = {}   # ip -> deque[timestamps]
_rl_day = {}      # ip -> [yyyymmdd, count]

PER_MINUTE = 8
PER_DAY = 80


def _check_rate(ip):
    now = time.time()
    day = time.strftime("%Y%m%d")
    with _rl_lock:
        q = _rl_minute.setdefault(ip, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= PER_MINUTE:
            raise RateLimited("Easy there — a few questions a minute, please.")
        d = _rl_day.get(ip)
        if d is None or d[0] != day:
            d = [day, 0]
            _rl_day[ip] = d
        if d[1] >= PER_DAY:
            raise RateLimited("Daily question limit reached — back tomorrow.")
        q.append(now)
        d[1] += 1


# ---------------------------------------------------------------------------
# Season pitch data (pandas over the bundled CSV, loaded once)
# ---------------------------------------------------------------------------

_df = None
_df_lock = threading.Lock()


def _season_df():
    global _df
    with _df_lock:
        if _df is None:
            import pandas as pd
            df = pd.read_csv(CSV_PATH, encoding="cp1252", low_memory=False)
            df.columns = [c.strip() for c in df.columns]
            df = df.loc[:, [c for c in df.columns if c]]  # drop unnamed columns
            # Dates are '2024-03-23 00:00:00' in some seasons and '4/15/2026'
            # in others -- mixed parsing handles both.
            dt = pd.to_datetime(df["Date"], format="mixed", errors="coerce")
            df["Year"] = dt.dt.year
            df["PitchVelo"] = pd.to_numeric(df["PitchVelo"], errors="coerce")
            _df = df
        return _df


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def _match_players(series, name):
    """Case-insensitive substring match; returns the distinct matching names."""
    hits = series.dropna().unique()
    name_l = name.lower().strip()
    return sorted({h for h in hits if name_l in str(h).lower()})


def tool_list_players(source, year=None):
    if source == "charting":
        r = rq.get(f"{CHARTING_BASE}/api/players", timeout=20)
        r.raise_for_status()
        return [{"name": p["name"], "throws": p.get("throws"),
                 "is_pitcher": p.get("is_pitcher")} for p in r.json()]
    df = _season_df()
    if year:
        df = df[df["Year"] == int(year)]
    if source == "season_pitchers":
        return sorted(df.loc[df["PitcherTeam"] == "Moeller", "Pitcher"].dropna().unique())
    if source == "season_batters":
        return sorted(df.loc[df["BatterTeam"] == "Moeller", "Batter"].dropna().unique())
    raise ValueError("source must be charting, season_pitchers, or season_batters")


def tool_charting_report(pitcher=None, session_type=None, since=None):
    params = {}
    if session_type and session_type != "all":
        params["session_type"] = session_type
    if since:
        params["since"] = since
    if pitcher:
        players = rq.get(f"{CHARTING_BASE}/api/players", timeout=20).json()
        matches = [p for p in players if pitcher.lower() in p["name"].lower()]
        if not matches:
            return {"error": f"no charted pitcher matching '{pitcher}'",
                    "available": [p["name"] for p in players if p.get("is_pitcher")]}
        params["pitcher_id"] = matches[0]["id"]
    r = rq.get(f"{CHARTING_BASE}/api/dashboard", params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def tool_season_pitching(pitcher, year=None):
    df = _season_df()
    df = df[df["PitcherTeam"] == "Moeller"]
    names = _match_players(df["Pitcher"], pitcher)
    if not names:
        return {"error": f"no Moeller pitcher matching '{pitcher}'",
                "available": sorted(df["Pitcher"].dropna().unique())}
    d = df[df["Pitcher"].isin(names)]
    if year:
        d = d[d["Year"] == int(year)]
        if d.empty:
            return {"error": f"{names[0]} has no pitches in {year}",
                    "years_with_data": sorted(df[df['Pitcher'].isin(names)]['Year'].dropna().unique().tolist())}

    swings = d["PitchResult"].isin(["Strike Swing and Miss", "Strike Foul", "Strike In Play"])
    whiffs = d["PitchResult"] == "Strike Swing and Miss"
    strikes = d["PitchResult"] != "Ball"

    by_type = []
    for ptype, g in d.groupby("PitchType"):
        g_sw = g["PitchResult"].isin(["Strike Swing and Miss", "Strike Foul", "Strike In Play"])
        by_type.append({
            "pitch_type": ptype,
            "n": len(g),
            "usage_pct": _pct(len(g), len(d)),
            "avg_velo": round(g["PitchVelo"].mean(), 1) if g["PitchVelo"].notna().any() else None,
            "max_velo": g["PitchVelo"].max() if g["PitchVelo"].notna().any() else None,
            "strike_pct": _pct((g["PitchResult"] != "Ball").sum(), len(g)),
            "whiff_pct": _pct((g["PitchResult"] == "Strike Swing and Miss").sum(), int(g_sw.sum())),
        })
    by_type.sort(key=lambda x: -x["n"])

    pa = d[d["AtBatResult"].notna() & (d["AtBatResult"] != "")]
    return {
        "pitcher": names if len(names) > 1 else names[0],
        "years": sorted(d["Year"].dropna().unique().tolist()),
        "pitches": len(d),
        "strike_pct": _pct(int(strikes.sum()), len(d)),
        "whiff_pct": _pct(int(whiffs.sum()), int(swings.sum())),
        "zone_mix_pct": {z: _pct(int((d["AttackZone"] == z).sum()), int(d["AttackZone"].notna().sum()))
                         for z in ("Heart", "Shadow", "Chase", "Waste")},
        "pitch_types": by_type,
        "pa_outcomes": pa["AtBatResult"].value_counts().to_dict(),
    }


def tool_season_batting(batter, year=None):
    df = _season_df()
    df = df[df["BatterTeam"] == "Moeller"]
    names = _match_players(df["Batter"], batter)
    if not names:
        return {"error": f"no Moeller batter matching '{batter}'",
                "available": sorted(df["Batter"].dropna().unique())}
    d = df[df["Batter"].isin(names)]
    if year:
        d = d[d["Year"] == int(year)]
        if d.empty:
            return {"error": f"{names[0]} has no pitches in {year}"}

    def split(g):
        swings = g["PitchResult"].isin(["Strike Swing and Miss", "Strike Foul", "Strike In Play"])
        pa = g[g["AtBatResult"].notna() & (g["AtBatResult"] != "")]
        return {
            "pitches_seen": len(g),
            "whiff_pct": _pct(int((g["PitchResult"] == "Strike Swing and Miss").sum()), int(swings.sum())),
            "pa_outcomes": pa["AtBatResult"].value_counts().to_dict(),
        }

    out = {"batter": names if len(names) > 1 else names[0],
           "years": sorted(d["Year"].dropna().unique().tolist()),
           **split(d),
           "vs_RHP": split(d[d["PitcherHand"] == "R"]),
           "vs_LHP": split(d[d["PitcherHand"] == "L"])}
    return out


# --- HitTrax cage data (skeleton; finalized against a real export) --------
# HitTrax export column names vary; resolve them by trying candidates rather
# than hard-coding. When the first real export lands, confirm/adjust the lists
# below and add per-session trends -- everything else already works.

_HT_CANDIDATES = {
    "batter":     ["batter", "user", "player", "name", "hitter"],
    "date":       ["date", "session date", "timestamp", "created", "datetime"],
    "exit_velo":  ["exit velocity", "exitvelo", "exit velo", "ev", "velo mph"],
    "launch":     ["launch angle", "launchangle", "la", "elevation", "angle"],
    "distance":   ["distance", "dist", "carry", "distance ft"],
    "result":     ["result", "hit type", "type", "outcome", "play result"],
    "pitch_velo": ["pitch velocity", "pitch velo", "pitch speed", "pitch mph"],
}

_ht = None  # cache: None=untried, False=no file, else (df, resolved_cols)


def _hittrax():
    global _ht
    if _ht is None:
        if not os.path.exists(HITTRAX_PATH):
            _ht = False
        else:
            import pandas as pd
            df = None
            for enc in ("utf-8-sig", "utf-8", "cp1252"):
                try:
                    df = pd.read_csv(HITTRAX_PATH, encoding=enc, low_memory=False)
                    break
                except Exception:
                    continue
            if df is None:
                _ht = False
            else:
                df.columns = [str(c).strip() for c in df.columns]
                norm = {c: c.lower().replace("_", " ").strip() for c in df.columns}
                resolved = {}
                for key, cands in _HT_CANDIDATES.items():
                    col = next((o for o, n in norm.items() if n in cands), None)
                    if col is None:
                        col = next((o for o, n in norm.items()
                                    if any(c in n for c in cands)), None)
                    if col:
                        resolved[key] = col
                _ht = (df, resolved)
    return _ht if _ht else None


def tool_hittrax(batter=None):
    ht = _hittrax()
    if ht is None:
        return {"status": "no HitTrax data loaded yet",
                "note": "HitTrax cage data is published to the hub weekly once a coach "
                        "exports it; none has been loaded on the server yet."}
    df, cols = ht
    if "batter" not in cols:
        return {"status": "loaded but not yet mapped",
                "columns": list(df.columns),
                "note": "The batter/player column couldn't be identified automatically; "
                        "this export needs its column mapping finalized."}
    import pandas as pd
    d = df
    if batter:
        names = _match_players(d[cols["batter"]], batter)
        if not names:
            return {"error": f"no HitTrax player matching '{batter}'",
                    "available": sorted(d[cols["batter"]].dropna().astype(str).unique())[:60]}
        d = d[d[cols["batter"]].astype(str).isin(names)]

    def num(key):
        return pd.to_numeric(d[cols[key]], errors="coerce") if key in cols else None

    out = {"player": batter or "all hitters", "swings": len(d)}
    ev, la, dist = num("exit_velo"), num("launch"), num("distance")
    if ev is not None and ev.notna().any():
        out["avg_exit_velo"] = round(float(ev.mean()), 1)
        out["max_exit_velo"] = round(float(ev.max()), 1)
        out["hard_hit_pct_90plus"] = _pct(int((ev >= 90).sum()), int(ev.notna().sum()))
    if la is not None and la.notna().any():
        out["avg_launch_angle"] = round(float(la.mean()), 1)
    if dist is not None and dist.notna().any():
        out["avg_distance"] = round(float(dist.mean()), 1)
        out["max_distance"] = round(float(dist.max()), 1)
    if "date" in cols:
        out["sessions"] = int(d[cols["date"]].astype(str).str[:10].nunique())
    return out


# ---------------------------------------------------------------------------
# Player-development tools  (spec section 9.1)
# ---------------------------------------------------------------------------
#
# These are deliberately THIN. The analysis already happened in profiles.py,
# changes.py and development.py; all these do is resolve a name and shape the
# result down to what the model needs. None of them returns a raw swing or
# pitch row -- that is the rule from roadmap section 9, enforced in code rather
# than left to the model's judgement.

def _engine():
    import db
    return db.get_engine()


def _resolve(name):
    """Name -> player row, via canonical names and every stored alias.

    Returns (row, None) or (None, error_dict). The error carries the roster so
    the model can pick the closest name instead of guessing or giving up.
    """
    import db
    from sqlalchemy import select
    engine = _engine()
    key = str(name or "").strip().lower()
    if not key:
        return None, {"error": "no name given"}
    with engine.connect() as conn:
        people = conn.execute(
            select(db.players.c.id, db.players.c.slug, db.players.c.first_name,
                   db.players.c.last_name, db.players.c.is_pitcher,
                   db.players.c.bats, db.players.c.throws,
                   db.players.c.class_year)
            .where(db.players.c.is_active == True)).all()  # noqa: E712
        aliases = {str(r.alias).lower(): r.player_id for r in conn.execute(
            select(db.player_aliases.c.alias, db.player_aliases.c.player_id))}

    by_id = {r.id: r for r in people}
    for r in people:
        if f"{r.first_name} {r.last_name}".lower() == key:
            return r, None
    if key in aliases and aliases[key] in by_id:
        return by_id[aliases[key]], None
    hits = [r for r in people
            if key in f"{r.first_name} {r.last_name}".lower()]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, {"error": f"'{name}' matches more than one player",
                      "candidates": [f"{r.first_name} {r.last_name}" for r in hits]}
    return None, {"error": f"no Moeller player matching '{name}'",
                  "roster": sorted(f"{r.first_name} {r.last_name}" for r in people)}


def tool_find_player(name):
    row, err = _resolve(name)
    if err:
        return err
    return {"player_id": row.id, "name": f"{row.first_name} {row.last_name}",
            "role": "pitcher" if row.is_pitcher else "position player",
            "class_year": row.class_year, "bats": row.bats, "throws": row.throws}


def tool_player_snapshot(player):
    import profiles
    row, err = _resolve(player)
    if err:
        return err
    prof = profiles.profile(_engine(), row.slug)
    return {
        "player": prof["player"]["name"],
        "role": "pitcher" if prof["player"]["is_pitcher"] else "position player",
        "last_session": prof["last_session"],
        "training_sessions": len(prof["training"]),
        "current_status": [
            {"metric": t["label"], "value": f"{t['value']}{t['unit']}",
             "baseline": t["baseline"], "delta": t["delta"],
             "favorable": t["favorable"], "observations": t["n"],
             "as_of": t["date"]} for t in prof["status"]],
        "unacknowledged_changes": len([c for c in prof["changes"]
                                       if not c["acknowledged"]]),
        "active_goals": len([g for g in prof["goals"] if g["status"] == "active"]),
        "note": ("No device training data yet -- this player's numbers below are "
                 "game data only." if not prof["training"] else None),
    }


def tool_what_changed(player, only_unacknowledged=True):
    import profiles
    row, err = _resolve(player)
    if err:
        return err
    prof = profiles.profile(_engine(), row.slug)
    items = prof["changes"]
    if only_unacknowledged:
        items = [c for c in items if not c["acknowledged"]]
    if not items:
        return {"player": prof["player"]["name"], "changes": [],
                "note": ("Nothing has cleared the detection thresholds for this player. "
                         "That is not a tool failure and does not mean he isn't improving "
                         "— detection needs enough sessions in both a recent and a "
                         "baseline window, and stays quiet rather than reporting noise.")}
    return {"player": prof["player"]["name"],
            "changes": [{"summary": c["summary"], "severity": c["severity"],
                         "favorable": c["favorable"], "detected_on": c["detected_on"],
                         "effect_size": c["effect_size"],
                         "observations": f"{c['n_recent']} recent vs {c['n_baseline']} baseline"}
                        for c in items[:10]]}


def tool_metric_history(player, metric_key):
    """Session-level means. A pitcher with 900 tracked pitches over 12 sessions
    returns 12 rows here, not 900."""
    import metrics as M
    import profiles
    row, err = _resolve(player)
    if err:
        return err
    if not M.known(metric_key):
        return {"error": f"'{metric_key}' is not a tracked metric",
                "available": sorted(M.REGISTRY)}
    m = M.get(metric_key)
    series = profiles.metric_series(_engine(), row.id, metric_key)
    if not series:
        return {"player": f"{row.first_name} {row.last_name}", "metric": m.label,
                "sessions": [], "note": "no sessions with this metric"}
    return {"player": f"{row.first_name} {row.last_name}",
            "metric": m.label, "unit": m.unit,
            "higher_is_better": m.polarity,
            "target_band": list(m.target_band) if m.target_band else None,
            "sessions": series[-20:]}


def tool_compare_windows(player, metric_key, recent_sessions=3, baseline_days=120,
                         pitch_type=None):
    """An arbitrary two-window comparison, computed in SQL and arithmetic --
    not by the model doing statistics in its head."""
    import changes as C
    import metrics as M
    row, err = _resolve(player)
    if err:
        return err
    if not M.known(metric_key):
        return {"error": f"'{metric_key}' is not a tracked metric",
                "available": sorted(M.REGISTRY)}
    engine = _engine()
    with engine.connect() as conn:
        obs = C._observations(conn, row.id)
        baseline_ids = C._baseline_sessions(conn, row.id)
    # Observations are keyed by (metric, pitch type): a fastball's ride and a
    # slider's are different measurements, not two samples of one.
    rows, resolved = C.observations_for(obs, metric_key, pitch_type)
    if not rows:
        available = C.available_pitch_types(obs, metric_key)
        note = f"no {metric_key} data for this player"
        if pitch_type and available:
            note = (f"no {metric_key} data for {pitch_type}; "
                    f"tracked pitches: {', '.join(available)}")
        return {"player": f"{row.first_name} {row.last_name}", "note": note}
    v = C.evaluate_metric(rows, M.get(metric_key), baseline_ids,
                          k=int(recent_sessions), baseline_days=int(baseline_days))
    v.pop("gates", None)
    v["player"] = f"{row.first_name} {row.last_name}"
    if resolved:
        v["pitch_type"] = resolved
        v["scope"] = f"{M.PITCH_TYPE_LABELS.get(resolved, resolved)} only"
        if pitch_type is None:
            v["note"] = (f"{metric_key} is pitch-specific; showing his most-thrown "
                         f"pitch ({resolved}). Pass pitch_type for another.")
    return v


def tool_goals_and_interventions(player):
    import profiles
    row, err = _resolve(player)
    if err:
        return err
    prof = profiles.profile(_engine(), row.slug)
    return {
        "player": prof["player"]["name"],
        "goals": [{"title": g["title"], "status": g["status"],
                   "metric": g["metric_label"], "target": g["target_value"],
                   "progress": g["progress"].get("state"),
                   "current": g["progress"].get("current"),
                   "pct_of_the_way": g["progress"].get("pct"),
                   "set_by": g["set_by"], "review_on": g["review_on"]}
                  for g in prof["goals"]][:10],
        "interventions": [{"title": i["title"], "date": i["date"],
                           "category": i["category"], "coach": i["coach"],
                           "outcome": i["outcome"],
                           "before_after": [
                               f"{m['label']} {m['pre_mean']}->{m['post_mean']}{m['unit']} "
                               f"({'moved' if m['moved'] else 'within noise'})"
                               for m in (i.get("evaluation") or [])[:3]]}
                          for i in prof["interventions"]][:10],
        "note": ("An intervention's before/after is a comparison, not proof of a cause. "
                 "Note the timing if it's relevant; don't claim it worked."),
    }


def tool_roster_alerts():
    import profiles
    ov = profiles.team_overview(_engine())
    return {
        "counts": ov["counts"],
        "changes_needing_review": [
            {"player": c["player"], "summary": c["summary"],
             "severity": c["severity"], "favorable": c["favorable"],
             "detected_on": c["detected_on"]} for c in ov["changes"][:20]],
        "note": ("Empty means nothing has cleared the detection thresholds -- not "
                 "that nothing is happening." if not ov["changes"] else None),
    }


def tool_protocol_status():
    import development
    import profiles
    engine = _engine()
    ov = profiles.team_overview(engine)
    due = development.due_for_review(engine)
    return {
        "players_with_no_training_data": [p["name"] for p in ov["no_data"]][:40],
        "players_without_a_baseline_session": [p["name"] for p in ov["no_baseline"]][:40],
        "names_awaiting_review": ov["counts"]["open_reviews"],
        "goals_due_for_review": [f"{g['player']}: {g['title']} (due {g['review_on']})"
                                 for g in due["goals"]][:20],
        "interventions_due_for_review": [
            f"{i['player']}: {i['title']} (due {i['review_on']})"
            for i in due["interventions"]][:20],
    }


def tool_player_summary(player):
    """The cached development note, if one is current. Never generates one --
    that would put an API call inside an API call."""
    import summaries
    row, err = _resolve(player)
    if err:
        return err
    hit = summaries.cached(_engine(), row.id)
    if not hit:
        return {"player": f"{row.first_name} {row.last_name}", "summary": None,
                "note": "no current cached summary; the weekly job writes these"}
    return {"player": f"{row.first_name} {row.last_name}",
            "summary": hit["summary"], "written": hit["created_at"]}


def tool_team_stats(kind):
    paths = {"batting": "/api/gcl/batting", "pitching": "/api/gcl/pitching",
             "games": "/api/gcl/games", "record": "/api/config"}
    if kind not in paths:
        raise ValueError("kind must be batting, pitching, games, or record")
    r = rq.get(STATS_BASE + paths[kind], timeout=25)
    r.raise_for_status()
    data = r.json()
    # slim the row payloads a little
    if isinstance(data, dict) and "rows" in data:
        for row in data["rows"]:
            row.pop("id", None)
    return data


def tool_app_links(player=None):
    """Deeplinks: hub pages, every deployed application, and player-specific
    views. The model cites these as [[Label|URL]] and the chat renders them
    clickable -- it must never construct a URL itself, only relay these."""
    import tools as registry
    out = {
        "how_to_cite": "Write a link as [[Label|URL]] -- the chat renders it "
                       "as a clickable link. Only use URLs from this tool.",
        "hub_pages": {
            "players roster": "/players",
            "team development (roster-wide changes, protocol gaps)": "/team",
            "game prep": "/prep",
            "video": "/video",
            "applications directory": "/tools",
        },
        "applications": [
            {"name": t["title"], "what": t["desc"], "url": t["url"]}
            for t in registry.TOOLS
        ],
    }
    if player:
        row, err = _resolve(player)
        if err:
            return err
        links = {
            "hub profile (training history, changes, goals, AI note)":
                f"/players/{row.slug}",
        }
        if row.is_pitcher:
            # Same slug in both apps -- they read the same database.
            links["Moeller Rapsodo dashboard (location, movement, velocity, "
                  "percentiles, Stuff+, session by date)"] = \
                f"https://rapsodo-app-production.up.railway.app/?p={row.slug}"
        out["player"] = {"name": f"{row.first_name} {row.last_name}",
                         "links": links}
    return out


TOOL_IMPLS = {
    # game-performance layer (unchanged -- these already worked)
    "list_players": tool_list_players,
    "charting_report": tool_charting_report,
    "season_pitching": tool_season_pitching,
    "season_batting": tool_season_batting,
    "hittrax": tool_hittrax,
    "team_stats": tool_team_stats,
    # player-development layer (spec section 9.1)
    "find_player": tool_find_player,
    "player_snapshot": tool_player_snapshot,
    "what_changed": tool_what_changed,
    "metric_history": tool_metric_history,
    "compare_windows": tool_compare_windows,
    "goals_and_interventions": tool_goals_and_interventions,
    "roster_alerts": tool_roster_alerts,
    "protocol_status": tool_protocol_status,
    "player_summary": tool_player_summary,
    # navigation layer -- the assistant is also the hub's front door
    "app_links": tool_app_links,
}

TOOLS = [
    {
        "name": "list_players",
        "description": ("List player names in a data source. Call this when you are unsure of a "
                        "player's exact name, when a lookup by name found nothing, or when the coach "
                        "asks who is in the data. Sources: 'charting' (off-season charting app roster), "
                        "'season_pitchers' / 'season_batters' (2024-2026 game pitch data)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["charting", "season_pitchers", "season_batters"]},
                "year": {"type": "integer", "description": "Optional filter for season sources: 2024, 2025 or 2026"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "charting_report",
        "description": ("Off-season charting data (bullpens, live at-bats) from the Charting App's live "
                        "database. Call this for anything about bullpens, off-season work, or recent "
                        "charted sessions. Returns per-pitcher strike%, whiff%, zone rates, velo, pitch "
                        "mix, and a pitch-location histogram by attack zone. All filters optional."),
        "input_schema": {
            "type": "object",
            "properties": {
                "pitcher": {"type": "string", "description": "Filter to one pitcher by (partial) name"},
                "session_type": {"type": "string", "enum": ["bullpen", "live_ab", "scrimmage", "intrasquad"]},
                "since": {"type": "string", "description": "Only sessions on/after this date, YYYY-MM-DD"},
            },
        },
    },
    {
        "name": "season_pitching",
        "description": ("A Moeller pitcher's game pitch data from the 2024-2026 seasons (19,560 tracked "
                        "pitches). Call this for questions about how a pitcher performed in games: pitch "
                        "mix, velocity, strike%, whiff%, attack-zone mix, and plate-appearance outcomes "
                        "against him. Partial names are matched."),
        "input_schema": {
            "type": "object",
            "properties": {
                "pitcher": {"type": "string"},
                "year": {"type": "integer", "description": "Optional: 2024, 2025 or 2026; omit for career"},
            },
            "required": ["pitcher"],
        },
    },
    {
        "name": "season_batting",
        "description": ("A Moeller batter's game pitch data from the 2024-2026 seasons: pitches seen, "
                        "whiff%, plate-appearance outcomes, and splits vs LHP/RHP. Call this for "
                        "questions about how a hitter fared in games. Partial names are matched."),
        "input_schema": {
            "type": "object",
            "properties": {
                "batter": {"type": "string"},
                "year": {"type": "integer"},
            },
            "required": ["batter"],
        },
    },
    {
        "name": "hittrax",
        "description": ("HitTrax cage/batted-ball data for Moeller hitters: exit velocity "
                        "(avg/max), hard-hit rate, launch angle, distance, and session counts. "
                        "Call this for questions about a hitter's exit velo, how hard he's "
                        "hitting the ball, or his cage/HitTrax numbers. Partial names are matched. "
                        "This data is refreshed weekly and may not be loaded yet — if the tool "
                        "reports no data, tell the coach HitTrax data hasn't been uploaded yet."),
        "input_schema": {
            "type": "object",
            "properties": {
                "batter": {"type": "string", "description": "Filter to one hitter by (partial) name; omit for all"},
            },
        },
    },
    {
        "name": "team_stats",
        "description": ("Official 2026 team statistics from the Team Stats app. Call this for the team's "
                        "record, box-score batting stats (AVG/OBP/HR/RBI...), pitching stats (ERA/IP/K...), "
                        "or the 2026 game-by-game log with scores."),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["batting", "pitching", "games", "record"]},
            },
            "required": ["kind"],
        },
    },

    # --- player development (spec 9.1) -----------------------------------
    {
        "name": "find_player",
        "description": ("Resolve a player's name to his Moeller Player ID and basics "
                        "(role, class, bats/throws). Call this first when a question is "
                        "about one player and you're unsure of the exact name -- it matches "
                        "nicknames and alternate spellings, and returns the roster on a miss."),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "player_snapshot",
        "description": ("Where a player stands right now: his headline training metrics with "
                        "each one's baseline and delta, when he was last measured, how many "
                        "sessions he has, and counts of open changes and goals. Call this for "
                        "'how is X doing' or 'what do I need to know about X'."),
        "input_schema": {
            "type": "object",
            "properties": {"player": {"type": "string"}},
            "required": ["player"],
        },
    },
    {
        "name": "what_changed",
        "description": ("Meaningful changes detected for a player against HIS OWN prior "
                        "baseline -- not against the team. Each one is already checked "
                        "against measurement noise, the player's own variability, and a "
                        "significance test, so anything returned here is worth a coach's "
                        "attention. Call this for 'what changed with X' or 'is X trending "
                        "up'. An empty list means nothing cleared the thresholds."),
        "input_schema": {
            "type": "object",
            "properties": {
                "player": {"type": "string"},
                "only_unacknowledged": {
                    "type": "boolean",
                    "description": "Default true. Set false to include changes a coach has already cleared."},
            },
            "required": ["player"],
        },
    },
    {
        "name": "metric_history",
        "description": ("One metric for one player over time, as SESSION AVERAGES -- one row "
                        "per session, never individual pitches or swings. Call this when a "
                        "coach wants the trend rather than a single comparison. Metric keys "
                        "are things like fb_velocity, spin_rate, bat_speed, exit_velocity, "
                        "attack_angle; a wrong key returns the full list."),
        "input_schema": {
            "type": "object",
            "properties": {
                "player": {"type": "string"},
                "metric_key": {"type": "string"},
            },
            "required": ["player", "metric_key"],
        },
    },
    {
        "name": "compare_windows",
        "description": ("Compare a player's recent sessions against his earlier baseline for "
                        "one metric, with the window sizes you choose. The comparison is "
                        "computed in the database -- means, standard deviations, effect size "
                        "and a significance test all come back done. Use this when the coach "
                        "asks about a different span than the default (e.g. 'last five "
                        "bullpens' or 'since the spring')."),
        "input_schema": {
            "type": "object",
            "properties": {
                "player": {"type": "string"},
                "metric_key": {"type": "string"},
                "recent_sessions": {"type": "integer", "description": "Sessions in the recent window; default 3"},
                "baseline_days": {"type": "integer", "description": "Days of history for the baseline; default 120"},
            },
            "required": ["player", "metric_key"],
        },
    },
    {
        "name": "goals_and_interventions",
        "description": ("A player's development goals with progress toward each target, and "
                        "the interventions logged for him (grip changes, drills, swing "
                        "changes, cues, strength blocks) with a before/after on each. Call "
                        "this for 'what are we working on with X' or 'did the grip change "
                        "work'. Report before/after as a comparison; never claim the "
                        "intervention caused the change."),
        "input_schema": {
            "type": "object",
            "properties": {"player": {"type": "string"}},
            "required": ["player"],
        },
    },
    {
        "name": "roster_alerts",
        "description": ("Changes across the whole roster that a coach hasn't cleared yet, most "
                        "recent first, plus overall counts. Call this for 'who needs "
                        "attention', 'anything I should know about this week', or any "
                        "team-wide development question."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "protocol_status",
        "description": ("Where data collection has gaps: players with no training data, "
                        "players missing a baseline session, export names still awaiting "
                        "review, and goals or interventions past their review date. Call this "
                        "for 'who are we missing data on' or 'are we keeping up with the "
                        "protocols'."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "player_summary",
        "description": ("The stored written development note for a player, if one is current. "
                        "Useful as background before answering a broader question. It may be "
                        "absent -- that is not an error, just means no note has been written "
                        "since his data last changed."),
        "input_schema": {
            "type": "object",
            "properties": {"player": {"type": "string"}},
            "required": ["player"],
        },
    },
    {
        "name": "app_links",
        "description": "Deeplinks for navigation: the hub's pages, every deployed "
                       "Moeller application (Rapsodo dashboard, Pitcher/Hitter Cards, "
                       "Scouting Agent, Video Search, Team Stats, Charting), and "
                       "player-specific views when a player name is given (his hub "
                       "profile; his Rapsodo dashboard if he pitches). Call it whenever "
                       "the coach asks where to find or open something, or when your "
                       "answer should end with a link to a richer view.",
        "input_schema": {
            "type": "object",
            "properties": {"player": {"type": "string",
                                      "description": "optional player name for "
                                                     "player-specific links"}},
        },
    },
]

SYSTEM = """You are the assistant on the Moeller Baseball Analytics hub -- the front door \
to the whole system for Archbishop Moeller High School coaches.

Your job has two halves. DEVELOPMENT: what changed for a player, what the evidence is, and \
what a coach might want to look at next. NAVIGATION: the hub sits on top of a family of \
apps (the Rapsodo dashboard, Pitcher and Hitter Cards, Scouting Agent, Video Search, Team \
Stats, Charting), and you know where everything lives -- when an answer has a richer view \
somewhere, hand the coach the link to it.

TWO LAYERS OF DATA

Player development — training sessions, detected changes, goals and interventions:
- find_player, player_snapshot, what_changed, metric_history, compare_windows,
  goals_and_interventions, roster_alerts, protocol_status, player_summary.
- These read a connected history of Blast / HitTrax / Rapsodo sessions per player.
- This data is NEW and may be thin or empty. That is expected, not an error.

Game performance — how it actually played out in competition:
- season_pitching / season_batting (19,560 tracked AWRE pitches, 2024-2026),
  team_stats (official 2026 record and box score), charting_report (off-season
  bullpens and live ABs), hittrax (cage batted-ball data), list_players.

HOW TO WORK

- For a question about one player, start with find_player, then player_snapshot and/or
  what_changed. Add game data when the question is about competition results.
- Prefer what_changed and compare_windows over eyeballing metric_history yourself. The
  comparison is already computed — means, effect size and significance come back done.
  Do not do statistics in your head or recompute what a tool already gave you.
- For team-wide questions use roster_alerts; for collection gaps use protocol_status.

NAVIGATION AND DEEPLINKS

- app_links returns every destination: the hub's own pages, every deployed application \
with what it is for, and player-specific views (his hub profile; his Rapsodo dashboard \
if he pitches) when you pass a player name.
- Cite a link as [[Label|URL]] -- the chat renders that as a clickable link. This is the \
ONE exception to the plain-text rule. Use URLs exactly as a tool returned them; never \
construct, guess, or modify one.
- When a coach asks where something is, the link IS the answer. When a data answer has an \
obvious deeper view -- a pitcher question has his Rapsodo dashboard, a development \
question has his hub profile -- end with that one link. One or two links, never a menu \
of everything.

WHAT NOT TO DO

- Never invent a number. Answer only from tool results. If a tool returns empty or an
  error, say what is missing.
- An empty change list means nothing cleared the detection thresholds — it does NOT mean
  the player is not improving, and it is not a tool failure. Say so plainly.
- A change is a comparison against the player's own baseline, never proof of a cause. If
  an intervention was logged near a change you may note the timing; do not say it caused it.
- "Up" is not the same as "good". The tools tell you whether a move is favorable — some
  metrics are lower-is-better and some have a target band. Trust the favorable flag over
  the sign of the number.
- Coaches own the interpretation and the development decision. Offer evidence and things
  worth checking, not verdicts on a player.

STYLE

- Lead with the answer and the key numbers. A couple of short paragraphs or a few plain
  lines is right. You're talking to coaches — baseball shorthand is fine.
- Plain text ONLY. The chat window renders your reply literally, so never use markdown:
  no **bold**, no ## headers, no tables, no bullet asterisks. Dashes and line breaks are
  fine. The single exception is the [[Label|URL]] link form described above.
- Whiff% means swinging strikes over swings. Attack zones: Heart (middle), Shadow (edges),
  Chase, Waste. The 2026 season is complete; charting data is current off-season work.
- Only baseball and Moeller-data questions; politely decline anything else."""


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------

_client = None


def _anthropic():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def answer(history, ip):
    """history: list of {'role': 'user'|'assistant', 'text': str}. Returns reply text."""
    _check_rate(ip)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "The assistant isn't configured yet (no API key on the server)."

    messages = []
    for m in history[-16:]:
        role = m.get("role")
        text = str(m.get("text", ""))[:4000]
        if role in ("user", "assistant") and text.strip():
            messages.append({"role": role, "content": text})
    if not messages or messages[-1]["role"] != "user":
        return "Ask me something about Moeller baseball data."

    client = _anthropic()
    for _ in range(6):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            output_config={"effort": "medium"},  # snappy enough for a chat widget
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "refusal":
            return "I can't help with that one."

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                try:
                    out = TOOL_IMPLS[block.name](**block.input)
                    content = json.dumps(out, default=str)[:30000]
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": content})
                except Exception as e:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"Tool failed: {e}", "is_error": True})
            messages.append({"role": "user", "content": results})
            continue

        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text or "I came up empty on that one — try rephrasing?"

    return "That took more digging than I can do in one go — try a narrower question."
