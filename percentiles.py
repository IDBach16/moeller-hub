"""Where a pitcher's stuff sits against the arms he is actually measured with.

Savant's percentile strip is the one thing on that page a coach reads without
being taught it -- "83rd" lands instantly where "2048 rpm" does not. This is
that idea, honestly scoped: the population is Moeller pitchers, and every
surface that shows these numbers says so.

Shared by two surfaces with different needs. A player sees one strip for the
pitch he throws most (his card is not a place to bury him in tabs); a coach
sees one per pitch type, because the question "is his slider any good" is a
different question from "is his fastball any good".
"""
import threading

from sqlalchemy import select

import db
import rapsodo_card

# Tracked pitches of a type before it is ranked, or counted in anyone else's
# ranking. Below this the average still moves with every rep, so a percentile
# reports attendance rather than stuff.
MIN_N = 10

# Enough same-level peers for "vs varsity" to mean something. Under it the
# comparison falls back to the whole staff and says which population it used.
MIN_PEERS = 6

# (key, label, unit, higher_is_better). Shape and arm strength only -- no
# command metric appears here, because bullpen location does not predict game
# command in this data (measured, r = -0.73) and a percentile built on it would
# tell a pitcher something untrue about himself.
STRIP = [
    ("velo", "Velocity", "mph", True),
    ("max", "Top velo", "mph", True),
    ("spin", "Spin rate", "rpm", True),
    ("eff", "Spin efficiency", "%", True),
    ("ivb", "Ride (IVB)", "in", True),
    ("run", "Run", "in", True),
]


def _pct(values, mine):
    """Percentile of `mine` within `values`, which includes him."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    below = sum(1 for v in vals if v < mine)
    ties = sum(1 for v in vals if v == mine) - 1          # not against himself
    return int(round(100.0 * (below + 0.5 * max(ties, 0)) / (len(vals) - 1)))


def _run(mix_row):
    """Arm-side run as a magnitude.

    A left-hander's horizontal break is negative by definition, so ranking the
    raw number sorts every lefty to the bottom of a column that has nothing to
    do with how good the pitch is.
    """
    hb = mix_row.get("hb")
    return None if hb is None else abs(hb)


def _one(cards, player_id, pitch, level):
    """Bars for a single pitch type, or None if he can't be ranked on it."""
    mine = next((m for m in cards[player_id].get("mix", [])
                 if m["pt"] == pitch), None)
    if not mine or mine["n"] < MIN_N:
        return None

    peers = []
    for pid, card in cards.items():
        row = next((m for m in card.get("mix", []) if m["pt"] == pitch), None)
        if row and row["n"] >= MIN_N:
            peers.append((card, row))

    same = [p for p in peers if level and p[0].get("level") == level]
    pool, pool_label = ((same, level) if len(same) >= MIN_PEERS
                        else (peers, "the staff"))

    bars = []
    for key, label, unit, higher_better in STRIP:
        mine_v = _run(mine) if key == "run" else mine.get(key)
        if mine_v is None:
            continue
        vals = [(_run(r) if key == "run" else r.get(key)) for _c, r in pool]
        pct = _pct(vals, mine_v)
        if pct is None:
            continue
        rank = pct if higher_better else 100 - pct
        bars.append({"label": label, "value": mine_v, "unit": unit,
                     "pct": rank, "ord": ordinal(rank)})
    if not bars:
        return None
    return {"pitch": pitch, "n": mine["n"], "bars": bars,
            "pool": pool_label, "pool_n": len(pool), "level": level}


def best_pitch(engine, player_id):
    """One strip, for the pitch he throws most.

    Not automatically the fastball: a reliever throwing 60% sliders should be
    measured on the slider.
    """
    cards = rapsodo_card.roster_cards(engine)
    mine = cards.get(player_id)
    if not mine or not mine.get("mix"):
        return None
    top = max(mine["mix"], key=lambda m: m["n"])
    got = _one(cards, player_id, top["pt"], mine.get("level"))
    if got:
        return dict(got, enough=True)
    # Say why it is empty rather than rendering nothing.
    return {"pitch": top["pt"], "n": top["n"], "enough": False, "min_n": MIN_N}


