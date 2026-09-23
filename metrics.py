"""
metrics.py -- the metric registry. See PLAYER_DEV_SPEC.md section 6.

Single source of truth for how every metric behaves: what it means, which way is
better, how big a move has to be before it counts as a change, and how many
observations a window needs before we trust it. Nothing downstream hard-codes a
threshold -- changes.py reads all of it from here.

mmc CALIBRATED 2026-09-21 from a season of our own data (Jan-Sep 2026). Method:
for each metric, each player's session-to-session SD of session means (sessions
of >=10 reps, players with >=3 sessions), then the median across players -- the
typical wobble a coach should NOT be paged about. mmc = max(that, the old
placeholder), never lowered. Measured on 15-18 players per metric. Still
placeholders (no device data yet): extension, strike/whiff/heart/chase (charting),
exit_velocity (HitTrax), body_rotation, on_plane_pct (Blast API-only). The
original note follows for history: every threshold was a PLACEHOLDER until we had a season
of our own data. They are deliberately in one file so they are trivial to revise.
The spec's commitment is that the numbers are easy to change, not that the
starting numbers are right.
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Polarity
# ---------------------------------------------------------------------------
# Not every metric is "up good". attack_angle wants a BAND. time_to_contact is
# lower-better. The engine must not congratulate a hitter whose attack angle
# climbed from 12 to 22 degrees.

HIGHER_BETTER = "higher_better"
LOWER_BETTER = "lower_better"
TARGET_BAND = "target_band"
NEUTRAL = "neutral"


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str
    side: str                       # "pitching" | "hitting"
    polarity: str
    mmc: float                      # minimum meaningful change -- below this it's noise
    min_n: int                      # observations needed before a window counts
    sources: Tuple[str, ...]
    target_band: Optional[Tuple[float, float]] = None
    # Shown on the player profile's status tile row (spec section 8.3). Keep this
    # to five or six per side -- the registry can hold thirty.
    headline: bool = False
    decimals: int = 1

    def favorable(self, delta: float) -> Optional[bool]:
        """Is a move of `delta` good for this player? None when it isn't a value
        judgement (neutral metrics, or a band without a current value)."""
        if self.polarity == HIGHER_BETTER:
            return delta > 0
        if self.polarity == LOWER_BETTER:
            return delta < 0
        return None

    def in_band(self, value: float) -> Optional[bool]:
        if self.polarity != TARGET_BAND or self.target_band is None:
            return None
        lo, hi = self.target_band
        return lo <= value <= hi


# ---------------------------------------------------------------------------
# Pitching
# ---------------------------------------------------------------------------

_PITCHING = [
    Metric("fb_velocity", "Fastball velocity", "mph", "pitching", HIGHER_BETTER,
           mmc=1.2, min_n=15, sources=("rapsodo", "charting", "awre"), headline=True),
    Metric("velocity", "Velocity", "mph", "pitching", HIGHER_BETTER,
           mmc=1.2, min_n=15, sources=("rapsodo", "charting", "awre")),
    Metric("spin_rate", "Spin rate", "rpm", "pitching", HIGHER_BETTER,
           mmc=100, min_n=15, sources=("rapsodo",), headline=True, decimals=0),
    Metric("induced_vertical_break", "Induced vertical break", "in", "pitching", NEUTRAL,
           mmc=1.3, min_n=15, sources=("rapsodo",), headline=True),
    Metric("horizontal_break", "Horizontal break", "in", "pitching", NEUTRAL,
           mmc=1.5, min_n=15, sources=("rapsodo",), headline=True),
    Metric("spin_efficiency", "Spin efficiency", "%", "pitching", HIGHER_BETTER,
           mmc=5.0, min_n=15, sources=("rapsodo",)),
    Metric("release_height", "Release height", "ft", "pitching", NEUTRAL,
           mmc=0.15, min_n=15, sources=("rapsodo",), decimals=2),
    Metric("release_side", "Release side", "ft", "pitching", NEUTRAL,
           mmc=0.6, min_n=15, sources=("rapsodo",), decimals=2),
    Metric("extension", "Extension", "ft", "pitching", HIGHER_BETTER,
           mmc=0.2, min_n=15, sources=("rapsodo",), decimals=2),
    # Execution, from the Charting App and AWRE rather than a device.
    Metric("strike_pct", "Strike %", "%", "pitching", HIGHER_BETTER,
           mmc=5.0, min_n=30, sources=("charting", "awre"), headline=True),
    Metric("whiff_pct", "Whiff %", "%", "pitching", HIGHER_BETTER,
           mmc=5.0, min_n=25, sources=("charting", "awre"), headline=True),
    Metric("heart_pct", "Heart %", "%", "pitching", NEUTRAL,
           mmc=5.0, min_n=30, sources=("charting", "awre")),
    Metric("chase_pct", "Chase %", "%", "pitching", NEUTRAL,
           mmc=5.0, min_n=30, sources=("charting", "awre")),
]


# ---------------------------------------------------------------------------
# Hitting
# ---------------------------------------------------------------------------
#
# Blast keys come straight from the 2024 R puller -- see BLAST_COLUMNS below.
# The CSV export spells them differently again; see BLAST_CSV_COLUMNS.
#
# TARGET BANDS ARE CALIBRATED TO OUR OWN HITTERS (Ian's call, 2026-09-16), from
# the 7,198-swing Blast export: the interquartile range of PLAYER MEANS over
# tagged swings only. Player means rather than swing means so one hitter with
# 1,320 swings doesn't set the band for the team; tagged only because untagged
# swings are a mix of drills (see SWING_CONTEXTS).
#
# ** Read the caveat before quoting these to anyone. ** A band calibrated on our
# own distribution says "typical for Moeller", NOT "good". It cannot tell us the
# team is collectively short in a metric, because the middle of whatever we do is
# always in band by construction. It is a useful relative marker and a bad
# absolute one. Replace with an external benchmark when we have one worth trusting.

_HITTING = [
    Metric("bat_speed", "Bat speed", "mph", "hitting", HIGHER_BETTER,
           mmc=1.8, min_n=20, sources=("blast",), headline=True),
    Metric("peak_hand_speed", "Peak hand speed", "mph", "hitting", HIGHER_BETTER,
           mmc=1.0, min_n=20, sources=("blast",)),
    Metric("attack_angle", "Attack angle", "deg", "hitting", TARGET_BAND,
           mmc=2.5, min_n=20, sources=("blast",), target_band=(6.0, 11.0), headline=True),
    Metric("vertical_bat_angle", "Vertical bat angle", "deg", "hitting", TARGET_BAND,
           mmc=3.5, min_n=20, sources=("blast",), target_band=(-33.0, -26.0)),
    Metric("on_plane_efficiency", "On-plane efficiency", "%", "hitting", HIGHER_BETTER,
           mmc=5.0, min_n=20, sources=("blast",), headline=True),
    Metric("rotational_acceleration", "Rotational acceleration", "g", "hitting", HIGHER_BETTER,
           mmc=1.5, min_n=20, sources=("blast",)),
    Metric("early_connection", "Early connection", "deg", "hitting", TARGET_BAND,
           mmc=4.0, min_n=20, sources=("blast",), target_band=(96.0, 105.0)),
    Metric("connection_at_impact", "Connection at impact", "deg", "hitting", TARGET_BAND,
           mmc=3.0, min_n=20, sources=("blast",), target_band=(83.0, 88.0)),
    # In the CSV export but NOT in the 2024 API puller, which is why it was missing
    # from this registry until the first real export landed. Neutral polarity: we
    # have no defensible direction for it, and a target band would be inventing one.
    Metric("hinge_angle", "Hinge angle at impact", "deg", "hitting", NEUTRAL,
           mmc=5.0, min_n=20, sources=("blast",)),
    Metric("body_rotation", "Body rotation", "%", "hitting", NEUTRAL,
           mmc=5.0, min_n=20, sources=("blast",)),
    Metric("body_tilt", "Body tilt", "deg", "hitting", NEUTRAL,
           mmc=4.0, min_n=20, sources=("blast",)),
    Metric("power", "Power", "kW", "hitting", HIGHER_BETTER,
           mmc=0.3, min_n=20, sources=("blast",), decimals=2),
    Metric("time_to_contact", "Time to contact", "s", "hitting", LOWER_BETTER,
           mmc=0.01, min_n=20, sources=("blast",), decimals=3),
    Metric("commit_time", "Commit time", "s", "hitting", LOWER_BETTER,
           mmc=0.01, min_n=20, sources=("blast",), decimals=3),
    Metric("on_plane_pct", "On plane %", "%", "hitting", HIGHER_BETTER,
           mmc=5.0, min_n=20, sources=("blast",)),
    # HitTrax. Keys are ours; the export's column names are unknown until a real
    # file lands and gets mapped on /collect.
    Metric("exit_velocity", "Exit velocity", "mph", "hitting", HIGHER_BETTER,
           mmc=1.5, min_n=20, sources=("hittrax",), headline=True),
    Metric("max_exit_velocity", "Max exit velocity", "mph", "hitting", HIGHER_BETTER,
           mmc=2.0, min_n=20, sources=("hittrax",), headline=True),
    Metric("launch_angle", "Launch angle", "deg", "hitting", TARGET_BAND,
           mmc=2.0, min_n=20, sources=("hittrax",), target_band=(10.0, 25.0)),
    Metric("distance", "Distance", "ft", "hitting", HIGHER_BETTER,
           mmc=10.0, min_n=20, sources=("hittrax",), decimals=0),
    Metric("hard_hit_pct", "Hard-hit %", "%", "hitting", HIGHER_BETTER,
           mmc=5.0, min_n=25, sources=("hittrax",)),
]


REGISTRY = {m.key: m for m in (_PITCHING + _HITTING)}


# ---------------------------------------------------------------------------
# Which metrics only mean something for ONE pitch type
# ---------------------------------------------------------------------------
# Pooling a fastball's 15" of ride with a slider's 2" gives a number that moves
# whenever the pitcher's MIX moves, even though no individual pitch changed. That
# is a false alarm, not a finding -- and a costly one, because it looks exactly
# like a real decline.
#
# Observed on Seth Maybury (2026-02-24): sliders went from 7% to 28% of his work.
# Pooled, that read as "spin efficiency down 17.5 points, SIGNIFICANT" and "velocity
# down 2.7 mph". Per pitch type his fastball was flat (velo -0.1, IVB +0.3) and his
# slider efficiency had actually IMPROVED (+3.5).
#
# Release point is deliberately NOT in here: slot is a property of the delivery,
# not of a pitch, and a real slot change shows up across every pitch at once --
# which is exactly what Maybury's did (FB +1.16, SL +1.37, CH +2.04 ft).
PITCH_SPECIFIC = {
    "velocity",
    "spin_rate",
    "induced_vertical_break",
    "horizontal_break",
    "spin_efficiency",
}


def is_pitch_specific(key):
    """True if this metric must be compared within a single pitch type."""
    return key in PITCH_SPECIFIC


# ---------------------------------------------------------------------------
# Swing context -- the hitting side's pitch type
# ---------------------------------------------------------------------------
# Blast tags every swing with the drill it came from, and the drill moves the
# numbers more than any swing change will. This is the same trap PITCH_SPECIFIC
# exists for, on the other side of the ball: a metric pooled across drills moves
# whenever the hitter's DRILL MIX moves, even though his swing did not.
#
# Measured on the first real export (2026-09-16, 7,198 swings, 26 hitters), as the
# gap between a player's own per-drill means:
#
#   JJ Skeldon      bat speed   tee 53.06  vs  general practice 64.37   = 11.3 mph
#   Andy Bennett    on-plane    tee 56.7%  vs  soft toss        68.1%   = 11.4 pts
#   Shane Green     bat speed   machine 57.32 vs soft toss      61.97   =  4.7 mph
#
# bat_speed's minimum meaningful change is 1.5 mph. An 11.3 mph drill artefact is
# seven times the threshold -- it would fire as SIGNIFICANT every time a hitter's
# cage work shifted from tee to live, and the AI would then explain the decline.
#
# So: EVERY Blast metric is context-specific. This is the honest reading of the
# measurement -- of thirteen metrics, ten moved by more than their own mmc between
# drills for at least a quarter of hitters, and the three that didn't
# (time_to_contact, commit_time, connection_at_impact) are the ones where a tee
# swing has no defensible value anyway: there is no pitch to commit to.
#
# This differs from the pitching side, where release point is deliberately POOLED
# because slot is a property of the delivery rather than of a pitch. There is no
# hitting equivalent -- no Blast metric is measured independently of the drill.

SWING_CONTEXTS = ["tee", "soft_toss", "machine", "live", "practice"]

SWING_CONTEXT_LABELS = {
    "tee": "Tee",
    "soft_toss": "Soft toss",
    "machine": "Pitching machine",
    "live": "Live pitching",
    "practice": "General practice",
}

_CONTEXT_ALIASES = {
    # Left of the colon is ours; the right is every spelling the Blast export has
    # actually produced. "soft toss underhand" and "soft toss overhand" collapse
    # together: they are the same drill and splitting them would halve already-thin
    # samples for no gain. Revisit if a coach says the two differ for our hitters.
    "tee": ["tee", "off tee", "off the tee", "tee work"],
    "soft_toss": ["soft toss", "soft toss underhand", "soft toss overhand",
                  "front toss", "flips"],
    "machine": ["pitching machine", "machine", "iron mike"],
    # "live batting practice" is Rapsodo Hitting's session type, not a Blast tag;
    # the two devices share this vocabulary so a hitter's live work is one drill.
    "live": ["live pitch", "live pitching", "live at bats", "live abs", "live bp",
             "live batting practice"],
    "practice": ["general practice", "practice", "batting practice", "bp"],
}

_CONTEXT_LOOKUP = {alias: code
                   for code, aliases in _CONTEXT_ALIASES.items()
                   for alias in aliases}


def normalize_context(raw):
    """Blast's Environment Tag -> one of SWING_CONTEXTS, or None.

    None is the right answer for an untagged swing and is NOT a failure. 1,466 of
    the first export's 7,198 swings (20%) carry no tag, because tagging is a
    coach-behaviour thing in the Blast app and nobody was doing it consistently.

    An untagged swing is stored and shown, but contributes to NO context's
    baseline -- exactly as an unlabelled pitch contributes to no pitch type. We
    cannot know which drill it was, and folding it into a real one would corrupt
    that drill's baseline with a mix. The fix is charting discipline in the app,
    not a guess here.
    """
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key or key in ("nan", "none", "null"):
        return None
    if key in SWING_CONTEXTS:
        return key
    return _CONTEXT_LOOKUP.get(key)


# Which hitting metrics may not be pooled across drills. This is every Blast
# metric, deliberately -- see the measurement in the SWING_CONTEXTS note above.
# Built from the registry rather than typed out so a metric added later is
# context-split by default; opting one OUT is the decision that should require
# an edit here, because pooling is the failure mode.
CONTEXT_SPECIFIC = {m.key for m in REGISTRY.values()
                    if m.side == "hitting" and "blast" in m.sources}


def is_context_specific(key):
    """True if this metric must be compared within a single drill context."""
    return key in CONTEXT_SPECIFIC


def split_label(code):
    """Render whichever split dimension a finding carries.

    `change_events.pitch_type` and `player_baselines.pitch_type` hold a PITCH code
    for pitching rows and a SWING CONTEXT code for hitting rows -- one column, two
    vocabularies, because the two sides never share a row and a second column
    would put a NULL in every composite key. See the note on db.swings.context.
    """
    if not code:
        return None
    return (PITCH_TYPE_LABELS.get(code)
            or SWING_CONTEXT_LABELS.get(code)
            or str(code))


def get(key):
    return REGISTRY.get(key)


def known(key):
    """Unknown keys are stored by ingest but not surfaced until registered here."""
    return key in REGISTRY


def for_side(side):
    return [m for m in REGISTRY.values() if m.side == side]


def headline(side):
    return [m for m in REGISTRY.values() if m.side == side and m.headline]


def for_source(source):
    return [m for m in REGISTRY.values() if source in m.sources]


# ---------------------------------------------------------------------------
# Pitch-type normalization  (spec section 6.3)
# ---------------------------------------------------------------------------
# Rapsodo, the Charting App and AWRE all name pitches differently, and the
# roadmap's protocol section specifically asks for consistent labels. One
# canonical vocabulary, one mapping per source. Anything unmapped surfaces in
# the /collect QC list rather than being silently coerced.

PITCH_TYPES = ["FB", "SI", "CT", "SL", "CB", "CH", "SP"]

PITCH_TYPE_LABELS = {
    "FB": "Fastball", "SI": "Sinker", "CT": "Cutter", "SL": "Slider",
    "CB": "Curveball", "CH": "Changeup", "SP": "Splitter",
}

_PITCH_ALIASES = {
    # "fast ball" / "two seam fast ball" are how the AWRE season export spells them.
    # Note "breaking ball" is deliberately absent: it covers 4,074 tracked pitches
    # that could be a slider or a curveball, and there is no way to tell after the
    # fact. It resolves to None and lands in QC rather than being guessed into one.
    "FB": ["fastball", "fast ball", "four seam", "four-seam", "4-seam", "4 seam",
           "ff", "fa", "fb"],
    "SI": ["sinker", "two seam", "two-seam", "2-seam", "2 seam",
           "two seam fast ball", "two seam fastball", "ft", "si"],
    "CT": ["cutter", "cut fastball", "fc", "ct"],
    "SL": ["slider", "sweeper", "sl", "st"],
    "CB": ["curveball", "curve", "knuckle curve", "cu", "kc", "cb"],
    "CH": ["changeup", "change up", "change", "ch"],
    "SP": ["splitter", "split finger", "split-finger", "fs", "sp"],
}

_PITCH_LOOKUP = {alias: code
                 for code, aliases in _PITCH_ALIASES.items()
                 for alias in aliases}


def normalize_pitch_type(raw):
    """Returns a canonical code, or None if it needs a human. None is not a
    failure -- it's a row on the QC list."""
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key:
        return None
    if key.upper() in PITCH_TYPES:
        return key.upper()
    return _PITCH_LOOKUP.get(key)


