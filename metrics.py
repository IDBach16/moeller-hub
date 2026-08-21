"""
metrics.py -- the metric registry. See PLAYER_DEV_SPEC.md section 6.

Single source of truth for how every metric behaves: what it means, which way is
better, how big a move has to be before it counts as a change, and how many
observations a window needs before we trust it. Nothing downstream hard-codes a
threshold -- changes.py reads all of it from here.

IMPORTANT -- every threshold in this file is a PLACEHOLDER until we have a season
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
           mmc=0.8, min_n=15, sources=("rapsodo", "charting", "awre"), headline=True),
    Metric("velocity", "Velocity", "mph", "pitching", HIGHER_BETTER,
           mmc=0.8, min_n=15, sources=("rapsodo", "charting", "awre")),
    Metric("spin_rate", "Spin rate", "rpm", "pitching", HIGHER_BETTER,
           mmc=100, min_n=15, sources=("rapsodo",), headline=True, decimals=0),
    Metric("induced_vertical_break", "Induced vertical break", "in", "pitching", NEUTRAL,
           mmc=1.0, min_n=15, sources=("rapsodo",), headline=True),
    Metric("horizontal_break", "Horizontal break", "in", "pitching", NEUTRAL,
           mmc=1.0, min_n=15, sources=("rapsodo",), headline=True),
    Metric("spin_efficiency", "Spin efficiency", "%", "pitching", HIGHER_BETTER,
           mmc=5.0, min_n=15, sources=("rapsodo",)),
    Metric("release_height", "Release height", "ft", "pitching", NEUTRAL,
           mmc=0.15, min_n=15, sources=("rapsodo",), decimals=2),
    Metric("release_side", "Release side", "ft", "pitching", NEUTRAL,
           mmc=0.15, min_n=15, sources=("rapsodo",), decimals=2),
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
# The two target bands are the placeholders most likely to be wrong; calibrate
# them on our own hitters before anyone reads them as gospel.

_HITTING = [
    Metric("bat_speed", "Bat speed", "mph", "hitting", HIGHER_BETTER,
           mmc=1.5, min_n=20, sources=("blast",), headline=True),
    Metric("peak_hand_speed", "Peak hand speed", "mph", "hitting", HIGHER_BETTER,
           mmc=1.0, min_n=20, sources=("blast",)),
    Metric("attack_angle", "Attack angle", "deg", "hitting", TARGET_BAND,
           mmc=2.0, min_n=20, sources=("blast",), target_band=(5.0, 15.0), headline=True),
    Metric("vertical_bat_angle", "Vertical bat angle", "deg", "hitting", TARGET_BAND,
           mmc=2.0, min_n=20, sources=("blast",), target_band=(-40.0, -25.0)),
    Metric("on_plane_efficiency", "On-plane efficiency", "%", "hitting", HIGHER_BETTER,
           mmc=5.0, min_n=20, sources=("blast",), headline=True),
    Metric("rotational_acceleration", "Rotational acceleration", "g", "hitting", HIGHER_BETTER,
           mmc=1.0, min_n=20, sources=("blast",)),
    Metric("early_connection", "Early connection", "deg", "hitting", TARGET_BAND,
           mmc=3.0, min_n=20, sources=("blast",), target_band=(80.0, 100.0)),
    Metric("connection_at_impact", "Connection at impact", "deg", "hitting", TARGET_BAND,
           mmc=3.0, min_n=20, sources=("blast",), target_band=(80.0, 100.0)),
    Metric("body_rotation", "Body rotation", "%", "hitting", NEUTRAL,
           mmc=5.0, min_n=20, sources=("blast",)),
    Metric("body_tilt", "Body tilt", "deg", "hitting", NEUTRAL,
           mmc=3.0, min_n=20, sources=("blast",)),
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
