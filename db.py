"""
db.py -- schema and connection for the Moeller player-development system.

See PLAYER_DEV_SPEC.md section 5 for why each table looks the way it does.

One engine, driven by DATABASE_URL -- the same arrangement as Charting_App/db.py:

    DATABASE_URL unset      -> sqlite:///playerdev.db   (local dev, no Postgres needed)
    DATABASE_URL set        -> that database            (Railway Postgres in production)

Railway hands out `postgres://...` on some plugin versions and SQLAlchemy only accepts
`postgresql://`, so we rewrite it. Everything is declared through SQLAlchemy Core so the
same DDL runs on both backends -- no SERIAL-vs-AUTOINCREMENT branching.

Why Postgres and not a SQLite file in the repo: Railway's filesystem is ephemeral. This
database holds goals and interventions a coach typed in by hand; a redeploy would take
them with it.
"""

import os

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, Float,
                        ForeignKey, Index, Integer, MetaData, String, Table,
                        Text, UniqueConstraint, create_engine, func)

metadata = MetaData()


# ===========================================================================
# Identity -- the Moeller Player ID  (spec section 4)
# ===========================================================================

# players.id IS the Moeller Player ID. Never reused, never changed. Everything
# else in this file joins to it.
players = Table(
    "players", metadata,
    Column("id", Integer, primary_key=True),
    Column("slug", String(80), nullable=False, unique=True),
    Column("first_name", String(60), nullable=False),
    Column("last_name", String(60), nullable=False),
    Column("class_year", String(10)),
    Column("primary_pos", String(12)),
    Column("bats", String(1)),          # R / L / S
    Column("throws", String(1)),        # R / L
    Column("is_pitcher", Boolean, server_default="0"),
    Column("is_active", Boolean, server_default="1"),
    Column("created_at", DateTime, server_default=func.now()),
)

# Every spelling a player appears under, per source. AWRE writes "Ponatoski, Matt";
# GCL writes "Matt Ponatoski". Ingest resolves by alias lookup, never by guessing.
player_aliases = Table(
    "player_aliases", metadata,
    Column("id", Integer, primary_key=True),
    Column("player_id", Integer, ForeignKey("players.id"), nullable=False),
    Column("source", String(20), nullable=False),   # awre|gcl|charting|blast|hittrax|rapsodo
    Column("alias", String(120), nullable=False),
    UniqueConstraint("source", "alias", name="uq_alias_source"),
)

# Where a vendor has its own stable ID we store it and stop matching on names
# entirely. Blast already gives us this for 35 players -- see seed.py.
player_vendor_ids = Table(
    "player_vendor_ids", metadata,
    Column("id", Integer, primary_key=True),
    Column("player_id", Integer, ForeignKey("players.id"), nullable=False),
    Column("vendor", String(20), nullable=False),
    Column("vendor_id", String(60), nullable=False),
    UniqueConstraint("vendor", "vendor_id", name="uq_vendor_id"),
)

# Level is per SEASON, not a column on the player. A kid who was JV as a sophomore
# and varsity as a junior is exactly the progression a development system exists to
# show; a single `level` field would overwrite that history and it can't be
# reconstructed afterwards. Source of truth is the school's own roster pages
# (letsgobigmoe.com), scraped per level -- see rapsodo/scrape_roster.py.
player_seasons = Table(
    "player_seasons", metadata,
    Column("id", Integer, primary_key=True),
    Column("player_id", Integer, ForeignKey("players.id"), nullable=False),
    Column("season", Integer, nullable=False),          # 2026 = spring 2026
    Column("level", String(12), nullable=False),        # varsity | jv | freshman
    Column("jersey", String(4)),
    Column("position", String(24)),                     # as the roster lists it, e.g. "P/INF"
    Column("academic_year", String(12)),                # Freshman..Senior in that season
    Column("height_in", Integer),
    Column("is_pitcher", Boolean, server_default="0"),
    Column("active", Boolean, server_default="1"),      # false = cut / left the program
    Column("source", String(20), server_default="roster_site"),
    Column("created_at", DateTime, server_default=func.now()),
    UniqueConstraint("player_id", "season", name="uq_player_season"),
)

