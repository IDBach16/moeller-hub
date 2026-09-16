"""Re-measure the reliability of every Blast metric.

    python blast/reliability.py

Run this before adding a bar to percentiles.BLAST_STRIP, and again whenever the
data grows enough to change the picture. The numbers in the comment above
BLAST_STRIP came from this script; a comment is a record of a measurement, not a
substitute for one, and this is the only thing that can tell you the record has
gone stale.

THE TEST. Split each hitter's SESSIONS in a drill odd/even by date, average each
half, correlate the halves across hitters, then Spearman-Brown correct
(r_full = 2r / (1 + r)) because each half holds only half the data. Ship at
r >= 0.60, the same bar percentiles.py uses everywhere else.

WHY SESSIONS AND NOT SWINGS. Splitting a hitter's individual swings odd/even
interleaves them inside the same session, so both halves share that day's bat,
that day's drill and that day's coach -- which cancels out exactly the variation
the test is meant to find, and inflates r. The pitching side learned this the
hard way: two game-strip bars scored 0.72 and 0.76 on the pitch split and 0.51
and 0.39 on the honest one. Sessions are the honest unit here.

WHY UNTAGGED SWINGS ARE EXCLUDED. An untagged pile is a mix of drills, so its
half-means differ for reasons that have nothing to do with the hitter.

WHY THE FLOOR IS SWEPT. A correlation measured at 40 swings does not license a
bar shipped at 25. percentiles.py had to relax three game floors and RE-MEASURE
at the relaxed value rather than assume; this prints every candidate floor so
that is a reading rather than a hope.

CLEARING 0.60 IS NOT SUFFICIENT, only necessary. A metric also needs a defensible
DIRECTION before it can be ranked, and six Blast metrics have none -- see the
note above percentiles.BLAST_STRIP. On this side it is direction, not noise, that
keeps bars off the page.
"""

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select                      # noqa: E402

import db                                          # noqa: E402
import percentiles                                 # noqa: E402

# Candidate floors to sweep. 25 is what BLAST_STRIP ships.
FLOORS = (20, 25, 30, 40)

METRICS = [k for k, *_ in percentiles.BLAST_STRIP]
DIRECTION = {k: h for k, _l, _u, h, _n, _b in percentiles.BLAST_STRIP}


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def spearman_brown(r):
    """Each half holds half the data, so the raw r understates the whole."""
    return None if r is None else 2 * r / (1 + r)


def load(engine):
    """(metric, drill, player) -> {date: [values]}, tagged drills only."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.swings.c.player_id, db.sessions.c.session_date,
                   db.swings.c.context, db.swings.c.metric_key,
                   db.swings.c.value)
            .select_from(db.sessions.join(
                db.swings, db.swings.c.session_id == db.sessions.c.id))
            .where(db.swings.c.value.isnot(None))
            .where(db.swings.c.context.isnot(None))).all()
    acc = defaultdict(lambda: defaultdict(list))
    for r in rows:
        acc[(r.metric_key, r.context, r.player_id)][r.session_date].append(
            float(r.value))
    return acc


def measure(acc, metric, floor):
    """(n_pairs, r, r_corrected) at this floor, over every hitter-drill block."""
    A, B = [], []
    for (key, _ctx, _pid), by_date in acc.items():
        if key != metric:
            continue
        dates = sorted(by_date)
        total = sum(len(v) for v in by_date.values())
        if total < floor or len(dates) < 2:
            continue
        odd = [v for i, d in enumerate(dates) if i % 2 == 0 for v in by_date[d]]
        even = [v for i, d in enumerate(dates) if i % 2 == 1 for v in by_date[d]]
        if not odd or not even:
            continue
        A.append(sum(odd) / len(odd))
        B.append(sum(even) / len(even))
    r = pearson(A, B)
    return len(A), r, spearman_brown(r)


def main():
    engine = db.get_engine()
    acc = load(engine)
    shipped = percentiles.BLAST_MIN_N

    for floor in FLOORS:
        mark = "   <- the floor BLAST_STRIP ships" if floor == shipped else ""
        print(f"\n=== per-hitter floor: {floor} swings in one drill, "
              f">= 2 sessions ==={mark}")
        print(f"  {'metric':26s} {'pairs':>6s} {'r':>7s} {'r_SB':>7s}  "
              f"{'verdict':10s} ranked?")
        print("  " + "-" * 72)
        for m in METRICS:
            n, r, rs = measure(acc, m, floor)
            ranked = {True: "yes", False: "yes (low)", None: "NO -- no direction"}[
                DIRECTION[m]]
            if r is None:
                print(f"  {m:26s} {n:6d}       -       -  too few")
                continue
            verdict = ("RANKABLE" if rs >= 0.60
                       else "marginal" if rs >= 0.45 else "NOISE")
            flag = "  <-- SHIPPED BUT UNDER THE BAR" if (
                rs < 0.60 and DIRECTION[m] is not None) else ""
            print(f"  {m:26s} {n:6d} {r:7.3f} {rs:7.3f}  {verdict:10s} "
                  f"{ranked}{flag}")

    print("\nA metric must clear 0.60 AND have a direction before it is ranked.")
    print("Anything marked 'SHIPPED BUT UNDER THE BAR' is a bar to pull from")
    print("percentiles.BLAST_STRIP, or to raise BLAST_MIN_N for.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
