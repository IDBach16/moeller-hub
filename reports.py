"""
reports.py -- pre-built report templates for the hub's Coach Assistant.

A coach shouldn't have to know which data source answers which question. These
templates do the joining: one call to build_report() pulls everything we have on
a player (or the team) out of every source, computes the derived rates coaches
actually ask about, and hands the model a single filled-in bundle to write up.

Report types:
  hitter    -- full offensive profile (official line + charted plate discipline + HitTrax)
  pitcher   -- full arsenal profile (official line + charted stuff/command + bullpens)
  bullpen   -- off-season charting work only (Charting App)
  team      -- season overview: record, leaders, game log
  compare   -- two players, same metrics, side by side

Everything here is deterministic: no model calls, no guessing. Missing data comes
back as an explicit note so the write-up can say what's missing instead of
inventing it. agent.py imports this lazily to avoid an import cycle.
"""

import re

# Report types the tool accepts, and what each one needs from the coach.
REPORT_TYPES = ["hitter", "pitcher", "bullpen", "team", "compare"]

PITCH_GROUPS = {
    "fastball": {"fast ball", "fastball", "two seam fast ball", "cut fastball",
                 "sinker", "four seam fast ball", "2 seam fast ball"},
    "breaking": {"slider", "breaking ball", "curveball", "curve", "sweeper"},
    "offspeed": {"change up", "changeup", "splitter", "split finger"},
}

SWING_RESULTS = ("Strike Swing and Miss", "Strike Foul", "Strike In Play")
WHIFF_RESULT = "Strike Swing and Miss"

# Plate-appearance vocabulary from the AtBatResult column.
HIT_BASES = {"1B": 1, "2B": 2, "3B": 3, "HR": 4}
WALKS = {"BB", "IBB"}
NOT_AT_BATS = {"BB", "IBB", "HBP", "Sacrifice", "Catchers Interference"}

NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _agent():
    """agent.py holds the data loaders; import at call time to dodge the cycle."""
    import agent
    return agent


def _pct(n, d, digits=1):
    return round(100.0 * n / d, digits) if d else None


def _rate(n, d, digits=3):
    return round(float(n) / d, digits) if d else None


def _norm_name(name):
    """'Reggie Watson III' -> 'reggie watson', so sources with different name
    conventions (season CSV vs the scraped GCL tables) still line up."""
    parts = [p for p in re.split(r"\s+", str(name).strip().lower()) if p]
    parts = [p.strip(".,") for p in parts if p.strip(".,") not in NAME_SUFFIXES]
    return " ".join(parts)


def _same_person(a, b):
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    pa, pb = na.split(), nb.split()
    # last name plus first initial -- catches 'Rob Smith' vs 'Robert Smith'
    return pa[-1] == pb[-1] and pa[0][:1] == pb[0][:1]


def _find_official(rows, name, key="player"):
    """Pick a player's row out of a GCL stats table."""
    for row in rows or []:
        if row.get("is_totals"):
            continue
        if _same_person(row.get(key, ""), name):
            return {k: v for k, v in row.items()
                    if k not in ("id", "is_totals", "updated_at")}
    return None


def _pitch_group(pitch_type):
    p = str(pitch_type).strip().lower()
    for group, members in PITCH_GROUPS.items():
        if p in members:
            return group
    return "other"


def _count_state(count_str, side):
    """Parse '1 and 2' (and its typos: '2 amd 1', '1 and1') into a count state."""
    digits = re.findall(r"\d", str(count_str))
    if len(digits) < 2:
        return None
    balls, strikes = int(digits[0]), int(digits[1])
    if strikes >= 2:
        return "two_strike"
    if balls > strikes:
        return "hitter_ahead" if side == "hitter" else "pitcher_behind"
    if strikes > balls:
        return "hitter_behind" if side == "hitter" else "pitcher_ahead"
    return "even"


def _hand(value):
    """'L'' and other stray characters show up in the hand columns."""
    v = str(value).strip().upper()
    return v[0] if v[:1] in ("L", "R") else None