def by_pitch(engine, player_id):
    """One strip per pitch type he throws enough of -- the coach's view.

    'Is his slider any good' is a different question from 'is his fastball any
    good', and pooling them would answer neither.
    """
    cards = rapsodo_card.roster_cards(engine)
    mine = cards.get(player_id)
    if not mine or not mine.get("mix"):
        return []
    out = []
    for row in mine["mix"]:
        if row["pt"] == "UNK":      # unlabelled pitches rank nothing
            continue
        got = _one(cards, player_id, row["pt"], mine.get("level"))
        if got:
            out.append(got)
    # Most-thrown first: that is the pitch the conversation starts with.
    out.sort(key=lambda s: -s["n"])
    return out


# ===========================================================================
# In-season percentiles, from the charted game data
# ===========================================================================
#
# The bullpen strip above answers "what is his stuff". This answers "what has
# he actually done in games" -- the question a coach asks second and a player
# asks first. It is also the only percentile a hitter can have: there is no
# bat-tracking data in the system yet, but every one of his plate appearances
# has been charted for three seasons.
#
# EVERY metric here was reliability-tested before it was allowed on the page,
# by splitting each player's own pitches odd/even and correlating the halves
# (Spearman-Brown corrected). A rate that does not correlate with itself
# cannot rank anybody, however reasonable it looks:
#
#   pitching   FB velocity      true/noise 17.6  -- decisive; it is a
#                                                   measurement, not a rate
#              strike%          r = 0.68
#              whiff%           r = 0.69
#              chase-thrown%    r = 0.00         -- REJECTED, pure noise
#   hitting    contact%         r = 0.89 at 60+ swings
#              zone-swing%      r = 0.72 at 60+ zone pitches
#              K% of PA         r = 0.70 at 40+ PA
#              XBH% of PA       r = 0.83 at 40+ PA
#              chase%           r = 0.52         -- REJECTED, marginal
#              BB% of PA        r = 0.12         -- REJECTED, noise
#
# The floors are those measured thresholds, not round numbers. Below one, the
# metric is dropped for that player and named as thin rather than drawn faintly:
# half a percentile is not half as useful, it is wrong.

SWING_RESULTS = ("Strike Swing and Miss", "Strike Foul", "Strike In Play")
_XBH = ("2B", "3B", "HR")

# (key, label, unit, higher_is_better, min_denominator)
GAME_PITCHING = [
    ("fb_velo",    "Fastball velocity", "mph", True, 25),
    ("strike_pct", "Strike%",           "%",   True, 50),
    ("whiff_pct",  "Whiff%",            "%",   True, 40),
]

# swing% is deliberately absent: a percentile implies a direction, and swinging
# more is neither good nor bad. zone-swing% has a direction -- go after strikes.
GAME_HITTING = [
    ("contact_pct", "Contact%",        "%", True,  60),
    ("zswing_pct",  "Zone swing%",     "%", True,  60),
    ("k_pct",       "Strikeout%",      "%", False, 40),
    ("xbh_pct",     "Extra-base hit%", "%", True,  40),
]

_game_cache = {}
_game_lock = threading.Lock()


def _rate(num, den):
    return (100.0 * num / den) if den else None