# ---------------------------------------------------------------------------
# Blast column map  (spec section 5.4)
# ---------------------------------------------------------------------------
# Blast ships pre-seeded because we already know its schema, recovered from
# 2025/Moller Misc/Blast_data_moeller3.0.R. HitTrax and Rapsodo have no entries
# here on purpose -- their headers get mapped on /collect when a real export
# arrives, which is the whole point of column_maps being a table.

BLAST_COLUMNS = {
    "swing_speed.value":             ("bat_speed", "mph"),
    "peak_hand_speed.value":         ("peak_hand_speed", "mph"),
    "bat_path_angle.value":          ("attack_angle", "deg"),
    "vertical_bat_angle.value":      ("vertical_bat_angle", "deg"),
    "planar_efficiency.value":       ("on_plane_efficiency", "%"),
    "rotational_acceleration.value": ("rotational_acceleration", "g"),
    "early_connection.value":        ("early_connection", "deg"),
    "connection.value":              ("connection_at_impact", "deg"),
    "body_rotation.value":           ("body_rotation", "%"),
    "body_tilt_angle.value":         ("body_tilt", "deg"),
    "power.value":                   ("power", "kW"),
    "time_to_contact.value":         ("time_to_contact", "s"),
    "commit_time.value":             ("commit_time", "s"),
    "on_plane.value":                ("on_plane_pct", "%"),
    # structural roles
    "created_at.date":               ("date", None),
    "player_id":                     ("vendor_id", None),
    "player_name":                   ("player", None),
}