# ---------------------------------------------------------------------------
# Derived metrics over a slice of charted pitches
# ---------------------------------------------------------------------------

def _discipline(d):
    """Swing decisions and contact for a slice of pitches (hitter or pitcher view)."""
    if d.empty:
        return None
    swings = d["PitchResult"].isin(SWING_RESULTS)
    whiffs = d["PitchResult"] == WHIFF_RESULT
    in_zone = d["AttackZone"].isin(["Heart", "Shadow"])
    out_zone = d["AttackZone"].isin(["Chase", "Waste"])
    contact = d["PitchResult"].isin(["Strike Foul", "Strike In Play"])
    return {
        "pitches": int(len(d)),
        "strike_pct": _pct(int((d["PitchResult"] != "Ball").sum()), len(d)),
        "swing_pct": _pct(int(swings.sum()), len(d)),
        "whiff_pct": _pct(int(whiffs.sum()), int(swings.sum())),
        "contact_pct": _pct(int((swings & contact).sum()), int(swings.sum())),
        "chase_pct": _pct(int((swings & out_zone).sum()), int(out_zone.sum())),
        "zone_swing_pct": _pct(int((swings & in_zone).sum()), int(in_zone.sum())),
        "zone_pct": _pct(int(in_zone.sum()), int(d["AttackZone"].notna().sum())),
    }


def _pa_line(d):
    """Slash line and outcome mix from charted plate appearances.

    Charted PAs only -- these are the tracked games, not the official book, and
    sacrifice flies can't be told from sac bunts, so both are held out of the
    OBP denominator. Close to the real line, not identical to it.
    """
    pa = d[d["AtBatResult"].notna() & (d["AtBatResult"].astype(str).str.strip() != "")]
    if pa.empty:
        return None
    outcomes = pa["AtBatResult"].value_counts().to_dict()
    n_pa = int(len(pa))
    hits = sum(outcomes.get(k, 0) for k in HIT_BASES)
    bases = sum(outcomes.get(k, 0) * v for k, v in HIT_BASES.items())
    walks = sum(outcomes.get(k, 0) for k in WALKS)
    hbp = outcomes.get("HBP", 0)
    non_ab = sum(outcomes.get(k, 0) for k in NOT_AT_BATS)
    ab = n_pa - non_ab
    on_base_chances = ab + walks + hbp
    strikeouts = outcomes.get("Strike Out", 0)
    out_types = {k: outcomes.get(k, 0)
                 for k in ("Ground Out", "Fly Out", "Line Out", "Infield Fly")
                 if outcomes.get(k)}
    balls_in_play_outs = sum(out_types.values())
    return {
        "charted_pa": n_pa,
        "ab": int(ab),
        "hits": int(hits),
        "avg": _rate(hits, ab),
        "obp": _rate(hits + walks + hbp, on_base_chances),
        "slg": _rate(bases, ab),
        "ops": (round(_rate(hits + walks + hbp, on_base_chances) +
                      _rate(bases, ab), 3)
                if ab and on_base_chances else None),
        "xbh": int(sum(outcomes.get(k, 0) for k in ("2B", "3B", "HR"))),
        "k_pct": _pct(strikeouts, n_pa),
        "bb_pct": _pct(walks + hbp, n_pa),
        "outcomes": outcomes,
        "outs_by_batted_ball": (
            {k: _pct(v, balls_in_play_outs) for k, v in out_types.items()}
            if balls_in_play_outs else None),
    }


def _by_year(d, fn):
    out = {}
    for year, g in d.groupby("Year"):
        if g.empty or year != year:  # skip NaN years
            continue
        out[str(int(year))] = fn(g)
    return out or None


def _scope_frame(d, year):
    """Apply the year filter and describe the scope in words."""
    if year:
        return d[d["Year"] == int(year)], str(int(year))
    years = sorted(int(y) for y in d["Year"].dropna().unique())
    label = (f"career {years[0]}-{years[-1]}" if len(years) > 1
             else (str(years[0]) if years else "career"))
    return d, label


