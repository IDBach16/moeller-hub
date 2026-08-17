"""
test_ingest.py -- Phase B: does the ingest pipeline actually hold up?

We have no real Blast / HitTrax / Rapsodo exports yet, so this builds synthetic
ones that reproduce the awkward parts of real vendor files:

  * a Rapsodo-style file with metadata lines ABOVE the header row
  * a HitTrax-style file whose column names we've never seen
  * values like "86.4 mph", "1,204" and "N/A"
  * a "Lastname, Firstname" player column
  * a name that doesn't match anyone on the roster
  * a cumulative re-export that re-sends sessions we already hold

    python test_ingest.py
"""

import os
import sys
import tempfile

FAILS = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILS.append(label)


TMP = os.path.join(tempfile.mkdtemp(), "ingest.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TMP.replace("\\", "/")
os.environ.pop("HUB_PASSWORD", None)
os.environ.pop("RAILWAY_ENVIRONMENT", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import func, insert, select  # noqa: E402

import db          # noqa: E402
import ingest      # noqa: E402
import metrics     # noqa: E402

engine = db.get_engine()

# --- a small roster to resolve against -------------------------------------
ROSTER = [("Matt", "Ponatoski"), ("Teagan", "Cumberland"), ("Jack", "Ujvagi")]
PID = {}
with engine.begin() as conn:
    for first, last in ROSTER:
        PID[f"{first} {last}"] = conn.execute(insert(db.players).values(
            slug=db.slugify(first, last), first_name=first, last_name=last,
            is_pitcher=(last in ("Ujvagi", "Cumberland")))).inserted_primary_key[0]


def n_rows(table, **where):
    q = select(func.count()).select_from(table)
    for k, v in where.items():
        q = q.where(table.c[k] == v)
    with engine.connect() as conn:
        return conn.execute(q).scalar()


# ===========================================================================
print("\n1. header sniffing")
# ===========================================================================

# Rapsodo puts session metadata above the real header. Assuming line 1 is the
# header would produce one garbage column and zero usable rows.
RAPSODO = b"""Rapsodo Pitching Report
Player: Jack Ujvagi
Session Date: 2026-08-10,,,,

No,Pitch Type,Velocity,Total Spin,IVB,HB,Date
1,Fastball,86.4 mph,2210,17.2,-8.1,2026-08-10
2,Fastball,87.1 mph,2245,17.6,-8.4,2026-08-10
3,Slider,78.2 mph,2410,2.1,9.9,2026-08-10
4,Knuckleball,70.0 mph,1500,5.0,5.0,2026-08-10
"""

s = ingest.sniff(RAPSODO, "rapsodo.csv")
check("finds the header below the preamble", s["header_row"] == 4, str(s["header_row"]))
check("reads 7 columns", len(s["columns"]) == 7, str(s["columns"]))
check("reads 4 data rows", s["row_count"] == 4, str(s["row_count"]))
check("skips the blank separator line",
      all(any(str(v).strip() for v in r.values()) for r in s["rows"]))

# A plain file with the header on line 1 still works.
s2 = ingest.sniff(b"Player,Date,Bat Speed\nMatt Ponatoski,2026-08-01,71.2\n", "b.csv")
check("plain header on line 1", s2["header_row"] == 0)
check("tab-delimited detected",
      ingest.sniff(b"Player\tDate\tEV\nA B\t2026-01-01\t90\n", "t.tsv")["columns"]
      == ["Player", "Date", "EV"])
check("duplicate column names disambiguated",
      ingest._dedupe_header(["a", "a", ""]) == ["a", "a.1", "column_3"],
      str(ingest._dedupe_header(["a", "a", ""])))


# ===========================================================================
print("\n2. value + date + name parsing")
# ===========================================================================

check("'86.4 mph' -> 86.4", ingest._to_float("86.4 mph") == 86.4)
check("'1,204' -> 1204", ingest._to_float("1,204") == 1204.0)
check("'-8.1' keeps its sign", ingest._to_float("-8.1") == -8.1)
check("'N/A' -> None", ingest._to_float("N/A") is None)
check("'' -> None", ingest._to_float("") is None)
check("'--' -> None", ingest._to_float("--") is None)
check("already a float", ingest._to_float(3.5) == 3.5)

check("ISO date", str(ingest._to_date("2026-08-10")) == "2026-08-10")
check("US date", str(ingest._to_date("8/10/2026")) == "2026-08-10")
check("datetime string", str(ingest._to_date("2026-08-10 00:00:00")) == "2026-08-10")
check("ISO timestamp", str(ingest._to_date("2026-08-10T14:22:01")) == "2026-08-10")
check("junk date -> None", ingest._to_date("not a date") is None)

check("'Ponatoski, Matt' == 'Matt Ponatoski'",
      ingest._name_key("Ponatoski, Matt") == ingest._name_key("Matt Ponatoski"))
check("extra whitespace collapses",
      ingest._name_key("Matt   Ponatoski ") == "matt ponatoski")


# ===========================================================================
print("\n3. an unmapped vendor: nothing is guessed into the database")
# ===========================================================================

HITTRAX = b"""Batter,Session Date,Velo,Elevation,Dist,Pitch
Matt Ponatoski,2026-08-01,95.2,12.4,310,62
Matt Ponatoski,2026-08-01,88.1,22.0,255,64
"Ponatoski, Matt",2026-08-01,101.3,9.0,340,61
Teagan Cumberland,2026-08-01,84.0,N/A,190,60
Nobody Here,2026-08-01,70.0,5.0,120,58
"""
imp_id, _ = ingest.store(engine, "hittrax", "hittrax_week1.csv", HITTRAX,
                         uploaded_by="ian", purpose="baseline")
a = ingest.analyze(engine, imp_id)
check("import stored", isinstance(imp_id, int))
check("nothing auto-mapped for an unknown vendor",
      all(c["mapped_to"] is None for c in a["columns"]))
check("not ready to commit yet", a["ready"] is False)
check("says what's missing", len(a["missing_roles"]) == 3, str(a["missing_roles"]))
sug = {c["column"]: c["suggestion"] for c in a["columns"]}
check("suggests Batter -> player", sug.get("Batter") == "player", str(sug))
check("suggests Session Date -> date", sug.get("Session Date") == "date", str(sug))
check("suggests Elevation -> launch_angle", sug.get("Elevation") == "launch_angle", str(sug))
check("shows sample values for mapping",
      any(c["samples"] for c in a["columns"]))

# Committing an unmapped file must refuse rather than write half a dataset.
refused = False
try:
    ingest.commit(engine, imp_id)
except ingest.IngestError:
    refused = True
check("commit refused while unmapped", refused)


# ===========================================================================
print("\n4. mapping, then committing")
# ===========================================================================

ingest.save_mappings(engine, "hittrax", {
    "Batter": "player", "Session Date": "date", "Velo": "exit_velocity",
    "Elevation": "launch_angle", "Dist": "distance", "Pitch": "ignore",
}, confirmed_by="ian")

a = ingest.analyze(engine, imp_id)
check("mapping remembered", a["ready"] is True, str(a["missing_roles"]))
check("'ignore' column stays out of the metrics",
      all(c["mapped_to"] != "ignore" or c["column"] == "Pitch" for c in a["columns"]))

bad = False
try:
    ingest.save_mappings(engine, "hittrax", {"Velo": "not_a_real_metric"})
except ingest.IngestError:
    bad = True
check("an invalid metric key is rejected", bad)

dry = ingest.commit(engine, imp_id, dry_run=True)
check("dry run writes nothing", n_rows(db.sessions) == 0)
check("dry run counts sessions", dry["sessions_new"] == 2, str(dry))

stats = ingest.commit(engine, imp_id)
check("2 sessions created (2 resolved players, 1 date)",
      stats["sessions_new"] == 2, str(stats))
# Ponatoski: 3 swings x 3 metrics = 9. Cumberland: 1 swing, but his launch angle
# is "N/A" -- 2 metrics, not 3. A missing cell must drop that ONE value, not the row.
check("11 measurements written (N/A drops one value, not the row)",
      stats["measurements"] == 11, str(stats))
check("'Lastname, Firstname' resolved to the same player",
      n_rows(db.swings, player_id=PID["Matt Ponatoski"]) == 9,
      str(n_rows(db.swings, player_id=PID["Matt Ponatoski"])))
check("the unknown player's row was NOT attributed",
      stats["rows_unresolved_player"] == 1, str(stats))
check("the unknown name was queued for review", stats["names_queued"] == 1)
check("session grouping fell back to (player, date), and said so",
      stats["synthesized_session_refs"] == 2, str(stats))
check("purpose carried onto the sessions",
      n_rows(db.sessions, purpose="baseline") == 2)
check("import marked committed",
      ingest.analyze(engine, imp_id)["status"] == "committed")

again = False
try:
    ingest.commit(engine, imp_id)
except ingest.IngestError:
    again = True
check("committing twice is refused", again)


# ===========================================================================
print("\n5. the same file cannot be uploaded twice")
# ===========================================================================

dupe = False
try:
    ingest.store(engine, "hittrax", "hittrax_week1_copy.csv", HITTRAX)
except ingest.IngestError as e:
    dupe = "already uploaded" in str(e)
check("identical file rejected by hash", dupe)


# ===========================================================================
print("\n6. a cumulative re-export re-sends old sessions")
# ===========================================================================

# Week 2's export contains week 1's rows again plus a new date. Only the new
# session should land -- this is what makes cumulative and incremental exports
# both work without the coach having to know which kind he has.
HITTRAX_WK2 = HITTRAX + b"""Matt Ponatoski,2026-08-08,97.0,14.0,325,62
Matt Ponatoski,2026-08-08,93.5,10.0,300,63
"""
imp2, _ = ingest.store(engine, "hittrax", "hittrax_week2.csv", HITTRAX_WK2,
                       uploaded_by="ian", purpose="development")
s2 = ingest.commit(engine, imp2)
check("only the new session is added", s2["sessions_new"] == 1, str(s2))
check("the repeated sessions are recognized", s2["sessions_existing"] == 2, str(s2))
check("no duplicate measurements for old sessions",
      n_rows(db.swings, player_id=PID["Matt Ponatoski"]) == 9 + 6,
      str(n_rows(db.swings, player_id=PID["Matt Ponatoski"])))
check("the same unknown name isn't queued twice", s2["names_queued"] == 0, str(s2))


# ===========================================================================
print("\n7. name review closes the loop")
# ===========================================================================

revs = ingest.open_reviews(engine)
check("one open review", len(revs) == 1, str(revs))
check("the queued name is the unresolved one",
      revs and revs[0]["raw_name"] == "Nobody Here", str(revs))

# Accept it against a real player; the alias makes it resolve forever after.
ingest.accept_review(engine, revs[0]["id"], player_id=PID["Jack Ujvagi"],
                     resolved_by="ian")
check("review closed", len(ingest.open_reviews(engine)) == 0)
check("an alias was written",
      n_rows(db.player_aliases, alias="Nobody Here") == 1)

before = n_rows(db.swings)
re_stats = ingest.recommit(engine, imp2)
check("recommit picks up the newly-resolved player",
      re_stats["rows_unresolved_player"] == 0, str(re_stats))
check("recommit didn't duplicate anything",
      n_rows(db.swings) == before + 3,
      f"{before} -> {n_rows(db.swings)}")


# ===========================================================================
print("\n8. pitching side: pitch types and preamble together")
# ===========================================================================

ingest.save_mappings(engine, "rapsodo", {
    "No": "seq", "Pitch Type": "pitch_type", "Velocity": "velocity",
    "Total Spin": "spin_rate", "IVB": "induced_vertical_break",
    "HB": "horizontal_break", "Date": "date",
}, confirmed_by="ian")

# The Rapsodo fixture has no player column, so add one the way a real
# per-player export would carry it.
RAPSODO_NAMED = RAPSODO.replace(
    b"No,Pitch Type,Velocity,Total Spin,IVB,HB,Date",
    b"No,Pitch Type,Velocity,Total Spin,IVB,HB,Date,Pitcher")
RAPSODO_NAMED = b"\n".join(
    line + (b",Jack Ujvagi" if line and b"," in line and not line.startswith(b"No,")
            and b"Rapsodo" not in line and b"Player:" not in line
            and b"Session Date" not in line else b"")
    for line in RAPSODO_NAMED.split(b"\n"))

ingest.save_mappings(engine, "rapsodo", {"Pitcher": "player"}, confirmed_by="ian")
imp3, _ = ingest.store(engine, "rapsodo", "ujvagi_0810.csv", RAPSODO_NAMED,
                       side="pitching", session_type="bullpen", purpose="checkpoint")
a3 = ingest.analyze(engine, imp3)
check("rapsodo mapping applied automatically to a new file",
      a3["ready"] is True, str(a3["missing_roles"]))

s3 = ingest.commit(engine, imp3)
check("pitching rows land in pitch_metrics",
      n_rows(db.pitch_metrics) == s3["measurements"] and s3["measurements"] > 0,
      str(s3))
check("nothing pitching leaked into swings",
      n_rows(db.swings, session_id=None) == 0)
check("'Knuckleball' flagged rather than coerced",
      s3["unknown_pitch_types"] == 1, str(s3))

with engine.connect() as conn:
    types = {r[0] for r in conn.execute(
        select(db.pitch_metrics.c.pitch_type).distinct())}
check("Fastball -> FB and Slider -> SL", {"FB", "SL"} <= types, str(types))
check("the unmapped type is stored as NULL, not invented", None in types, str(types))

with engine.connect() as conn:
    velo = conn.execute(select(db.pitch_metrics.c.value).where(
        (db.pitch_metrics.c.metric_key == "velocity") &
        (db.pitch_metrics.c.seq == 1))).scalar()
check("'86.4 mph' stored as 86.4", velo == 86.4, str(velo))


# ===========================================================================
print("\n9. routes")
# ===========================================================================

import app as hubapp  # noqa: E402
client = hubapp.app.test_client()

r = client.get("/collect")
check("/collect renders", r.status_code == 200, f"got {r.status_code}")
body = r.get_data(as_text=True)
check("upload form present", 'id="upForm"' in body)
check("import history listed", "hittrax_week1.csv" in body)

r = client.get(f"/api/import/{imp_id}")
check("/api/import/<id> returns the analysis", r.status_code == 200 and
      r.get_json().get("vendor") == "hittrax")

r = client.get("/api/players")
check("/api/players returns the roster", r.status_code == 200 and
      len(r.get_json()) == 3, str(r.get_json()))

r = client.get("/api/reviews")
check("/api/reviews returns json", r.status_code == 200)

# Writes must self-disable on a public production URL (spec section 11).
hubapp.os.environ["RAILWAY_ENVIRONMENT"] = "production"
try:
    check("writes disabled in production without a password gate",
          hubapp.writes_enabled() is False)
    r = client.post("/api/import")
    check("upload endpoint returns 403 when writes are off", r.status_code == 403,
          f"got {r.status_code}")
    hubapp.HUB_PASSWORD = "x"
    check("setting a password re-enables writes", hubapp.writes_enabled() is True)
finally:
    hubapp.HUB_PASSWORD = ""
    hubapp.os.environ.pop("RAILWAY_ENVIRONMENT", None)


print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all ingest checks passed\n")
