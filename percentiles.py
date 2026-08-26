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

# (key, label, unit, higher_is_better, blurb). Shape and arm strength only --
# no command metric appears here, because bullpen location does not predict
# game command in this data (measured, r = -0.73) and a percentile built on it
# would tell a pitcher something untrue about himself.
STRIP = [
    ("velo", "Velocity", "mph", True, "average off the unit"),
    ("max", "Top velo", "mph", True, "his best rep"),
    ("spin", "Spin rate", "rpm", True, "revolutions per minute"),
    ("eff", "Spin efficiency", "%", True, "how much of the spin moves the ball"),
    ("ivb", "Vertical break", "in", True, "induced, with gravity taken out"),
    # Ranked as a magnitude -- see _run(). A left-hander's horizontal break is
    # negative by definition, which is handedness, not shape.
    ("run", "Horizontal break", "in", True, "arm-side, as a distance"),
    # NOT ranked. Release height is a trait, not a virtue: a lower slot flattens
    # a fastball's approach angle and a higher one steepens a curveball, so
    # neither end of this distribution is the good end. The number is worth
    # seeing -- a gold bar next to it would tell a kid that taller is better.
    ("rel_h", "Release height", "ft", None, "where the ball leaves his hand"),
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
    for key, label, unit, higher_better, blurb in STRIP:
        mine_v = _run(mine) if key == "run" else mine.get(key)
        if mine_v is None:
            continue
        if higher_better is None:
            bars.append({"label": label, "value": mine_v, "unit": unit,
                         "display": mine_v, "blurb": blurb, "no_rank": True,
                         "pct": None, "ord": ""})
            continue
        vals = [(_run(r) if key == "run" else r.get(key)) for _c, r in pool]
        pct = _pct(vals, mine_v)
        if pct is None:
            continue
        rank = pct if higher_better else 100 - pct
        bars.append({"label": label, "value": mine_v, "unit": unit,
                     "display": mine_v, "blurb": blurb,
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
#              BB% of PA        r = 0.85 at 40+ PA, 0.67 at the 30 used
#              chase-induced%   r = 0.75
#              whiff%           r = 0.69
#              strike%          r = 0.68
#              K% of PA         r = 0.67 at 40+ PA, 0.62 at the 30 used
#              ahead-in-count%  r = 0.66
#              heart%           r = 0.82  -- REJECTED on direction, not noise:
#                                            middle-middle is both a strike and
#                                            the most hittable pitch there is
#              XBH allowed%     r = 0.55  -- REJECTED, marginal
#              CSW%             r = 0.52  -- REJECTED, marginal
#              zone%            r = 0.52  -- REJECTED, marginal
#              first-pitch str% r = 0.29  -- REJECTED, noise
#              hits allowed%    r = 0.21  -- REJECTED, noise
#              called-strike%   r = -0.28 -- REJECTED, anti-correlated with
#                                            itself; it is the umpire's stat
#              chase-thrown%    r = 0.00  -- REJECTED, pure noise
#   hitting    contact%         r = 0.89 at 60+ swings
#              zone-contact%    r = 0.84 at 50+ in-zone swings
#              XBH% of PA       r = 0.83 at 40+ PA
#              reached-base%    r = 0.76 at 40+ PA
#              zone-swing%      r = 0.72 at 60+ zone pitches
#              two-strike ctc%  r = 0.71 at 40, 0.69 at the 25 used
#              ahead-ct swing%  r = 0.70 at 50+ ahead-count pitches
#              K% of PA         r = 0.70 at 40+ PA
#              first-pitch sw%  r = 0.70  -- REJECTED on direction: hunting the
#                                            first pitch is an approach, not a
#                                            better or worse one
#              in-play%         r = 0.53  -- REJECTED, marginal
#              chase%           r = 0.52  -- REJECTED, marginal
#              hit% of PA       r = 0.38  -- REJECTED, noise at this sample
#              taken-strike%    r = 0.08  -- REJECTED, noise
#              BB% of PA        r = 0.12  -- REJECTED, noise (the PITCHER's
#                                            walk rate is reliable; the
#                                            hitter's is not -- walks are
#                                            mostly done to you, not by you)
#
# pitches per PA is absent for a different reason: odd/even splitting cannot
# test it. The numerator splits across halves but each PA lands wholly in the
# half holding its final pitch, which anti-correlates the halves by
# construction (r = -0.51). Untestable is not the same as bad, but it does not
# go on the page until there is a valid test for it.
#
# Three floors were relaxed below their first-measured value and RE-MEASURED
# there rather than assumed: 2026 is a thinner charted season than 2025-26, and
# at 40 PA only four pitchers qualified -- too few to rank against at all. They
# were lowered to the point where at least six teammates clear them and the
# correlation still holds (see the two figures above).
#
# The floors are those measured thresholds, not round numbers. Below one, the
# metric is dropped for that player and named as thin rather than drawn faintly:
# half a percentile is not half as useful, it is wrong.

SWING_RESULTS = ("Strike Swing and Miss", "Strike Foul", "Strike In Play")
_WHIFF = "Strike Swing and Miss"
_HITS = ("1B", "2B", "3B", "HR")
_XBH = ("2B", "3B", "HR")
_WALKS = ("BB", "IBB")
_IN_ZONE = ("Heart", "Shadow")
_OUT_ZONE = ("Chase", "Waste")

# Linear weights and the AB/PA vocabularies, copied from Hitter_Card so the hub
# and the printed cards cannot disagree about a player's wOBA.
_WOBA_W = {"BB": .69, "IBB": .69, "HBP": .72, "Catchers Interference": .72,
           "1B": .89, "2B": 1.27, "3B": 1.62, "HR": 2.10}
_AB_RESULTS = {"1B", "2B", "3B", "HR", "Strike Out", "Ground Out", "Fly Out",
               "Line Out", "Double Play", "FC", "Fielders Choice", "Pop Out",
               "Infield Fly", "Error"}
_WOBA_DEN = _AB_RESULTS | {"BB", "IBB", "HBP", "Catchers Interference"}


# (key, label, unit, higher_is_better, min_denominator, blurb)
# The blurb is what the metric means in a coach's words -- these are not all
# self-explanatory, and an unexplained percentile invites a wrong reading.
GAME_PITCHING = [
    ("fb_velo",    "Fastball velocity", "mph", True,  25,
     "average fastball in games"),
    ("strike_pct", "Strike%",           "%",   True,  50,
     "of all pitches thrown"),
    ("bb_pct",     "Walk%",             "%",   False, 30,
     "of batters faced"),
    ("k_pct",      "Strikeout%",        "%",   True,  30,
     "of batters faced"),
    ("whiff_pct",  "Whiff%",            "%",   True,  40,
     "swings he misses"),
    # NOT "Chase%" -- that name reads as the share of pitches he THROWS out of
    # the zone, which is the version that failed the screen (r = 0.00). This is
    # the share of hitters' swings he draws at balls, which is deception.
    ("chase_pct",  "Chases drawn%",     "%",   True,  40,
     "of the balls he threw, hitters swung"),
    ("ahead_pct",  "Ahead in count%",   "%",   True,  80,
     "pitches thrown with more strikes than balls"),
    ("woba",       "wOBA against",      "",    False, 30,
     "every outcome weighted by what it is worth"),
    ("fps_pct",    "First-pitch strike%", "%",  True,  20,
     "of the at-bats he started"),
    ("moestuff",   "MoeStuff+",         "",    True,  60,
     "the Pitcher Card's own score -- 100 is the staff average"),
]

# Zone-win% (strikes + chases drawn, r = 0.60) qualified and is still absent:
# it is a linear combination of two bars already on the strip, so it adds a
# number without adding information. Put-away% (K per two-strike PA) is on the
# Pitcher Card but did NOT qualify here (r = 0.34) and is not carried over.

# swing% and first-pitch swing% are deliberately absent although both are
# reliable: a percentile implies a direction, and swinging more is an approach,
# not a better one. Every metric below has a defensible right answer.
GAME_HITTING = [
    ("contact_pct",  "Contact%",        "%", True,  60,
     "of his swings"),
    ("zcontact_pct", "Zone contact%",   "%", True,  50,
     "on swings at strikes"),
    ("k2_pct",       "2-strike contact%", "%", True, 25,
     "with his back against the wall"),
    ("zswing_pct",   "Zone swing%",     "%", True,  60,
     "does he go after strikes"),
    ("aswing_pct",   "Hitter-count swing%", "%", True, 50,
     "attacks when he is ahead"),
    ("k_pct",        "Strikeout%",      "%", False, 40,
     "of his plate appearances"),
    ("ob_pct",       "Reached base%",   "%", True,  40,
     "hit, walk or hit-by-pitch"),
    ("xbh_pct",      "Extra-base hit%", "%", True,  40,
     "of his plate appearances"),
    ("woba",         "wOBA",            "",  True,  40,
     "every outcome weighted by what it is worth"),
    # BABIP is a HITTER's stat only. His holds up (r = 0.93 at 30+ balls in
    # play) because hitting it where they aren't is repeatable. The pitcher's
    # version was built, measured at r = 0.29 on the game-date split, shown
    # unranked for one commit, and then removed at Ian's call -- a number on a
    # card invites a conclusion whatever the caption says, and there is no
    # conclusion to draw from a staff's batting-average-on-balls-in-play.
    ("babip",        "BABIP",           "%",  True,  30,
     "hits per ball he put in play"),
    ("moeswing",     "MoeSwing+",       "",   True,  30,
     "the Hitter Card's own score -- 100 is the team average"),
]

# A per-player floor is not enough on its own. If only two teammates clear it,
# the only percentiles that exist are 0th and 100th, and 87.5% two-strike
# contact renders as "0th" against a single other player. A metric needs a real
# field behind it or it is not ranked at all that season.
MIN_POOL = 6

# --- MoeStuff+ / MoeSwing+ --------------------------------------------------
# Ian's own composite scores, copied verbatim from Pitcher_Card and
# Hitter_Card so a player cannot be quoted two different numbers by two
# Moeller tools. Changing a weight here without changing it there is the bug
# this comment exists to prevent.
_BASE_SCORE = {"Ball": -0.50, "Strike Looking": 1.0, "Strike Foul": 0.25,
               "Strike Swing and Miss": 2.0, "Strike In Play": 0.0}
_IN_PLAY_SCORE = {"Ground Out": 1.0, "Fly Out": 1.0, "Line Out": 0.5,
                  "Double Play": 2.0, "1B": -0.5, "2B": -0.75, "3B": -0.75,
                  "HR": -1.0}
_LOCATION_SCORE = {"Chase": 3.0, "Shadow": 2.0, "Heart": 1.0, "Waste": 0.5}

_ATBAT_SCORE = {"HR": 6.0, "3B": 5.0, "2B": 4.0, "1B": 3.0, "BB": 2.5,
                "IBB": 2.5, "HBP": 2.0, "Catchers Interference": 2.0,
                "Error": 1.5, "Sac Fly": .5, "Sac Bunt": .5, "Sacrifice": .5,
                "Line Out": -1.5, "Fly Out": -1.8, "FC": -1.8,
                "Fielders Choice": -1.8, "Ground Out": -2.0, "Pop Out": -2.5,
                "Infield Fly": -2.5, "Strike Out": -3.0, "Double Play": -5.0}
_COUNT_MULT = {(3, 0): 1.3, (2, 0): 1.2, (3, 1): 1.2, (2, 1): 1.1,
               (1, 0): 1.05, (0, 0): 1.0, (1, 1): 1.0, (3, 2): 1.0,
               (0, 1): .95, (2, 2): .95, (1, 2): .9, (0, 2): .85}
_MOESWING_SCALE = 10

# Balls actually put in play, for BABIP. Home runs are excluded on purpose --
# they never gave a fielder a chance.
_BIP = ("Ground Out", "Fly Out", "Line Out", "Pop Out", "Infield Fly",
        "Double Play", "FC", "Fielders Choice", "Error", "1B", "2B", "3B")


def _stuff_scores(d):
    """Per-pitch MoeStuff raw score (Pitcher_Card.compute_pitch_scores)."""
    s = d["PitchResult"].map(_BASE_SCORE).fillna(0.0)
    ip = d["PitchResult"] == "Strike In Play"
    s.loc[ip] = s.loc[ip] + d.loc[ip, "AtBatResult"].map(_IN_PLAY_SCORE).fillna(0.0)
    so = d["AtBatResult"] == "Strike Out"
    s.loc[so & (d["PitchResult"] == _WHIFF)] += 2.0
    s.loc[so & (d["PitchResult"] == "Strike Looking")] += 1.5
    return s + d["AttackZone"].map(_LOCATION_SCORE).fillna(0.0)


def _swing_scores(d):
    """Per-pitch MoeSwing raw score (Hitter_Card.compute_moeswing_scores)."""
    import pandas as pd
    heart = d["AttackZone"] == "Heart"
    shadow = d["AttackZone"] == "Shadow"
    chase = d["AttackZone"] == "Chase"
    waste = d["AttackZone"] == "Waste"
    sw = d["PitchResult"].isin(SWING_RESULTS)
    wh = d["PitchResult"] == _WHIFF
    ct = d["PitchResult"].isin(["Strike Foul", "Strike In Play"])
    pr, st = d["PitchResult"], d["Strikes"]

    dec = pd.Series(0.0, index=d.index)
    dec.loc[pr == "Ball"] = 0.3
    dec.loc[(pr == "Ball") & chase] = 0.4
    dec.loc[(pr == "Ball") & waste] = 0.5
    dec.loc[(pr == "Strike Looking") & shadow] = -0.3
    dec.loc[(pr == "Strike Looking") & heart] = -0.6
    dec.loc[sw & chase] = -0.5
    dec.loc[sw & waste] = -0.8
    dec.loc[sw & shadow & ct] = 0.5
    dec.loc[sw & heart] = 0.6

    out = pd.Series(0.0, index=d.index)
    out.loc[pr == "Strike In Play"] = 0.3
    out.loc[(pr == "Strike Foul") & (st < 2)] = -0.1
    out.loc[(pr == "Strike Foul") & (st == 2)] = 0.2
    out.loc[wh & heart] = -0.6
    out.loc[wh & shadow] = -0.4
    out.loc[wh & chase] = -0.3
    out.loc[wh & waste] = -0.2

    ab = d["AtBatResult"].map(_ATBAT_SCORE).fillna(0.0)
    mult = pd.Series([_COUNT_MULT.get((b, k), 1.0)
                      for b, k in zip(d["Balls"], d["Strikes"])], index=d.index)
    return (dec + out + ab) * mult


def _pa_totals(g, col):
    """Sum a per-pitch score within each plate appearance, then hand back the
    per-PA totals. A PA ends on the pitch carrying an AtBatResult, so a reverse
    cumulative count over that marker groups each pitch with the PA it belongs
    to. Rows are chronological within a player."""
    ends = (g["AtBatResult"].notna() & (g["AtBatResult"] != "")).astype(int)
    pa_id = ends[::-1].cumsum()[::-1]
    complete = pa_id > 0                      # trailing pitches with no result
    if not complete.any():
        return None
    return g.loc[complete, col].groupby(pa_id[complete]).sum()


_game_cache = {}
_game_lock = threading.Lock()


def _rate(num, den):
    return (100.0 * num / den) if den else None


def _show(key, val):
    """How the number is PRINTED. Always alongside the numeric value, never
    instead of it -- a formatted string sorts lexically, so "15.7" < "6.5" and
    anything that compares these would be quietly wrong.

    Rates read to one decimal (never %g: a column of 80 / 76.8 / 73.6 looks
    like three different precisions). wOBA is a batting-style figure and reads
    to three, so .600 does not get mistaken for a percentage.
    """
    if key == "woba":
        return ("%.3f" % val).lstrip("0")
    return "%.1f" % val


def _woba(pa_rows):
    """Weighted on-base average -- the one number that says what a plate
    appearance was actually worth. Same weights as the Hitter Card."""
    den = int(pa_rows["AtBatResult"].isin(_WOBA_DEN).sum())
    if not den:
        return None
    num = sum(_WOBA_W.get(r, 0.0) for r in pa_rows["AtBatResult"])
    return round(num / den, 3)


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
        d = df[df["PitcherTeam"] == "Moeller"].copy()
        # MoeStuff+ is a RATIO to the staff average, so the divisor is computed
        # once across the whole staff that season and shared by everyone.
        d["_stuff"] = _stuff_scores(d)
        staff_avg = float(d["_stuff"].mean()) if len(d) else 0.0
        for name, g in d.groupby("Pitcher"):
            sw = g["PitchResult"].isin(SWING_RESULTS)
            fb = g[g["PitchType"].astype(str).str.contains("Fast", case=False,
                                                           na=False)]
            velo = fb["PitchVelo"].dropna()
            pa = g[g["AtBatResult"].notna() & (g["AtBatResult"] != "")]
            n_pa = int(len(pa))
            outside = g[g["AttackZone"].isin(_OUT_ZONE)]
            first = g[(g["Balls"] == 0) & (g["Strikes"] == 0)]
            out[str(name)] = {
                "fps_pct": _rate(int((first["PitchResult"] != "Ball").sum()),
                                 len(first)),
                "fps_pct_n": int(len(first)),
                "moestuff": (round(float(g["_stuff"].mean()) / staff_avg * 100, 1)
                             if staff_avg else None),
                "moestuff_n": int(len(g)),
                "fb_velo": round(float(velo.mean()), 1) if len(velo) else None,
                "fb_velo_n": int(len(velo)),
                "strike_pct": _rate(int((g["PitchResult"] != "Ball").sum()), len(g)),
                "strike_pct_n": int(len(g)),
                "whiff_pct": _rate(
                    int((g["PitchResult"] == _WHIFF).sum()), int(sw.sum())),
                "whiff_pct_n": int(sw.sum()),
                "bb_pct": _rate(int(pa["AtBatResult"].isin(_WALKS).sum()), n_pa),
                "bb_pct_n": n_pa,
                "k_pct": _rate(int((pa["AtBatResult"] == "Strike Out").sum()), n_pa),
                "k_pct_n": n_pa,
                # Swings he drew at balls -- deception, not location.
                "chase_pct": _rate(
                    int(outside["PitchResult"].isin(SWING_RESULTS).sum()),
                    int(len(outside))),
                "chase_pct_n": int(len(outside)),
                "ahead_pct": _rate(int((g["Strikes"] > g["Balls"]).sum()), len(g)),
                "ahead_pct_n": int(len(g)),
                "woba": _woba(pa),
                "woba_n": int(pa["AtBatResult"].isin(_WOBA_DEN).sum()),
                "total": int(len(g)),
            }
    else:
        d = df[df["BatterTeam"] == "Moeller"].copy()
        # MoeSwing+ is a z-score against the team, so both the team mean and the
        # between-player spread come from the same season.
        d["_swing"] = _swing_scores(d)
        _pa_means = {}
        for _n, _g in d.groupby("Batter"):
            _t = _pa_totals(_g, "_swing")
            if _t is not None and len(_t):
                _pa_means[str(_n)] = (float(_t.mean()), int(len(_t)))
        _qual = [v for v, k in _pa_means.values() if k >= 15]
        if len(_qual) < 3:
            _qual = [v for v, _k in _pa_means.values()]
        team_mean = (sum(_qual) / len(_qual)) if _qual else 0.0
        if len(_qual) > 1:
            _var = sum((x - team_mean) ** 2 for x in _qual) / (len(_qual) - 1)
            between_sd = _var ** 0.5 or 1.0
        else:
            between_sd = 1.0

        for name, g in d.groupby("Batter"):
            n_sw = int(g["PitchResult"].isin(SWING_RESULTS).sum())
            zone = g[g["AttackZone"].isin(_IN_ZONE)]
            z_sw = int(zone["PitchResult"].isin(SWING_RESULTS).sum())
            two = g[g["Strikes"] == 2]
            t_sw = int(two["PitchResult"].isin(SWING_RESULTS).sum())
            ahead = g[g["Balls"] > g["Strikes"]]
            pa = g[g["AtBatResult"].notna() & (g["AtBatResult"] != "")]
            n_pa = int(len(pa))
            bip = pa[pa["AtBatResult"].isin(_BIP)]
            _mine = _pa_means.get(str(name))
            out[str(name)] = {
                "babip": _rate(int(bip["AtBatResult"].isin(_HITS[:3]).sum()),
                               len(bip)),
                "babip_n": int(len(bip)),
                "moeswing": (round(100 + ((_mine[0] - team_mean) / between_sd)
                                   * _MOESWING_SCALE, 1) if _mine else None),
                "moeswing_n": (_mine[1] if _mine else 0),
                "contact_pct": _rate(
                    n_sw - int((g["PitchResult"] == _WHIFF).sum()), n_sw),
                "contact_pct_n": n_sw,
                # Contact on pitches he SHOULD hit, separate from chases.
                "zcontact_pct": _rate(
                    z_sw - int((zone["PitchResult"] == _WHIFF).sum()), z_sw),
                "zcontact_pct_n": z_sw,
                "k2_pct": _rate(
                    t_sw - int((two["PitchResult"] == _WHIFF).sum()), t_sw),
                "k2_pct_n": t_sw,
                "zswing_pct": _rate(z_sw, int(len(zone))),
                "zswing_pct_n": int(len(zone)),
                "aswing_pct": _rate(
                    int(ahead["PitchResult"].isin(SWING_RESULTS).sum()),
                    int(len(ahead))),
                "aswing_pct_n": int(len(ahead)),
                "k_pct": _rate(int((pa["AtBatResult"] == "Strike Out").sum()), n_pa),
                "k_pct_n": n_pa,
                "ob_pct": _rate(
                    int(pa["AtBatResult"].isin(_HITS + _WALKS + ("HBP",)).sum()),
                    n_pa),
                "ob_pct_n": n_pa,
                "xbh_pct": _rate(int(pa["AtBatResult"].isin(_XBH).sum()), n_pa),
                "xbh_pct_n": n_pa,
                "woba": _woba(pa),
                "woba_n": int(pa["AtBatResult"].isin(_WOBA_DEN).sum()),
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
    for key, label, unit, higher_better, min_n, blurb in spec:
        n = mine.get(key + "_n") or 0
        val = mine.get(key)
        if val is None:
            continue
        if n < min_n:
            thin.append({"label": label, "value": round(val, 3),
                         "display": _show(key, val), "unit": unit,
                         "n": n, "need": min_n})
            continue
        # An unranked metric is just a number, so it needs no comparison pool
        # at all. Checking one withheld BABIP against entirely -- only four
        # pitchers cleared the sample floor, which is irrelevant to a figure
        # that was never going to carry a percentile.
        if higher_better is None:
            bars.append({"label": label, "value": round(val, 3),
                         "display": _show(key, val), "unit": unit,
                         "blurb": blurb, "n": n, "pool_n": None,
                         "no_rank": True, "pct": None, "ord": ""})
            continue

        # Peers must clear the same floor, or a teammate with nine swings sets
        # the bottom of the scale.
        vals = [r[key] for r in table.values()
                if r.get(key) is not None and (r.get(key + "_n") or 0) >= min_n]
        if len(vals) < MIN_POOL:
            # Too few teammates qualified to make a percentile mean anything.
            # He is not short of sample -- the field is -- so say that instead
            # of quoting him a rank out of two.
            thin.append({"label": label, "value": round(val, 3),
                         "display": _show(key, val), "unit": unit,
                         "n": n, "need": min_n, "pool_n": len(vals),
                         "pool_short": True})
            continue
        pct = _pct(vals, val)
        if pct is None:
            continue
        rank = pct if higher_better else 100 - pct
        bars.append({"label": label, "value": round(val, 3), "unit": unit,
                     "display": _show(key, val),
                     "pct": rank, "ord": ordinal(rank), "blurb": blurb,
                     "n": n, "pool_n": len(vals),
                     "lower_better": not higher_better})

    if not bars and not thin:
        return None
    return {"year": year, "years": years, "side": side, "bars": bars,
            "thin": thin, "total": mine.get("total", 0)}
