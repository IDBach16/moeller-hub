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
        ok("every bar is a real percentile",
           all(0 <= b["pct"] <= 100 for b in s["bars"]))
        ok("each bar names its sample and its pool",
           all(b["n"] and b["pool_n"] >= 2 for b in s["bars"]))
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
               all(b["pool_n"] >= percentiles.MIN_POOL for b in g["bars"]),
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
              "chase_pct", "ahead_pct", "woba"}, sorted(pk))
    ok("the hitting strip is exactly the qualified set",
       hk == {"contact_pct", "zcontact_pct", "k2_pct", "zswing_pct",
              "aswing_pct", "k_pct", "ob_pct", "xbh_pct", "woba"}, sorted(hk))
    ok("the hitter's own walk rate is absent (r = 0.12 -- walks are done TO "
       "a hitter, not by him)", "bb_pct" not in hk)
    ok("no direction-free metric is ranked (swing%, first-pitch swing%)",
       not ({"swing_pct", "fpswing_pct", "heart_pct"} & (hk | pk)))
    ok("every metric carries a plain-English blurb",
       all(b and len(b) > 5 for *_r, b in
           percentiles.GAME_PITCHING + percentiles.GAME_HITTING))


def by_pitch():
    print("wOBA")
    import pandas as pd
    # Weights and denominator must match Hitter_Card exactly, or the hub and
    # the printed card quote a player two different wOBAs.
    ok("weights are the card's", percentiles._WOBA_W["HR"] == 2.10
       and percentiles._WOBA_W["1B"] == .89
       and percentiles._WOBA_W["2B"] == 1.27
       and percentiles._WOBA_W["3B"] == 1.62
       and percentiles._WOBA_W["BB"] == .69)
    ok("a walk counts in the denominator", "BB" in percentiles._WOBA_DEN)
    ok("a sacrifice does not inflate it",
       "Sac Bunt" not in percentiles._WOBA_DEN)
    pa = pd.DataFrame({"AtBatResult": ["HR", "BB", "Strike Out", "Ground Out"]})
    got = percentiles._woba(pa)
    ok("hand-computed wOBA matches", got == round((2.10 + .69) / 4, 3),
       "%s vs %s" % (got, round((2.10 + .69) / 4, 3)))
    ok("no plate appearances -> no wOBA",
       percentiles._woba(pd.DataFrame({"AtBatResult": []})) is None)

    print("value stays a number, display carries the formatting")
    # Formatting the value in place made "15.7" < "6.5" a string comparison,
    # which would have silently inverted the strikeout-polarity test rather
    # than failing it. The two fields are kept separate for that reason.
    st = percentiles.game_strip(_a_hitter(), "hitting")
    if st and st["bars"]:
        ok("every bar's value is numeric",
           all(isinstance(b["value"], (int, float)) for b in st["bars"]),
           [(b["label"], type(b["value"]).__name__) for b in st["bars"]])
        ok("every bar has a printable display string",
           all(isinstance(b["display"], str) and b["display"]
               for b in st["bars"]))
        w = next((b for b in st["bars"] if b["label"] == "wOBA"), None)
        if w:
            ok("wOBA prints to three places, batting-style", "." in w["display"]
               and len(w["display"].split(".")[1]) == 3, w["display"])
        r = next((b for b in st["bars"] if b["unit"] == "%"), None)
        if r:
            ok("a rate prints to exactly one place",
               len(r["display"].split(".")[1]) == 1, r["display"])

    print("pitch types come from the data, not a regrouping")
    lbl = percentiles.PITCH_LABEL
    ok("spelling variants collapse",
       lbl["CurveBall"] == lbl["Curve"] == "Curveball"
       and lbl["Fast Ball"] == "Fastball"
       and lbl["Two Seam Fast Ball"] == "Sinker")
    ok("Breaking Ball stays its own bucket, not merged into Slider",
       lbl["Breaking Ball"] == "Breaking Ball" and lbl["Slider"] == "Slider")
    ok("a slider is not called a fastball",
       "Slider" not in percentiles._FASTBALL_FAMILY
       and "Fastball" in percentiles._FASTBALL_FAMILY)

    print("per-family qualification")
    pk = {k: fams for k, _l, _u, _h, _n, _b, fams in percentiles.BY_PITCH_PITCHING}
    hk = {k: fams for k, _l, _u, _h, _n, _b, fams in percentiles.BY_PITCH_HITTING}
    # The headline finding: fastball whiff rate does not correlate with itself
    # (r = 0.33) while breaking-ball whiff rate does (r = 0.69).
    ok("pitcher whiff% is withheld on fastballs", "fb" not in pk["whiff_pct"])
    ok("pitcher whiff% is shown on everything else", "off" in pk["whiff_pct"])
    ok("chases drawn is shown on both", set(pk["chase_pct"]) == {"fb", "off"})
    ok("hitter zone-swing is withheld on fastballs (r = 0.59)",
       "fb" not in hk["zswing_pct"])
    ok("hitter contact% and wOBA are shown on both",
       set(hk["contact_pct"]) == {"fb", "off"} == set(hk["woba"]))

    print("against the real roster")
    import agent
    df = agent._season_df()
    bat = df[df["BatterTeam"] == "Moeller"].groupby("Batter").size()
    if bat.empty:
        print("  [skip] no charted data")
        return
    name = str(bat.idxmax())
    strips = percentiles.game_by_pitch(name, "hitting")
    ok("his most-seen hitter gets pitch-type strips", len(strips) >= 2, name)
    if strips:
        ok("ordered by how much he saw of it",
           [s["n"] for s in strips] == sorted([s["n"] for s in strips],
                                              reverse=True))
        allbars = [b for s in strips for b in s["bars"]]
        ok("nothing is ranked against fewer than the by-pitch pool floor",
           all(b["pool_n"] >= percentiles.BY_PITCH_MIN_POOL for b in allbars),
           [(b["label"], b["pool_n"]) for b in allbars])
        ok("every ranked bar has a percentile and an ordinal",
           all(0 <= b["pct"] <= 100 and b["ord"]
               for b in allbars if not b.get("no_rank")))
        # swing% has no right answer, so it must carry the number WITHOUT a rank
        sw = [b for b in allbars if b["label"] == "Swing%"]
        ok("swing% is shown but not ranked",
           sw and all(b.get("no_rank") and b["pct"] is None for b in sw),
           [(b["label"], b.get("no_rank")) for b in sw])
        ok("no value is left unrounded",
           all(len(str(b["value"]).split(".")[-1]) <= 3 for b in allbars),
           [b["value"] for b in allbars][:6])
        for s in strips:
            ok("thin entries on %s are rounded too" % s["pitch"],
               all(len(str(t["value"]).split(".")[-1]) <= 3 for t in s["thin"]),
               [t["value"] for t in s["thin"]][:4])
            break

    print("fastball whiff is really absent for a pitcher")
    pit = df[df["PitcherTeam"] == "Moeller"].groupby("Pitcher").size()
    if not pit.empty:
        pstrips = percentiles.game_by_pitch(str(pit.idxmax()), "pitching")
        fb = next((s for s in pstrips
                   if s["pitch"] in percentiles._FASTBALL_FAMILY), None)
        if fb:
            ok("no fastball whiff bar on a real pitcher",
               not any(b["label"] == "Whiff%" for b in fb["bars"]),
               [b["label"] for b in fb["bars"]])
            ok("and it is not listed as merely thin either",
               not any(t["label"] == "Whiff%" for t in fb["thin"]))


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
    keys = [k for k, _l, _u, _h in percentiles.STRIP]
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
            ok("every bar is a real percentile",
               all(0 <= b["pct"] <= 100 for b in one["bars"]))
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

    print()
    game()

    print()
    by_pitch()

    print()
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
        return 1
    print("all percentile checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
