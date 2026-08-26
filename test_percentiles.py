"""Percentile strip: the ranking has to be fair before it is pretty.

A percentile is the most confidently-read number on either page -- nobody asks
what "83rd" means -- so a wrong one is worse than no number at all.
"""
import sys

import percentiles

FAILED = []


def ok(label, cond, detail=""):
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", label,
                           "" if cond else "  -- " + str(detail)))
    if not cond:
        FAILED.append(label)


def _a_hitter():
    """The Moeller batter with the most charted pitches."""
    import agent
    df = agent._season_df()
    return str(df[df["BatterTeam"] == "Moeller"].groupby("Batter").size().idxmax())


def _floors(spec):
    return {label: min_n for _k, label, _u, _h, min_n, _b in spec}


def game():
    print("ordinals")
    o = percentiles.ordinal
    ok("33 is 33rd, not 33th", o(33) == "33rd", o(33))
    ok("1/2/3 take st/nd/rd", (o(1), o(2), o(3)) == ("1st", "2nd", "3rd"))
    ok("the teens are all th", (o(11), o(12), o(13)) == ("11th", "12th", "13th"),
       (o(11), o(12), o(13)))
    ok("0 and 100 read sanely", (o(0), o(100)) == ("0th", "100th"))

    import agent
    df = agent._season_df()
    pit = df[df["PitcherTeam"] == "Moeller"].groupby("Pitcher").size()
    bat = df[df["BatterTeam"] == "Moeller"].groupby("Batter").size()
    if pit.empty or bat.empty:
        print("  [skip] no charted game data")
        return

    print("in-season strips")
    workhorse = str(pit.idxmax())
    s = percentiles.game_strip(workhorse, "pitching")
    ok("a heavily-used pitcher gets a strip", s and s["bars"], workhorse)
    if s:
        ok("it is scoped to one season", isinstance(s["year"], int))
        ok("the season is one he actually played", s["year"] in s["years"])
        ranked = [b for b in s["bars"] if not b.get("no_rank")]
        ok("every ranked bar is a real percentile",
           all(0 <= b["pct"] <= 100 for b in ranked))
        ok("each ranked bar names its sample and its pool",
           all(b["n"] and b["pool_n"] >= 2 for b in ranked))
        floors = _floors(percentiles.GAME_PITCHING)
        ok("no bar is drawn below its measured floor",
           all(b["n"] >= floors[b["label"]] for b in s["bars"]),
           [(b["label"], b["n"], floors[b["label"]]) for b in s["bars"]])

    print("polarity")
    # Strikeout rate is the one metric where LOW is good. Drop the inversion and
    # the best contact hitter on the team ranks last.
    ranked = []
    for name in bat[bat >= 150].index:
        g = percentiles.game_strip(str(name), "hitting")
        k = next((b for b in (g or {}).get("bars", [])
                  if b["label"] == "Strikeout%"), None)
        if k:
            ranked.append((str(name), k["value"], k["pct"], k["lower_better"]))
    if len(ranked) >= 3:
        ranked.sort(key=lambda r: r[1])                 # lowest K% first
        ok("the lowest strikeout rate gets the HIGHEST percentile",
           ranked[0][2] > ranked[-1][2],
           "%s K%%=%.1f -> %d  vs  %s K%%=%.1f -> %d"
           % (ranked[0][0], ranked[0][1], ranked[0][2],
              ranked[-1][0], ranked[-1][1], ranked[-1][2]))
        ok("it is flagged so the page can say 'lower is better'",
           all(r[3] for r in ranked))
    else:
        print("  [skip] too few hitters with 150+ pitches")

    print("floors")
    thin_seen = False
    for name in bat[(bat >= 20) & (bat < 80)].index[:10]:
        g = percentiles.game_strip(str(name), "hitting")
        if g and g["thin"]:
            thin_seen = True
            ok("a thin sample is named, never ranked (%s)" % name,
               all(t["n"] < t["need"] or t.get("pool_short")
                   for t in g["thin"]))
            hfloors = _floors(percentiles.GAME_HITTING)
            ok("and it is not silently on the strip too",
               not any(b["label"] == t["label"]
                       for b in g["bars"] for t in g["thin"]))
            ok("the shortfall it reports is the real floor",
               all(t["need"] == hfloors[t["label"]] for t in g["thin"]))
            ok("nothing is ranked against fewer than MIN_POOL teammates",
               all(b["pool_n"] >= percentiles.MIN_POOL
                   for b in g["bars"] if not b.get("no_rank")),
               [(b["label"], b["pool_n"]) for b in g["bars"]])
            break
    if not thin_seen:
        print("  [skip] no thin-sample hitter in this data")

    print("only qualified metrics are on the strips")
    # Checked as an exact key set rather than by matching label text: the
    # rejected "chase-thrown%" and the kept "chases drawn%" are different
    # metrics whose names look alike, and a substring test confuses them.
    hk = {k for k, *_ in percentiles.GAME_HITTING}
    pk = {k for k, *_ in percentiles.GAME_PITCHING}
    ok("the pitching strip is exactly the qualified set",
       pk == {"fb_velo", "strike_pct", "bb_pct", "k_pct", "whiff_pct",
              "chase_pct", "ahead_pct", "woba", "fps_pct", "moestuff"},
       sorted(pk))
    ok("the hitting strip is exactly the qualified set",
       hk == {"contact_pct", "zcontact_pct", "k2_pct", "zswing_pct",
              "aswing_pct", "k_pct", "ob_pct", "xbh_pct", "woba", "babip",
              "moeswing"}, sorted(hk))
    ok("the hitter's own walk rate is absent (r = 0.12 -- walks are done TO "
       "a hitter, not by him)", "bb_pct" not in hk)
    ok("no direction-free metric is ranked (swing%, first-pitch swing%)",
       not ({"swing_pct", "fpswing_pct", "heart_pct"} & (hk | pk)))
    # The BABIP asymmetry, which is the whole reason one is ranked and one is
    # not: hitting it where they aren't is a skill (r = 0.93), preventing it is
    # mostly defence and bounce (r = 0.29).
    pdir = {k: h for k, _l, _u, h, _n, _b in percentiles.GAME_PITCHING}
    hdir = {k: h for k, _l, _u, h, _n, _b in percentiles.GAME_HITTING}
    ok("BABIP is a hitter's stat only -- the pitcher's is off the strip "
       "(r = 0.29: his defence and the bounce, not him)",
       "babip" not in pdir and hdir["babip"] is True)
    ok("MoeStuff+ and MoeSwing+ are both higher-is-better",
       pdir["moestuff"] is True and hdir["moeswing"] is True)

    # These are Ian's own composites; a weight drifting from the card apps
    # would have the hub and the printed card quote different numbers.
    ok("MoeStuff weights match Pitcher_Card",
       percentiles._BASE_SCORE["Strike Swing and Miss"] == 2.0
       and percentiles._LOCATION_SCORE["Chase"] == 3.0
       and percentiles._IN_PLAY_SCORE["HR"] == -1.0)
    ok("MoeSwing weights match Hitter_Card",
       percentiles._ATBAT_SCORE["HR"] == 6.0
       and percentiles._ATBAT_SCORE["Strike Out"] == -3.0
       and percentiles._COUNT_MULT[(3, 0)] == 1.3
       and percentiles._MOESWING_SCALE == 10)
    ok("a home run is not a ball in play (it gave no fielder a chance)",
       "HR" not in percentiles._BIP and "1B" in percentiles._BIP)

    ok("every metric carries a plain-English blurb",
       all(b and len(b) > 5 for *_r, b in
           percentiles.GAME_PITCHING + percentiles.GAME_HITTING))