Index("ix_player_seasons_season_level", player_seasons.c.season, player_seasons.c.level)


# ===========================================================================
# Ingest -- layer 1: nothing uploaded is ever discarded  (spec section 5.3)
# ===========================================================================

raw_imports = Table(
    "raw_imports", metadata,
    Column("id", Integer, primary_key=True),
    Column("vendor", String(20), nullable=False),
    Column("filename", String(255)),
    Column("sha256", String(64), nullable=False, unique=True),  # same file can't count twice
    Column("uploaded_by", String(60)),
    Column("uploaded_at", DateTime, server_default=func.now()),
    Column("header", JSON),         # the detected header row, verbatim
    Column("header_row", Integer),  # which line it was on -- Rapsodo puts preamble above it
    Column("row_count", Integer),
    Column("payload", JSON),        # the rows themselves, before any parsing
    Column("status", String(20), server_default="pending"),  # pending|mapped|committed|rejected
    # Declared by the uploader, applied to every session in the file. `purpose`
    # is what makes the roadmap's protocols measurable (spec section 10) -- an
    # export can't tell us whether a bullpen was a baseline or a checkpoint.
    Column("side", String(10)),          # hitting | pitching
    Column("session_type", String(20)),
    Column("purpose", String(20)),
    Column("note", Text),
)

# Column mapping is DATA, not code. When the first HitTrax export lands, its
# headers get mapped once on the /collect page and the mapping persists -- no
# redeploy, no code change. (spec section 5.4)
column_maps = Table(
    "column_maps", metadata,
    Column("id", Integer, primary_key=True),
    Column("vendor", String(20), nullable=False),
    Column("source_column", String(120), nullable=False),
    # our canonical metric key, or one of the structural roles:
    # 'player' | 'vendor_id' | 'date' | 'session' | 'pitch_type' | 'ignore'
    Column("metric_key", String(60), nullable=False),
    Column("unit", String(20)),
    Column("scale", Float, server_default="1"),   # unit conversion, e.g. m/s -> mph
    Column("confirmed_by", String(60)),
    Column("confirmed_at", DateTime),
    UniqueConstraint("vendor", "source_column", name="uq_colmap"),
)

# Export names that did not resolve to a Moeller Player ID. Nothing is
# auto-guessed into the database; a human accepts or rejects each one.
name_review = Table(
    "name_review", metadata,
    Column("id", Integer, primary_key=True),
    Column("import_id", Integer, ForeignKey("raw_imports.id")),
    Column("vendor", String(20), nullable=False),
    Column("raw_name", String(120), nullable=False),
    Column("suggested_player_id", Integer, ForeignKey("players.id")),
    Column("suggestion_score", Float),
    Column("status", String(20), server_default="open"),   # open|accepted|rejected
    Column("resolved_by", String(60)),
    Column("resolved_at", DateTime),
)


# ===========================================================================
# Sessions -- everything measured hangs off one  (spec section 5.2)
# ===========================================================================

# The Charting App learned this the hard way: a date alone cannot tell two
# bullpens on the same day apart.
sessions = Table(
    "sessions", metadata,
    Column("id", Integer, primary_key=True),
    Column("player_id", Integer, ForeignKey("players.id"), nullable=False),
    Column("session_date", Date, nullable=False),
    Column("session_type", String(20), nullable=False),
    Column("source", String(20), nullable=False),
    Column("source_ref", String(120)),   # vendor session id / game key, for dedupe
    Column("purpose", String(20)),
    Column("notes", Text),
    Column("import_id", Integer, ForeignKey("raw_imports.id")),
    Column("created_at", DateTime, server_default=func.now()),
    # Re-uploading the same export is idempotent. Multiple NULL source_refs are
    # allowed on both backends, which is what manual session entry needs.
    UniqueConstraint("source", "source_ref", name="uq_session_source_ref"),
)

Index("ix_sessions_player_date", sessions.c.player_id, sessions.c.session_date)