# ---------------------------------------------------------------------------
# Blast CSV export column map
# ---------------------------------------------------------------------------
# BLAST_COLUMNS above is the API schema, recovered from the 2024 R puller. The
# "All swings by player" CSV export from Blast Connect is a DIFFERENT schema with
# different names, and mapping one file with the other's keys silently produces an
# import where every metric column is unmapped and the commit writes nothing.
#
# Three things in here are load-bearing:
#
#  1. ** On Plane Efficiency ships as a FRACTION despite the (%) in its header. **
#     Values run 0.29-1.00, not 29-100. Its scale is 100. Without that, on-plane
#     efficiency enters the database around 0.7, its mmc of 5.0 is never cleared
#     by anything, and the metric silently never fires a finding for anyone. It
#     looks like "nothing changed", which is the worst possible failure mode --
#     an empty change list is a legitimate result, so nothing looks wrong.
#
#  2. The export splits the name over first_name + last_name, so there is no one
#     "player" column. Hence the player_first / player_last roles -- see
#     db.COLUMN_ROLES.
#
#  3. Hinge Angle at Impact exists here and NOT in the API puller; body_rotation
#     and on_plane_pct are the reverse. Both registries are right about their own
#     source. Do not "reconcile" them.
#
# A trailing scale of None means 1.0. The third slot is the structural role or
# None for an ordinary metric.

