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


TOOL_IMPLS = {
    "list_players": tool_list_players,
    "charting_report": tool_charting_report,
    "season_pitching": tool_season_pitching,
    "season_batting": tool_season_batting,
    "team_stats": tool_team_stats,
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

Ground rules:
- Answer from tool results, never from memory. If a tool returns an error or empty data, say what's \
missing rather than guessing. Never invent numbers.
- If a player lookup misses, check list_players and use the closest name.
- The 2026 season is complete; charting data is current off-season work.
- Be concise and concrete: lead with the answer and the key numbers. A couple of short paragraphs \
or a few plain lines is the right size. You're talking to coaches — baseball shorthand is fine.
- Plain text ONLY. The chat window renders your reply literally, so never use markdown: no **bold**, \
no ## headers, no tables, no bullet asterisks. Dashes and line breaks are fine.
- Whiff% means swinging strikes over swings. Attack zones: Heart (middle), Shadow (edges), Chase, Waste.
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