# ===========================================================================
# Measurements -- long format, deliberately  (spec section 5.3)
# ===========================================================================
#
# We do not know HitTrax's or Rapsodo's column list yet. A wide table would need
# a migration for every surprise column; a long table absorbs them. metrics.py
# decides which keys are meaningful -- unknown keys are stored but not surfaced.

swings = Table(
    "swings", metadata,
    Column("id", Integer, primary_key=True),
    Column("session_id", Integer, ForeignKey("sessions.id"), nullable=False),
    Column("player_id", Integer, ForeignKey("players.id"), nullable=False),
    Column("seq", Integer),             # swing number within the session
    Column("ts", DateTime),
    Column("metric_key", String(60), nullable=False),
    Column("value", Float),
)

pitch_metrics = Table(
    "pitch_metrics", metadata,
    Column("id", Integer, primary_key=True),
    Column("session_id", Integer, ForeignKey("sessions.id"), nullable=False),
    Column("player_id", Integer, ForeignKey("players.id"), nullable=False),
    Column("seq", Integer),             # pitch number within the session
    Column("ts", DateTime),
    Column("pitch_type", String(4)),    # normalized -- see metrics.PITCH_TYPES
    Column("metric_key", String(60), nullable=False),
    Column("value", Float),
)

# The access pattern for every baseline and trend query in changes.py.
Index("ix_swings_player_metric_ts", swings.c.player_id, swings.c.metric_key, swings.c.ts)
Index("ix_swings_session", swings.c.session_id)
Index("ix_pitch_player_metric_ts",
      pitch_metrics.c.player_id, pitch_metrics.c.metric_key, pitch_metrics.c.ts)
Index("ix_pitch_session", pitch_metrics.c.session_id)


# ===========================================================================
# Development records  (spec section 5.5)
# ===========================================================================

goals = Table(
    "goals", metadata,
    Column("id", Integer, primary_key=True),
    Column("player_id", Integer, ForeignKey("players.id"), nullable=False),
    Column("metric_key", String(60)),        # a goal can be measurable or narrative
    Column("direction", String(16)),         # increase|decrease|target_band
    Column("target_value", Float),
    # Snapshot of where he was when the goal was set. Without it "60% of the way
    # there" is unanswerable -- progress needs a start, not just a target.
    Column("start_value", Float),
    Column("title", String(160), nullable=False),
    Column("detail", Text),
    Column("set_by", String(60)),
    Column("set_on", Date),
    Column("review_on", Date),
    Column("status", String(16), server_default="active"),  # active|met|abandoned|superseded
    Column("created_at", DateTime, server_default=func.now()),
)

# The roadmap's requirement -- date, player, coach, goal, intervention, review
# date -- is exactly these columns. intervention_date is what lets changes.py cut
# the data into pre/post windows automatically.
interventions = Table(
    "interventions", metadata,
    Column("id", Integer, primary_key=True),
    Column("player_id", Integer, ForeignKey("players.id"), nullable=False),
    Column("intervention_date", Date, nullable=False),
    Column("category", String(24)),          # grip|pitch_shape|bat_path|drill|approach|strength|mechanical_cue
    Column("title", String(160), nullable=False),
    Column("detail", Text),
    Column("coach", String(60)),
    Column("goal_id", Integer, ForeignKey("goals.id")),
    Column("review_on", Date),
    Column("outcome", String(16), server_default="pending"),  # pending|working|no_change|reverted
    Column("created_at", DateTime, server_default=func.now()),
)


# ===========================================================================
# Computed tables -- what the AI actually reads  (spec section 5.6)
# ===========================================================================
#
# The rule from roadmap section 9, made concrete: the LLM never sees a raw swing
# or pitch row. Everything it reads is something the database already computed.