BLAST_CSV_COLUMNS = {
    # structural roles
    "Swing Timestamp":                  ("date", None, 1.0),
    "user_id":                          ("vendor_id", None, 1.0),
    "first_name":                       ("player_first", None, 1.0),
    "last_name":                        ("player_last", None, 1.0),
    "Environment Tag":                  ("context", None, 1.0),
    "captureid":                        ("ignore", None, 1.0),
    "timezone":                         ("ignore", None, 1.0),
    "Bat Nickname":                     ("ignore", None, 1.0),
    "sensorserialnumber":               ("ignore", None, 1.0),
    "Upload Timestamp":                 ("ignore", None, 1.0),
    "actiontype":                       ("ignore", None, 1.0),
    # metrics
    "Bat Speed (MPH)":                  ("bat_speed", "mph", 1.0),
    "Peak Hand Speed (MPH)":            ("peak_hand_speed", "mph", 1.0),
    "Rotational Acceleration (G's)":    ("rotational_acceleration", "g", 1.0),
    "Power (kW)":                       ("power", "kW", 1.0),
    # the fraction -> percent conversion described above
    "On Plane Efficiency (%)":          ("on_plane_efficiency", "%", 100.0),
    "Attack Angle (°'s)":             ("attack_angle", "deg", 1.0),
    "Vert. Bat Angle (°'s)":          ("vertical_bat_angle", "deg", 1.0),
    "Time to Contact (s)":              ("time_to_contact", "s", 1.0),
    "Commit Time (s)":                  ("commit_time", "s", 1.0),
    "Early Connection (°'s)":         ("early_connection", "deg", 1.0),
    "Hinge Angle at Impact (°'s)":    ("hinge_angle", "deg", 1.0),
    "Connection at Impact (°'s)":     ("connection_at_impact", "deg", 1.0),
    "Body Tilt Angle (°'s)":          ("body_tilt", "deg", 1.0),
}