# ---------------------------------------------------------------------------
# Hitter report
# ---------------------------------------------------------------------------

def _hitter_bundle(name, year=None):
    a = _agent()
    df = a._season_df()
    df = df[df["BatterTeam"] == "Moeller"]
    matches = a._match_players(df["Batter"], name)
    if not matches:
        exact = [n for n in df["Batter"].dropna().unique() if _same_person(n, name)]
        matches = exact
    if not matches:
        return {"error": f"no Moeller hitter matching '{name}'",
                "available": sorted(df["Batter"].dropna().unique().tolist())}

    resolved = matches[0]
    d_all = df[df["Batter"].isin(matches)]
    d, scope = _scope_frame(d_all, year)
    notes = []
    if d.empty:
        return {"error": f"{resolved} has no charted pitches in {year}",
                "years_with_data": sorted(int(y) for y in d_all["Year"].dropna().unique())}

    hand = d["Batter Hand"].map(_hand).dropna()
    pitcher_hand = d["PitcherHand"].map(_hand)

    def slice_report(g):
        if g.empty:
            return None
        return {"discipline": _discipline(g), "results": _pa_line(g)}

    by_pitch_group = {}
    groups = d["PitchType"].map(_pitch_group)
    for group in ("fastball", "breaking", "offspeed"):
        g = d[groups == group]
        if len(g) >= 10:
            by_pitch_group[group] = _discipline(g)

    by_count = {}
    states = d["Count"].map(lambda c: _count_state(c, "hitter"))
    for state in ("hitter_ahead", "even", "hitter_behind", "two_strike"):
        g = d[states == state]
        if len(g) >= 10:
            by_count[state] = _discipline(g)

    # Official 2026 line from the scraped GCL table
    official = None
    try:
        rows = a.tool_team_stats("batting").get("rows", [])
        official = _find_official(rows, resolved)
        if official is None:
            notes.append("No official 2026 GCL batting line found under this name.")
    except Exception as e:
        notes.append(f"Official 2026 batting stats unavailable ({e}).")

    # HitTrax cage data (may not be loaded yet)
    hittrax = None
    try:
        ht = a.tool_hittrax(batter=resolved)
        hittrax = ht if not ht.get("error") else {"note": ht["error"]}
    except Exception as e:
        hittrax = {"note": f"HitTrax lookup failed ({e})."}

    notes.append("Charted numbers come from tracked-game pitch data (AWRE); the "
                 "official line comes from the GCL book. They will not match exactly.")

    return {
        "report_type": "hitter",
        "player": resolved,
        "also_matched": matches[1:] or None,
        "scope": scope,
        "bats": hand.mode().iloc[0] if not hand.empty else None,
        "official_2026_line": official,
        "charted_overall": {"discipline": _discipline(d), "results": _pa_line(d)},
        "vs_RHP": slice_report(d[pitcher_hand == "R"]),
        "vs_LHP": slice_report(d[pitcher_hand == "L"]),
        "by_pitch_group": by_pitch_group or None,
        "by_count": by_count or None,
        "by_year": _by_year(d_all, lambda g: {"discipline": _discipline(g),
                                              "results": _pa_line(g)}),
        "hittrax": hittrax,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Pitcher report
# ---------------------------------------------------------------------------

def _arsenal(d):
    """Per-pitch-type stuff and command."""
    out = []
    for ptype, g in d.groupby("PitchType"):
        swings = g["PitchResult"].isin(SWING_RESULTS)
        out_zone = g["AttackZone"].isin(["Chase", "Waste"])
        velo = g["PitchVelo"]
        out.append({
            "pitch_type": ptype,
            "group": _pitch_group(ptype),
            "n": int(len(g)),
            "usage_pct": _pct(len(g), len(d)),
            "avg_velo": round(float(velo.mean()), 1) if velo.notna().any() else None,
            "max_velo": float(velo.max()) if velo.notna().any() else None,
            "strike_pct": _pct(int((g["PitchResult"] != "Ball").sum()), len(g)),
            "whiff_pct": _pct(int((g["PitchResult"] == WHIFF_RESULT).sum()), int(swings.sum())),
            "chase_pct": _pct(int((swings & out_zone).sum()), int(out_zone.sum())),
            "heart_pct": _pct(int((g["AttackZone"] == "Heart").sum()),
                              int(g["AttackZone"].notna().sum())),
        })
    out.sort(key=lambda x: -x["n"])
    return out


def _pitcher_bundle(name, year=None):
    a = _agent()
    df = a._season_df()
    df = df[df["PitcherTeam"] == "Moeller"]
    matches = a._match_players(df["Pitcher"], name)
    if not matches:
        matches = [n for n in df["Pitcher"].dropna().unique() if _same_person(n, name)]
    if not matches:
        return {"error": f"no Moeller pitcher matching '{name}'",
                "available": sorted(df["Pitcher"].dropna().unique().tolist())}

    resolved = matches[0]
    d_all = df[df["Pitcher"].isin(matches)]
    d, scope = _scope_frame(d_all, year)
    notes = []
    if d.empty:
        return {"error": f"{resolved} has no charted pitches in {year}",
                "years_with_data": sorted(int(y) for y in d_all["Year"].dropna().unique())}

    throws = d["PitcherHand"].map(_hand).dropna()
    batter_hand = d["Batter Hand"].map(_hand)

    first_pitch = d[d["Count"].astype(str).str.match(r"^\s*0\D+0")]
    two_strike = d[d["Count"].map(lambda c: _count_state(c, "pitcher")) == "two_strike"]
    ts_swings = two_strike["PitchResult"].isin(SWING_RESULTS)

    zone_counts = d["AttackZone"].value_counts()
    zone_total = int(d["AttackZone"].notna().sum())

    def side(g):
        if g.empty:
            return None
        return {"discipline": _discipline(g), "results_against": _pa_line(g)}

    official = None
    try:
        rows = a.tool_team_stats("pitching").get("rows", [])
        official = _find_official(rows, resolved)
        if official is None:
            notes.append("No official 2026 GCL pitching line found under this name.")
    except Exception as e:
        notes.append(f"Official 2026 pitching stats unavailable ({e}).")

    # Off-season bullpen work, if this arm has been charted
    charting = None
    try:
        cr = tool_bullpen_slice(resolved)
        charting = cr
    except Exception as e:
        charting = {"note": f"Charting App lookup failed ({e})."}

    notes.append("Charted numbers are tracked-game pitch data (AWRE); the official "
                 "line is the GCL book. Velocity is charted-gun velocity.")

    return {
        "report_type": "pitcher",
        "player": resolved,
        "also_matched": matches[1:] or None,
        "scope": scope,
        "throws": throws.mode().iloc[0] if not throws.empty else None,
        "official_2026_line": official,
        "charted_overall": _discipline(d),
        "results_against": _pa_line(d),
        "first_pitch_strike_pct": _pct(int((first_pitch["PitchResult"] != "Ball").sum()),
                                       len(first_pitch)),
        "two_strike": {
            "pitches": int(len(two_strike)),
            "whiff_pct": _pct(int((two_strike["PitchResult"] == WHIFF_RESULT).sum()),
                              int(ts_swings.sum())),
        } if len(two_strike) else None,
        "attack_zone_mix_pct": {z: _pct(int(zone_counts.get(z, 0)), zone_total)
                                for z in ("Heart", "Shadow", "Chase", "Waste")},
        "arsenal": _arsenal(d),
        "vs_RHH": side(d[batter_hand == "R"]),
        "vs_LHH": side(d[batter_hand == "L"]),
        "by_year": _by_year(d_all, lambda g: {
            "pitches": int(len(g)),
            "avg_velo": (round(float(g["PitchVelo"].mean()), 1)
                         if g["PitchVelo"].notna().any() else None),
            "strike_pct": _pct(int((g["PitchResult"] != "Ball").sum()), len(g)),
            "whiff_pct": _pct(int((g["PitchResult"] == WHIFF_RESULT).sum()),
                              int(g["PitchResult"].isin(SWING_RESULTS).sum())),
        }),
        "offseason_charting": charting,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Bullpen report (Charting App only)
# ---------------------------------------------------------------------------

def tool_bullpen_slice(pitcher=None, session_type=None, since=None):
    """The Charting App dashboard, trimmed to one pitcher when asked."""
    a = _agent()
    data = a.tool_charting_report(pitcher=pitcher, session_type=session_type, since=since)
    if isinstance(data, dict) and data.get("error"):
        return {"note": data["error"]}
    if isinstance(data, dict) and not data.get("pitchers"):
        return {"note": "No charted sessions match — the Charting App has no data "
                        "for this filter yet."}
    return data


def _bullpen_bundle(name=None, session_type=None, since=None):
    data = tool_bullpen_slice(name, session_type, since)
    return {
        "report_type": "bullpen",
        "player": name or "all charted pitchers",
        "session_type": session_type or "all sessions",
        "since": since,
        "charting": data,
        "notes": ["Off-season charting data from the Charting App's live database "
                  "(bullpens and live ABs). Separate from tracked-game season data."],
    }


# ---------------------------------------------------------------------------
# Team report
# ---------------------------------------------------------------------------

def _slim(rows, keys, sort_key, top=12):
    out = []
    for r in rows or []:
        if r.get("is_totals"):
            continue
        out.append({k: r.get(k) for k in keys if r.get(k) is not None})
    out.sort(key=lambda r: -(r.get(sort_key) or 0))
    return out[:top]


def _team_totals(rows, kind):
    """The GCL tables carry no totals row, so add up the players ourselves."""
    players = [r for r in rows or [] if not r.get("is_totals")]
    if not players:
        return None

    def total(key):
        return sum((r.get(key) or 0) for r in players)

    if kind == "batting":
        ab, h, bb, hbp = total("ab"), total("h"), total("bb"), total("hbp")
        sf = total("sf")
        tb = total("tb")
        return {"players": len(players), "pa": total("pa"), "ab": ab, "h": h,
                "runs": total("runs"), "hr": total("hr"), "rbi": total("rbi"),
                "sb": total("sb"), "bb": bb, "so": total("so"),
                "avg": _rate(h, ab), "obp": _rate(h + bb + hbp, ab + bb + hbp + sf),
                "slg": _rate(tb, ab)}
    er, ip_outs = total("er"), total("ip_full") * 3 + total("ip_partial")
    innings = round(ip_outs / 3.0, 1)
    return {"players": len(players), "ip": innings, "w": total("w"), "l": total("l"),
            "sv": total("sv"), "so": total("so"), "bb": total("bb"), "h": total("h"),
            "er": er, "hr": total("hr"),
            "era": round(er * 7.0 / (ip_outs / 3.0), 2) if ip_outs else None,
            "whip": round((total("h") + total("bb")) / (ip_outs / 3.0), 3) if ip_outs else None,
            "era_note": "high-school 7-inning ERA"}


def _team_bundle(year=None):
    a = _agent()
    notes = []
    record, batting, pitching, games, totals = None, None, None, None, {}
    try:
        record = a.tool_team_stats("record")
    except Exception as e:
        notes.append(f"Record unavailable ({e}).")
    try:
        rows = a.tool_team_stats("batting").get("rows", [])
        totals["batting"] = _team_totals(rows, "batting")
        batting = _slim(rows, ["player", "class_year", "g", "pa", "ab", "h", "avg",
                               "obp", "slg", "ops", "hr", "rbi", "sb", "bb", "so"],
                        "pa")
    except Exception as e:
        notes.append(f"Team batting unavailable ({e}).")
    try:
        rows = a.tool_team_stats("pitching").get("rows", [])
        totals["pitching"] = _team_totals(rows, "pitching")
        pitching = _slim(rows, ["player", "class_year", "g", "ip", "w", "l", "sv",
                                "era", "whip", "so", "bb", "h", "k9"],
                         "ip_full")
    except Exception as e:
        notes.append(f"Team pitching unavailable ({e}).")
    try:
        g = a.tool_team_stats("games")
        games = g.get("rows", g) if isinstance(g, dict) else g
        if isinstance(games, list):
            games = games[-15:]
    except Exception as e:
        notes.append(f"Game log unavailable ({e}).")

    # Team-wide charted plate discipline for the season
    charted = None
    try:
        df = a._season_df()
        d = df[df["BatterTeam"] == "Moeller"]
        if year:
            d = d[d["Year"] == int(year)]
        arms = df[df["PitcherTeam"] == "Moeller"]
        if year:
            arms = arms[arms["Year"] == int(year)]
        charted = {"offense": {"discipline": _discipline(d), "results": _pa_line(d)},
                   "pitching_staff": {"discipline": _discipline(arms),
                                      "results_against": _pa_line(arms)}}
    except Exception as e:
        notes.append(f"Charted team data unavailable ({e}).")

    return {
        "report_type": "team",
        "scope": str(year) if year else "2026 season",
        "record": record,
        "team_totals": totals or None,
        "batting_leaders": batting,
        "pitching_leaders": pitching,
        "recent_games": games,
        "charted_team": charted,
        "notes": notes or None,
    }


# ---------------------------------------------------------------------------
# Compare report
# ---------------------------------------------------------------------------

def _compare_bundle(name, compare_to, role="hitter", year=None):
    build = _hitter_bundle if role == "hitter" else _pitcher_bundle
    a_side = build(name, year)
    b_side = build(compare_to, year)
    return {
        "report_type": "compare",
        "role": role,
        "scope": a_side.get("scope") or b_side.get("scope"),
        "player_a": a_side,
        "player_b": b_side,
        "notes": ["Same metrics on both sides — compare like for like, and mind the "
                  "sample sizes before drawing conclusions."],
    }


# ---------------------------------------------------------------------------
# Entry point + the roster the UI's dropdowns use
# ---------------------------------------------------------------------------

def build_report(report, player=None, year=None, compare_to=None,
                 session_type=None, since=None, role=None):
    if report not in REPORT_TYPES:
        return {"error": f"unknown report type '{report}'; use one of {REPORT_TYPES}"}
    if report == "team":
        return _team_bundle(year)
    if report == "bullpen":
        return _bullpen_bundle(player, session_type, since)
    if report == "compare":
        if not player or not compare_to:
            return {"error": "a compare report needs two players (player and compare_to)"}
        return _compare_bundle(player, compare_to, role or "hitter", year)
    if not player:
        return {"error": f"a {report} report needs a player name"}
    if report == "hitter":
        return _hitter_bundle(player, year)
    return _pitcher_bundle(player, year)


_options = None


def report_options():
    """Roster + years for the report buttons' dropdowns, computed once."""
    global _options
    if _options is None:
        a = _agent()
        df = a._season_df()
        years = sorted((int(y) for y in df["Year"].dropna().unique()), reverse=True)

        def roster(team_col, name_col):
            """Names with a real sample, each tagged with the years it has data
            so the year picker can filter the list."""
            d = df[df[team_col] == "Moeller"]
            d = d[d[name_col].notna() & (d[name_col].astype(str).str.strip() != "")]
            out = []
            for name, g in d.groupby(name_col):
                if len(g) < 15:
                    continue
                out.append({"name": str(name),
                            "years": sorted(int(y) for y in g["Year"].dropna().unique()),
                            "pitches": int(len(g))})
            out.sort(key=lambda p: p["name"])
            return out

        _options = {
            "hitters": roster("BatterTeam", "Batter"),
            "pitchers": roster("PitcherTeam", "Pitcher"),
            "years": years,
            "reports": REPORT_TYPES,
        }
    return _options