def main():
    print("the maths")
    ok("lowest of five is 0th", percentiles._pct([1, 2, 3, 4, 5], 1) == 0)
    ok("highest of five is 100th", percentiles._pct([1, 2, 3, 4, 5], 5) == 100)
    ok("middle of five is 50th", percentiles._pct([1, 2, 3, 4, 5], 3) == 50)
    ok("a tie splits the difference rather than rounding up",
       percentiles._pct([1, 3, 3, 5], 3) == 50,
       percentiles._pct([1, 3, 3, 5], 3))
    ok("one man alone is not ranked", percentiles._pct([7], 7) is None)
    ok("Nones are ignored, not counted as zero",
       percentiles._pct([1, None, 5], 5) == 100)

    print("handedness")
    # A lefty's horizontal break is negative by definition. Ranking the raw
    # number sorts every left-hander to the bottom of a column that says
    # nothing about the pitch.
    lefty, righty = {"hb": -14.5}, {"hb": 14.5}
    ok("a lefty's run is a magnitude, not a negative",
       percentiles._run(lefty) == 14.5)
    ok("lefty and righty with equal run rank equally",
       percentiles._run(lefty) == percentiles._run(righty))
    ok("missing break yields no bar", percentiles._run({"hb": None}) is None)

    print("what is measured")
    keys = [k for k, _l, _u, _h, _b in percentiles.STRIP]
    labels = {k: l for k, l, _u, _h, _b in percentiles.STRIP}
    dirs = {k: h for k, _l, _u, h, _b in percentiles.STRIP}
    ok("break is named for what it is, not for jargon",
       labels["ivb"] == "Vertical break" and labels["run"] == "Horizontal break",
       (labels["ivb"], labels["run"]))
    ok("release height is on the strip", "rel_h" in keys, keys)
    # Neither end of the release-height distribution is the good end: a lower
    # slot flattens a fastball, a higher one steepens a curve.
    ok("release height is shown but never ranked", dirs["rel_h"] is None)
    ok("every bullpen metric carries a blurb",
       all(b and len(b) > 4 for *_r, b in percentiles.STRIP))
    ok("no command metric is on the strip",
       not any(k in keys for k in ("strike", "strike_pct", "zone", "location")),
       keys)
    ok("the floor is a real sample", percentiles.MIN_N >= 10)

    print("against the real roster")
    import db
    import rapsodo_card
    engine = db.get_engine()
    cards = rapsodo_card.roster_cards(engine)
    if not cards:
        print("  [skip] no rapsodo data in this database")
    else:
        pid = max(cards, key=lambda k: cards[k]["total"])
        one = percentiles.best_pitch(engine, pid)
        ok("his best pitch is ranked", one and one.get("enough"), one)
        if one and one.get("enough"):
            ok("every ranked bar is a real percentile",
               all(0 <= b["pct"] <= 100
                   for b in one["bars"] if not b.get("no_rank")))
            ok("release height rides along with a number and no rank",
               any(b["label"] == "Release height" and b["pct"] is None
                   for b in one["bars"]),
               [b["label"] for b in one["bars"]])
            ok("he is ranked on his most-thrown pitch",
               one["pitch"] == max(cards[pid]["mix"],
                                   key=lambda m: m["n"])["pt"])
            ok("the pool is named", one["pool"] and one["pool_n"] >= 2)

        many = percentiles.by_pitch(engine, pid)
        ok("the coach gets one strip per pitch", len(many) >= 1)
        ok("unlabelled pitches rank nothing",
           not any(s["pitch"] == "UNK" for s in many))
        ok("strips are ordered most-thrown first",
           [s["n"] for s in many] == sorted([s["n"] for s in many],
                                            reverse=True))
        ok("no pitch under the floor is ranked",
           all(s["n"] >= percentiles.MIN_N for s in many),
           [(s["pitch"], s["n"]) for s in many])
        # The two surfaces must agree: a player's card and his coach's profile
        # showing different percentiles for the same pitch is a support call.
        if one and one.get("enough") and many:
            same = next((s for s in many if s["pitch"] == one["pitch"]), None)
            ok("the player card and the coach profile agree",
               same and [b["pct"] for b in same["bars"]]
                     == [b["pct"] for b in one["bars"]])
            ok("and they agree on the labels too",
               same and [b["label"] for b in same["bars"]]
                     == [b["label"] for b in one["bars"]])

    print()
    game()


    print()
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
        return 1
    print("all percentile checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
