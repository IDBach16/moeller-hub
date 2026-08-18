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
STATS_BASE = "https://moeller-2026-stats-production.up.railway.app"

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


def tool_build_report(report, player=None, year=None, compare_to=None,
                      session_type=None, since=None, role=None):
    """Pre-built report templates -- one call gathers every source we have."""
    import reports
    return reports.build_report(report, player=player, year=year,
                                compare_to=compare_to, session_type=session_type,
                                since=since, role=role)


TOOL_IMPLS = {
    "build_report": tool_build_report,
    "list_players": tool_list_players,
    "charting_report": tool_charting_report,
    "season_pitching": tool_season_pitching,
    "season_batting": tool_season_batting,
    "hittrax": tool_hittrax,
    "team_stats": tool_team_stats,
}

TOOLS = [
    {
        "name": "build_report",
        "description": (
            "Build a full pre-built report. USE THIS FIRST whenever a coach asks for a "
            "report, profile, breakdown, write-up, scouting report, development report, "
            "or 'everything we have' on a player or the team — one call gathers every "
            "source (official stats, tracked-game pitch data, off-season charting, "
            "HitTrax) and returns a filled-in bundle to write up. Types:\n"
            "- 'hitter': one hitter's full offensive profile — official line, charted "
            "slash + plate discipline, splits vs LHP/RHP, by pitch group, by count, "
            "year-over-year, HitTrax cage data.\n"
            "- 'pitcher': one pitcher's full profile — official line, arsenal with velo "
            "and whiff by pitch, command/zone mix, first-pitch strike%, two-strike "
            "put-away, splits vs RHH/LHH, year-over-year, off-season bullpens.\n"
            "- 'bullpen': off-season charting work only (Charting App sessions).\n"
            "- 'team': season overview — record, batting and pitching leaders, recent "
            "games, team-wide charted numbers.\n"
            "- 'compare': two players side by side on the same metrics (set role to "
            "'hitter' or 'pitcher').\n"
            "For a single narrow number, the smaller tools are still faster — use this "
            "when the coach wants the whole picture."),
        "input_schema": {
            "type": "object",
            "properties": {
                "report": {"type": "string",
                           "enum": ["hitter", "pitcher", "bullpen", "team", "compare"]},
                "player": {"type": "string",
                           "description": "Player name (partial is fine). Required for "
                                          "hitter, pitcher and compare; optional for bullpen."},
                "compare_to": {"type": "string", "description": "Second player, compare reports only"},
                "role": {"type": "string", "enum": ["hitter", "pitcher"],
                         "description": "Which side to compare on, compare reports only"},
                "year": {"type": "integer",
                         "description": "2024, 2025 or 2026; omit for career/all years"},
                "session_type": {"type": "string",
                                 "enum": ["bullpen", "live_ab", "scrimmage", "intrasquad"],
                                 "description": "bullpen reports only"},
                "since": {"type": "string", "description": "YYYY-MM-DD, bullpen reports only"},
            },
            "required": ["report"],
        },
    },
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
]

SYSTEM = """You are the Coach Assistant on the Moeller Baseball Analytics hub, answering questions \
for Archbishop Moeller High School coaches.

You have tools over three real data sources:
- The Charting App: off-season bullpens and live at-bats being charted right now (live database).
- Season pitch data: 19,560 tracked pitches from the 2024, 2025 and 2026 seasons (AWRE/GoRout).
- Team Stats: the official 2026 season record, box-score batting/pitching stats, and game log.
- HitTrax: cage batted-ball data (exit velo, launch angle, distance) — refreshed weekly. This \
may not be loaded yet; if the tool says so, tell the coach HitTrax data hasn't been uploaded yet.

Ground rules:
- Answer from tool results, never from memory. If a tool returns an error or empty data, say what's \
missing rather than guessing. Never invent numbers.
- If a player lookup misses, check list_players and use the closest name.
- The 2026 season is complete; charting data is current off-season work.
- Be concise and concrete: lead with the answer and the key numbers. A couple of short paragraphs \
or a few plain lines is the right size for a normal question. You're talking to coaches — baseball \
shorthand is fine.
- Whiff% means swinging strikes over swings. Attack zones: Heart (middle), Shadow (edges), Chase, Waste.
- Only baseball and Moeller-data questions; politely decline anything else.

FORMATTING
The chat window renders a small markdown subset. You may use: ## Section headers, **bold**, \
"- " bullets, and --- as a divider. Nothing else — no tables, no numbered-list markup, no links, \
no code fences. For a short answer, skip the headers entirely and just talk.

REPORTS
When a coach asks for a report, profile, breakdown or write-up, call build_report once and write it \
up in the matching template below. Fill every section from the bundle; if a section's data is missing, \
say so in one line and move on — never invent it. Round sensibly, put the sample size next to any rate \
built on a small sample, and finish with coaching takeaways, not just numbers.

Hitter report template:
## <Name> — Hitter Report (<scope>)
One-line summary: what kind of hitter he is, from the numbers.
## The Line
Official 2026 book line (AVG/OBP/SLG/OPS, PA, HR, RBI, SB, BB/K) plus the charted slash for context.
## Plate Discipline
Swing%, chase%, zone-swing%, contact%, whiff%, K% and BB% — say what each one means for him.
## What He Handles
Fastball vs breaking vs offspeed, and the count splits (ahead / even / behind / two strikes).
## Splits
vs RHP and vs LHP.
## Cage / HitTrax
Exit velo, hard-hit rate, launch angle — or one line saying it hasn't been uploaded.
## Trend
Year over year, if there's more than one season.
## Takeaways
Two to four bullets: strengths, the one thing to work on, how to use him in a lineup.

Pitcher report template:
## <Name> — Pitcher Report (<scope>)
One-line summary of the arm.
## The Line
Official 2026 book line (ERA, IP, W-L, SV, K, BB, WHIP) plus charted pitch count.
## Arsenal
Each pitch: usage, avg/max velo, strike%, whiff%, chase% — best pitch first, and say which one is the out pitch.
## Command
Strike%, first-pitch strike%, attack-zone mix, two-strike put-away.
## Results Against
Slash against, K% and BB%, outcome mix.
## Splits
vs RHH and vs LHH.
## Off-Season Work
Charted bullpens — or one line saying none are charted yet.
## Trend
Year over year velo and strike%, if there's more than one season.
## Takeaways
Two to four bullets: what plays, what to build, how to deploy him.

Bullpen report: sessions and pitch count, mix and velo, strike%/whiff%, where the ball is going by \
attack zone, then takeaways.

Team report:
## Moeller Baseball — Team Report (<scope>)
Record and a one-line read on the season. Then ## Offense (team line + leaders), ## Pitching \
(team line + leaders), ## Recent Games, ## Takeaways.

Compare report: a short verdict first, then walk the same metrics for both players side by side under \
## headers by category, then ## Verdict — who profiles better at what, and why."""


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