def ordinal(n):
    """33 -> '33rd'. A page that says '33th' is a page nobody trusts."""
    if n is None:
        return ""
    if 11 <= (n % 100) <= 13:
        return "%dth" % n
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _game_table(year, side):
    """Every Moeller player's in-season rates for one season, keyed by the name
    as the charter typed it. Cached -- this reads a 19,500-row CSV."""
    key = (year, side)
    with _game_lock:
        if key in _game_cache:
            return _game_cache[key]

    import agent
    df = agent._season_df()
    df = df[df["Year"] == year]
    out = {}

    if side == "pitching":
        d = df[df["PitcherTeam"] == "Moeller"]
        for name, g in d.groupby("Pitcher"):
            sw = g["PitchResult"].isin(SWING_RESULTS)
            fb = g[g["PitchType"].astype(str).str.contains("Fast", case=False,
                                                           na=False)]
            velo = fb["PitchVelo"].dropna()
            out[str(name)] = {
                "fb_velo": round(float(velo.mean()), 1) if len(velo) else None,
                "fb_velo_n": int(len(velo)),
                "strike_pct": _rate(int((g["PitchResult"] != "Ball").sum()), len(g)),
                "strike_pct_n": int(len(g)),
                "whiff_pct": _rate(
                    int((g["PitchResult"] == "Strike Swing and Miss").sum()),
                    int(sw.sum())),
                "whiff_pct_n": int(sw.sum()),
                "total": int(len(g)),
            }
    else:
        d = df[df["BatterTeam"] == "Moeller"]
        for name, g in d.groupby("Batter"):
            n_sw = int(g["PitchResult"].isin(SWING_RESULTS).sum())
            zone = g[g["AttackZone"].isin(["Heart", "Shadow"])]
            pa = g[g["AtBatResult"].notna() & (g["AtBatResult"] != "")]
            n_pa = int(len(pa))
            out[str(name)] = {
                "contact_pct": _rate(
                    n_sw - int((g["PitchResult"] == "Strike Swing and Miss").sum()),
                    n_sw),
                "contact_pct_n": n_sw,
                "zswing_pct": _rate(
                    int(zone["PitchResult"].isin(SWING_RESULTS).sum()), int(len(zone))),
                "zswing_pct_n": int(len(zone)),
                "k_pct": _rate(int((pa["AtBatResult"] == "Strike Out").sum()), n_pa),
                "k_pct_n": n_pa,
                "xbh_pct": _rate(int(pa["AtBatResult"].isin(_XBH).sum()), n_pa),
                "xbh_pct_n": n_pa,
                "total": int(len(g)),
            }

    with _game_lock:
        _game_cache[key] = out
    return out


def game_years(name, side):
    """Seasons this player actually appears in, newest first."""
    import agent
    df = agent._season_df()
    col, team = (("Pitcher", "PitcherTeam") if side == "pitching"
                 else ("Batter", "BatterTeam"))
    d = df[(df[team] == "Moeller") & (df[col] == name)]
    return sorted({int(y) for y in d["Year"].dropna().unique()}, reverse=True)


def game_strip(name, side, year=None):
    """In-season percentiles for one player against his own team that season.

    Ranked within ONE season on purpose: a sophomore's 2024 and a senior's 2026
    are different populations, and pooling them ranks a player against a
    version of his teammates that no longer exists.
    """
    years = game_years(name, side)
    if not years:
        return None
    year = int(year) if year else years[0]

    table = _game_table(year, side)
    mine = table.get(name)
    if not mine:
        return None

    spec = GAME_PITCHING if side == "pitching" else GAME_HITTING
    bars, thin = [], []
    for key, label, unit, higher_better, min_n in spec:
        n = mine.get(key + "_n") or 0
        val = mine.get(key)
        if val is None:
            continue
        if n < min_n:
            thin.append({"label": label, "value": round(val, 1), "unit": unit,
                         "n": n, "need": min_n})
            continue
        # Peers must clear the same floor, or a teammate with nine swings sets
        # the bottom of the scale.
        vals = [r[key] for r in table.values()
                if r.get(key) is not None and (r.get(key + "_n") or 0) >= min_n]
        pct = _pct(vals, val)
        if pct is None:
            continue
        rank = pct if higher_better else 100 - pct
        bars.append({"label": label, "value": round(val, 1), "unit": unit,
                     "pct": rank, "ord": ordinal(rank),
                     "n": n, "pool_n": len(vals),
                     "lower_better": not higher_better})

    if not bars and not thin:
        return None
    return {"year": year, "years": years, "side": side, "bars": bars,
            "thin": thin, "total": mine.get("total", 0)}