# Only swings. The export also carries 'air Swing' rows (22 of 7,198) -- a sensor
# reading with no ball, which Blast itself excludes from a player's averages.
BLAST_ACTION_TYPES = {"swing"}


# ---------------------------------------------------------------------------
# Change-detection constants  (spec section 7)
# ---------------------------------------------------------------------------
# Read by changes.py. Here rather than there so every tunable number in the
# system lives in one file.

RECENT_SESSIONS = 3          # k: the recent window is the last k sessions
BASELINE_DAYS = 120          # baseline window length, ending where recent starts
MIN_EFFECT_SIZE = 0.5        # delta / baseline sd -- bigger than the player's own noise
MAX_P_VALUE = 0.10           # a coach's attention queue, not a paper
SIGNIFICANT_EFFECT = 0.8     # promotes 'notable' -> 'significant'
SIGNIFICANT_P = 0.05


def format_value(key, value):
    """Consistent rendering wherever a metric is shown or summarized."""
    if value is None:
        return "--"
    m = REGISTRY.get(key)
    if m is None:
        return str(round(float(value), 2))
    return f"{float(value):.{m.decimals}f}"


# ---------------------------------------------------------------------------
# Blast's own published benchmarks  ("How Do You Measure Up?")
# ---------------------------------------------------------------------------
# Transcribed 2026-09-16 from https://blastmotion.com/products/baseball/ -- the
# vendor's own table, NOT anything derived from our data. This is the second,
# clearly-labelled pool the percentile note asks for: `percentiles.BLAST_STRIP`
# ranks a hitter against his TEAMMATES, and this says where he sits against the
# level Blast says he should be hitting. Different questions, both worth asking,
# never merged into one number.
#
# THREE THINGS TO KNOW BEFORE TRUSTING A VERDICT FROM THIS TABLE:
#
#  1. ** The JV bat-speed cell renders as "55-56 MPH" on the page. ** That is not
#     a credible one-mph band. JV duplicates the "Amateur All Levels" column
#     EXACTLY for hand speed (17-21) and power (2.17-3.45), so that column's
#     bat speed (55-65) is used here. Flagged as provisional on the page rather
#     than presented as read.
#
#  2. ** Blast publishes different numbers in different places. ** Their own blog
#     gives varsity 57-71 and JV 53-67 against this page's 60-70. We use the
#     product-page table throughout so at least one source is applied
#     consistently. blastconnect.com now redirects to WIN Reality -- Blast has
#     been absorbed, which is the likely reason the two disagree, and a reason to
#     re-read this table before the spring.
#
#  3. ** Two of these bands pass everybody. ** Attack angle (0-15 deg) and
#     vertical bat angle (-10 to -40 deg) are wide enough that all 19 of our
#     measured hitters clear them. `wide=True` marks those so the page can say
#     the band is uninformative rather than let a coach read 19/19 as good news.
#
# Blast's "Rotation Score" is on their table and absent here: it is a Blast
# composite the CSV export does not carry, and we will not approximate it with
# rotational acceleration, which is a different measurement.

