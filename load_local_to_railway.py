"""Copy the local playerdev.db into the Railway Postgres, once.

The local SQLite file is the real player-development database -- roster, Rapsodo
sessions, pitch metrics, links, goals. The deployed Postgres only ever held what
auto-seed put there on first boot. This lifts one into the other.

    railway connect Postgres --tunnel-only -P 55432     # in another terminal
    python load_local_to_railway.py                     # dry run, writes nothing
    python load_local_to_railway.py --commit            # actually load

Reads the tunnel URL from --url, else $RAILWAY_PG_URL. Everything runs in one
transaction: it all lands or none of it does.

group_summaries is skipped on purpose -- it exists only in the local file, no
module in this repo defines it, and the app never reads it.
"""
import argparse, os, sys, time
from sqlalchemy import Integer, create_engine, inspect, text
import db as schema

SKIP = {"group_summaries"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("RAILWAY_PG_URL", ""))
    ap.add_argument("--sqlite", default="playerdev.db")
    ap.add_argument("--commit", action="store_true", help="write; otherwise dry run")
    a = ap.parse_args()
    if not a.url:
        sys.exit("no Postgres URL -- pass --url or set RAILWAY_PG_URL "
                 "(the tunnel prints one)")

    pg = create_engine(a.url, future=True)
    loc = create_engine(f"sqlite:///{a.sqlite}", future=True)
    insp = inspect(pg)
    remote = set(insp.get_table_names())
    local = set(inspect(loc).get_table_names())
    tables = [t for t in schema.metadata.sorted_tables
              if t.name in local and t.name not in SKIP]

    # --- schema drift: create_all makes missing tables but never adds columns
    ddl = []
    for t in schema.metadata.sorted_tables:
        if t.name not in remote:
            ddl.append(("create table", t.name, ""))
            continue
        have = {c["name"] for c in insp.get_columns(t.name)}
        for c in t.columns:
            if c.name not in have:
                ddl.append(("add column", t.name,
                            f"{c.name} {c.type.compile(pg.dialect)}"))

    print("SCHEMA")
    for kind, tbl, what in ddl or [("", "nothing to change", "")]:
        print(f"   {kind:<12} {tbl:<22} {what}")

    print("\nROWS")
    total = 0
    with loc.connect() as lc, pg.connect() as pc:
        for t in tables:
            n = lc.execute(text(f'select count(*) from "{t.name}"')).scalar()
            r = (pc.execute(text(f'select count(*) from "{t.name}"')).scalar()
                 if t.name in remote else 0)
            if n or r:
                print(f"   {t.name:<22} local {n:>6}   remote {r:>6}"
                      f"   -> {'replace' if r else 'insert'}")
            total += n
    print(f"   {'TOTAL':<22} {total} rows to write")

    if not a.commit:
        print("\nDRY RUN -- nothing written. Re-run with --commit to load.")
        return

    t0 = time.time()
    with loc.connect() as lc, pg.begin() as pc:
        for kind, tbl, what in ddl:
            if kind == "add column":
                pc.execute(text(f'ALTER TABLE "{tbl}" ADD COLUMN {what}'))
        schema.metadata.create_all(pc)          # only creates absent tables
        for t in reversed(tables):              # children before parents
            pc.execute(text(f'DELETE FROM "{t.name}"'))
        for t in tables:                        # parents before children
            rows = [dict(r._mapping) for r in lc.execute(t.select())]
            for i in range(0, len(rows), 1000):
                pc.execute(t.insert(), rows[i:i + 1000])
            if rows:
                print(f"   loaded {t.name:<22} {len(rows)}")
        # ids were copied verbatim, so nudge each sequence past the max. Only
        # integer keys that actually own a sequence -- a text primary key still
        # reports autoincrement, and COALESCE(max(text),0) is a type error.
        for t in schema.metadata.sorted_tables:
            for c in t.columns:
                if not (c.primary_key and isinstance(c.type, Integer)):
                    continue
                seq = pc.execute(text("SELECT pg_get_serial_sequence(:t, :c)"),
                                 {"t": t.name, "c": c.name}).scalar()
                if not seq:
                    continue
                pc.execute(text(
                    "SELECT setval(:s, COALESCE((SELECT MAX(%s) FROM \"%s\"),0)+1, false)"
                    % (c.name, t.name)), {"s": seq})
    print(f"\ndone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
