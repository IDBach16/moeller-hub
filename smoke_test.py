"""
smoke_test.py -- does the hub still work after the Phase A re-plumb?

Runs against a throwaway SQLite database so it never touches the real one, and
never requires Postgres or an ANTHROPIC_API_KEY.

    python smoke_test.py

Checks, in order:
  1. the schema builds on a clean database
  2. the metric registry is internally consistent
  3. pitch-type normalization maps what it should and refuses what it shouldn't
  4. every asset URL the old app served still resolves
  5. the home page renders with all 8 tool cards
  6. /api/health reports the database
  7. the git-push endpoint stays disabled while the gate is off
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


TMP = os.path.join(tempfile.mkdtemp(), "smoke.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TMP.replace("\\", "/")
os.environ.pop("HUB_PASSWORD", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db          # noqa: E402
import metrics     # noqa: E402
import tools       # noqa: E402


print("\n1. schema")
engine = db.get_engine()
from sqlalchemy import inspect, insert, select  # noqa: E402
names = set(inspect(engine).get_table_names())
expected = {"players", "player_aliases", "player_vendor_ids", "raw_imports",
            "column_maps", "name_review", "sessions", "swings", "pitch_metrics",
            "goals", "interventions", "player_baselines", "change_events",
            "ai_summaries"}
check(f"all {len(expected)} tables created", expected <= names,
      f"missing {sorted(expected - names)}")
check("sqlite backend for the test", not db.is_postgres())

# A player round-trips, and the vendor-id uniqueness constraint holds.
with engine.begin() as conn:
    pid = conn.execute(insert(db.players).values(
        slug="test-player", first_name="Test", last_name="Player")).inserted_primary_key[0]
    conn.execute(insert(db.player_vendor_ids).values(
        player_id=pid, vendor="blast", vendor_id="999999"))
with engine.connect() as conn:
    got = conn.execute(select(db.players.c.slug)).scalar()
check("player round-trips", got == "test-player", got)

dup_rejected = False
try:
    with engine.begin() as conn:
        conn.execute(insert(db.player_vendor_ids).values(
            player_id=pid, vendor="blast", vendor_id="999999"))
except Exception:
    dup_rejected = True
check("duplicate vendor id rejected", dup_rejected)


print("\n2. metric registry")
check("registry is non-empty", len(metrics.REGISTRY) > 0)
check("every key matches its Metric.key",
      all(k == m.key for k, m in metrics.REGISTRY.items()))
check("every metric has a positive mmc",
      all(m.mmc > 0 for m in metrics.REGISTRY.values()))
check("every metric has a positive min_n",
      all(m.min_n > 0 for m in metrics.REGISTRY.values()))
check("every side is pitching or hitting",
      all(m.side in ("pitching", "hitting") for m in metrics.REGISTRY.values()))
bad_band = [m.key for m in metrics.REGISTRY.values()
            if m.polarity == metrics.TARGET_BAND and
            (m.target_band is None or m.target_band[0] >= m.target_band[1])]
check("every target_band metric has a valid band", not bad_band, str(bad_band))
check("headline metrics exist on both sides",
      metrics.headline("pitching") and metrics.headline("hitting"))
# Polarity actually drives the verdict -- the bug that would congratulate a
# hitter for a worse time-to-contact.
check("higher_better: +delta is favorable",
      metrics.get("bat_speed").favorable(1.0) is True)
check("lower_better: +delta is NOT favorable",
      metrics.get("time_to_contact").favorable(0.02) is False)
check("target_band has no simple verdict",
      metrics.get("attack_angle").favorable(1.0) is None)
check("target_band knows in-band", metrics.get("attack_angle").in_band(10.0) is True)
check("target_band knows out-of-band", metrics.get("attack_angle").in_band(22.0) is False)
# Every Blast column maps to a real metric key or a structural role.
roles = set(db.COLUMN_ROLES)
unknown = [k for (k, _u) in metrics.BLAST_COLUMNS.values()
           if k not in metrics.REGISTRY and k not in roles]
check("every Blast column maps to a known key or role", not unknown, str(unknown))


print("\n3. pitch-type normalization")
check("Four-Seam -> FB", metrics.normalize_pitch_type("Four-Seam") == "FB")
check("sweeper -> SL", metrics.normalize_pitch_type("sweeper") == "SL")
check("already-canonical CB survives", metrics.normalize_pitch_type("CB") == "CB")
check("unknown returns None (goes to QC, not coerced)",
      metrics.normalize_pitch_type("knuckleball") is None)
check("blank returns None", metrics.normalize_pitch_type("  ") is None)
check("every canonical code has a label",
      all(c in metrics.PITCH_TYPE_LABELS for c in metrics.PITCH_TYPES))


print("\n4. tools")
# 9 = the original 8 plus Moeller Rapsodo (2026-08-21).
check("9 tools registered", len(tools.TOOLS) == 9, str(len(tools.TOOLS)))
check("every tool has a url and an icon",
      all(t.get("url") and t.get("icon") for t in tools.TOOLS))
check("tool keys are unique",
      len({t["key"] for t in tools.TOOLS}) == len(tools.TOOLS))


print("\n5. routes")
import app as hubapp  # noqa: E402
client = hubapp.app.test_client()

for path in ["/bg-field.jpg", "/shield.png", "/moeller-logo.png",
             "/field2.jpg", "/manifest.json", "/favicon.ico"]:
    r = client.get(path)
    check(f"{path} serves", r.status_code == 200, f"got {r.status_code}")

r = client.get("/")
check("home page renders", r.status_code == 200, f"got {r.status_code}")
body = r.get_data(as_text=True)
check("assistant widget present", 'id="caMsgs"' in body)
check("hero present", "Moeller Baseball Analytics" in body)
# Phase C moved the tool cards off the front page to /tools by design; that they
# all survived the move is checked in section 6.
check("tool cards no longer crowd the front page",
      sum(1 for t in tools.TOOLS if t["title"] in body) == 0)

r = client.get("/api/health")
check("/api/health ok", r.status_code == 200)
h = r.get_json()
check("health reports the db backend", h.get("backend") == "sqlite", str(h))
check("health counts players", h.get("players") == 1, str(h))
check("health reports whether writes are on", "writes_enabled" in h, str(h))


print("\n5b. seed-on-startup")
import seed  # noqa: E402

# Off by default outside production -- otherwise every test suite would find 25
# real players underneath its own fixtures.
check("auto-seed is off in a plain environment", seed.autoseed_enabled() is False)
res = seed.maybe_seed(engine)
check("  and does nothing when disabled", res["ran"] is False and "disabled" in res["reason"])

os.environ["RAILWAY_ENVIRONMENT"] = "production"
try:
    check("auto-seed turns on in production", seed.autoseed_enabled() is True)
    # This database already has a player, so it must decline rather than re-seed.
    res = seed.maybe_seed(engine)
    check("  but refuses to touch a database that already has players",
          res["ran"] is False and "already" in res["reason"], str(res))
    os.environ["AUTO_SEED"] = "0"
    check("  AUTO_SEED=0 overrides production", seed.autoseed_enabled() is False)
finally:
    os.environ.pop("AUTO_SEED", None)
    os.environ.pop("RAILWAY_ENVIRONMENT", None)

os.environ["AUTO_SEED"] = "1"
try:
    check("AUTO_SEED=1 forces it on locally", seed.autoseed_enabled() is True)
finally:
    os.environ.pop("AUTO_SEED", None)

# It must seed a genuinely empty database, exactly once.
import tempfile as _tf  # noqa: E402

from sqlalchemy import create_engine as _ce  # noqa: E402
fresh_path = os.path.join(_tf.mkdtemp(), "fresh.db")
fresh = _ce("sqlite:///" + fresh_path.replace("\\", "/"), future=True)
db.metadata.create_all(fresh)
os.environ["AUTO_SEED"] = "1"
try:
    first = seed.maybe_seed(fresh)
    check("an empty database gets seeded", first.get("ran") is True, str(first))
    check("  with the real roster", first.get("players", 0) >= 20, str(first))
    check("  Blast IDs linked", first.get("blast_linked", 0) > 0, str(first))
    check("  and Blast columns mapped", first.get("blast_columns", 0) > 0, str(first))
    second = seed.maybe_seed(fresh)
    check("running it again is a no-op", second["ran"] is False, str(second))
    from sqlalchemy import func as _f  # noqa: E402
    from sqlalchemy import select as _s  # noqa: E402
    with fresh.connect() as c:
        n = c.execute(_s(_f.count()).select_from(db.players)).scalar()
    check("  and did not duplicate anyone", n == first["players"], f"{n}")
finally:
    os.environ.pop("AUTO_SEED", None)
    fresh.dispose()

r = client.post("/api/git-push")
check("git-push disabled while the gate is off", r.status_code == 403,
      f"got {r.status_code}")

r = client.get("/login")
check("/login redirects home when no gate is set", r.status_code == 302)


print("\n6. player pages")
import profiles  # noqa: E402

# A player with no data at all must still render -- the empty states are the
# point, not a fallback.
with engine.begin() as conn:
    conn.execute(insert(db.players).values(
        slug="empty-player", first_name="Empty", last_name="Player"))

for path in ["/players", "/team", "/prep", "/video", "/tools", "/collect"]:
    r = client.get(path)
    check(f"{path} renders", r.status_code == 200, f"got {r.status_code}")

r = client.get("/players/empty-player")
check("a player with no data still renders", r.status_code == 200)
body = r.get_data(as_text=True)
check("  empty state instead of fake numbers",
      "No changes detected yet" in body and "No Blast, HitTrax or Rapsodo" in body)
check("  video block is marked not-wired-up, not faked",
      "Not wired up yet" in body)

r = client.get("/players/does-not-exist")
check("unknown player 404s", r.status_code == 404, f"got {r.status_code}")

# Nav is present on every page and marks the current section.
r = client.get("/team")
body = r.get_data(as_text=True)
for item in ["Players", "Team Dev", "Game Prep", "Video", "Applications"]:
    check(f"  nav item present: {item}", f">{item}<" in body)
# Sidebar shell (2026-08-23): links are .nlink with the class before the href.
check("  current section marked active", 'class="nlink active" href="/team"' in body)

# Data Collection was taken off the nav (Ian, 2026-08-19): the roster now comes
# from the school's roster pages, so coaches have no reason to land there. The
# route still works for whoever needs the name-review queue.
check("  Data Collection is off the nav", ">Data Collection<" not in body)
check("  /collect still reachable directly", client.get("/collect").status_code == 200)

# The tool cards moved off the front page but none were lost.
tools_body = client.get("/tools").get_data(as_text=True)
for t in tools.TOOLS:
    check(f"  /tools still has: {t['title']}", t["title"] in tools_body)
home_body = client.get("/").get_data(as_text=True)
check("home page leads with player search", 'id="pSearch"' in home_body)
check("home page keeps the assistant", 'id="caMsgs"' in home_body)

# Category routing must not silently drop a tool.
categorised = sum(len(tools.by_category(c))
                  for c in {t.get("category") for t in tools.TOOLS})
check("every tool has a nav category", categorised == len(tools.TOOLS),
      f"{categorised} of {len(tools.TOOLS)}")

r = client.get("/api/players/1/metric/bat_speed")
check("/api/players/<id>/metric/<key> ok", r.status_code == 200)
r = client.get("/api/players/1/metric/not_a_metric")
check("unknown metric 404s", r.status_code == 404, f"got {r.status_code}")
r = client.get("/api/players/99999/timeline")
check("timeline for a missing player 404s", r.status_code == 404)

ov = profiles.team_overview(engine)
check("team overview counts players", ov["counts"]["players"] >= 1, str(ov["counts"]))
check("players with no data are listed as needing attention",
      any(p["slug"] == "empty-player" for p in ov["no_data"]))


print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed\n")