BLAST_LEVELS = {
    "varsity": "High School (Varsity)",
    "jv": "High School (JV)",
    # Blast has no freshman column. A high-school freshman is a JV-level hitter,
    # not the "Middle School" one -- that column is for younger players.
    "freshman": "High School (JV)",
}

# metric key -> {level: (lo, hi)}. Level "*" applies to everyone.
BLAST_BENCHMARKS = {
    "bat_speed":          {"varsity": (60.0, 70.0), "jv": (55.0, 65.0)},
    "peak_hand_speed":    {"varsity": (19.0, 23.0), "jv": (17.0, 21.0)},
    "power":              {"varsity": (2.81, 4.09), "jv": (2.17, 3.45)},
    "time_to_contact":    {"varsity": (0.15, 0.18), "jv": (0.15, 0.20)},
    "attack_angle":       {"varsity": (2.0, 15.0),  "jv": (0.0, 15.0)},
    "vertical_bat_angle": {"*": (-40.0, -10.0)},
    "on_plane_efficiency": {"*": (65.0, 85.0)},
    "early_connection":   {"*": (80.0, 105.0)},
    "connection_at_impact": {"*": (80.0, 95.0)},
}

# Bands so wide that every hitter clears them -- see note 3 above.
BLAST_WIDE = {"attack_angle", "vertical_bat_angle"}

# Blast's own stated ideal inside the band, where the page gives one. Shown as a
# marker, never as a pass/fail: missing the ideal while inside the band is not a
# finding.
BLAST_IDEAL = {
    "on_plane_efficiency": 70.0,     # "70% or higher"
    "early_connection": 90.0,        # "90 degrees (perpendicular)"
    "connection_at_impact": 90.0,
}

# The JV bat-speed substitution from note 1. Surfaced so the page can mark it.
BLAST_PROVISIONAL = {("bat_speed", "jv"), ("bat_speed", "freshman")}


def blast_band(metric_key, level):
    """(lo, hi) from Blast's published table for this metric at this level.

    Returns None where Blast publishes no band for the metric. `level` is ours
    (varsity / jv / freshman); anything unrecognised is treated as JV, which is
    the more forgiving band -- we would rather understate a shortfall than invent
    one against a player whose level we do not actually know.
    """
    table = BLAST_BENCHMARKS.get(metric_key)
    if not table:
        return None
    if "*" in table:
        return table["*"]
    key = level if level in table else ("varsity" if level == "varsity" else "jv")
    return table.get(key)