player_baselines = Table(
    "player_baselines", metadata,
    Column("player_id", Integer, ForeignKey("players.id"), primary_key=True),
    Column("metric_key", String(60), primary_key=True),
    # '' = pooled across pitch types. Part of the key because a fastball's ride
    # and a slider's are different measurements, not two samples of one -- see
    # metrics.PITCH_SPECIFIC. Empty string rather than NULL: NULLs in a composite
    # primary key don't compare equal, so dedupe would silently stop working.
    Column("pitch_type", String(4), primary_key=True, server_default=""),
    Column("window_end", Date, primary_key=True),
    Column("window_start", Date),
    Column("n", Integer),
    Column("mean", Float),
    Column("sd", Float),
    Column("p25", Float),
    Column("p50", Float),
    Column("p75", Float),
    Column("computed_at", DateTime, server_default=func.now()),
)

change_events = Table(
    "change_events", metadata,
    Column("id", Integer, primary_key=True),
    Column("player_id", Integer, ForeignKey("players.id"), nullable=False),
    Column("metric_key", String(60), nullable=False),
    # Which pitch this is about. NULL = pooled (release point, and the hitting
    # metrics). A pitch-specific metric with no pitch type is not reportable.
    Column("pitch_type", String(4)),
    Column("detected_on", Date, nullable=False),
    Column("direction", String(8)),          # up|down
    Column("recent_mean", Float),
    Column("baseline_mean", Float),
    Column("delta", Float),
    Column("effect_size", Float),            # delta / baseline sd
    Column("p_value", Float),
    Column("severity", String(16)),          # notable|significant
    Column("favorable", Boolean),            # derived from polarity, not from sign
    Column("n_recent", Integer),
    Column("n_baseline", Integer),
    # Pre-written by the engine, e.g. "FB velo 86.4 vs 84.9 baseline (+1.5 mph)
    # over 3 sessions" -- exactly the compact context roadmap section 9 asks for.
    Column("summary", String(400)),
    Column("acknowledged", Boolean, server_default="0"),
    Column("intervention_id", Integer, ForeignKey("interventions.id")),
    Column("created_at", DateTime, server_default=func.now()),
)

Index("ix_change_player", change_events.c.player_id, change_events.c.detected_on)

# Cached AI summaries. Stored against the player's latest session id, so
# unchanged data returns cached text with no API call at all. (spec section 9.2)
ai_summaries = Table(
    "ai_summaries", metadata,
    Column("player_id", Integer, ForeignKey("players.id"), primary_key=True),
    Column("basis", String(64), primary_key=True),   # hash of latest session id + change ids
    Column("summary", Text),
    Column("model", String(40)),
    Column("created_at", DateTime, server_default=func.now()),
)


# ===========================================================================
# Vocabularies -- single source of truth, as in Charting_App/db.py
# ===========================================================================

SESSION_TYPES = ["bullpen", "live_ab", "cage", "tee", "front_toss",
                 "machine", "scrimmage", "intrasquad", "game"]

# What makes the roadmap's protocols measurable: a 'baseline' session is what
# everything else gets compared against.
SESSION_PURPOSES = ["baseline", "development", "checkpoint", "intervention", "competition"]

SOURCES = ["blast", "hittrax", "rapsodo", "charting", "awre", "manual"]

INTERVENTION_CATEGORIES = ["grip", "pitch_shape", "bat_path", "drill",
                           "approach", "strength", "mechanical_cue"]

GOAL_DIRECTIONS = ["increase", "decrease", "target_band"]

# Structural roles a source column can map to, alongside any metric key.
COLUMN_ROLES = ["player", "vendor_id", "date", "session", "pitch_type", "seq", "ignore"]


# ===========================================================================
# Engine
# ===========================================================================

_engine = None


def database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        here = os.path.dirname(os.path.abspath(__file__))
        return "sqlite:///" + os.path.join(here, "playerdev.db")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def get_engine():
    global _engine
    if _engine is None:
        url = database_url()
        kwargs = {"future": True}
        if url.startswith("postgresql"):
            # Railway idles connections out; check them before handing them over.
            kwargs.update(pool_pre_ping=True, pool_recycle=280)
        _engine = create_engine(url, **kwargs)
        metadata.create_all(_engine)
    return _engine


def is_postgres():
    return database_url().startswith("postgresql")


def slugify(first, last):
    """Stable URL handle for /players/<slug>. The integer id stays the join key."""
    import re
    s = f"{first}-{last}".lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")
