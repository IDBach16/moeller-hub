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
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
        return 1
    print("all percentile checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
