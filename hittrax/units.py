"""HitTrax unit conversions, applied before ingest.

PlaysExport measures ``Velo`` in metres per second and ``Dist`` in metres.
``Elv`` is already degrees. ``ingest.save_mappings`` always writes
``column_maps.scale = 1.0`` and there is no other place to put a factor, so a
scale on the HitTrax map would either do nothing or, if it were ever honoured,
convert twice. The conversion therefore happens here, while the collect-ready
CSV is built, and the stored scale stays 1.0.

Factors are the exact international mile / international yard values used in
the phase-1 proof (import #200), not a rounded 2.237 or 3.281.

    Velo m/s → mph × 2.2369362921
    Dist m   → ft  × 3.280839895
    Elv      → degrees, unchanged
"""

from __future__ import annotations

import re

# International mile: 1 m/s = 2.2369362920544... mph. Phase-1 locked ten digits.
MS_TO_MPH = 2.2369362921
# International yard: 1 m = 3.2808398950131... ft. Phase-1 locked nine digits.
M_TO_FT = 3.280839895

_MISSING = {"", "-", "--", "n/a", "na", "null", "none", "nan"}


def _to_float(value):
    """'86.4' → 86.4, '' / 'N/A' → None. No unit suffix is expected pre-conversion."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in _MISSING:
        return None
    match = re.fullmatch(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group())


def ms_to_mph(value):
    """Plays.Velo, metres per second, as miles per hour. None if blank."""
    number = _to_float(value)
    if number is None:
        return None
    return number * MS_TO_MPH


def m_to_ft(value):
    """Plays.Dist, metres, as feet. None if blank."""
    number = _to_float(value)
    if number is None:
        return None
    return number * M_TO_FT


def as_degrees(value):
    """Plays.Elv is already degrees. Do not scale it."""
    return _to_float(value)


def format_measure(value):
    """Four decimal places, matching the phase-1 collect-ready CSV.

    Empty string for a missing measure so the column is still present and
    ingest drops that one value instead of the row.
    """
    if value is None:
        return ""
    return f"{float(value):.4f}"
