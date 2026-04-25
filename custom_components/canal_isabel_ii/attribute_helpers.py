"""Pure helpers for entity attribute computation.

The sensor entities expose a handful of aggregate attributes derived
from the cached hourly readings (``consumption_today_l``,
``consumption_last_7d_l``, ``data_age_minutes``...). The math is
trivial but the timezone gymnastics around "today in local civil time"
vs "rolling 7d window in UTC" is the kind of code that breaks silently
across DST transitions if it isn't pinned by tests.

These helpers are extracted so they can be exercised without spinning
up HomeAssistant's dt_util or zoneinfo machinery: callers normalise
timestamps to timezone-aware UTC up-front and pass in the local
timezone explicitly.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import NamedTuple


class TimedReading(NamedTuple):
    """Minimal shape this module needs from a Reading.

    Decoupled from the richer ``models.Reading`` so tests don't have to
    drag its other fields just to verify a sum.
    """

    timestamp: datetime
    liters: float


def sum_for_local_day(
    rows: Iterable[TimedReading],
    *,
    now: datetime,
    local_tz: tzinfo,
    days_back: int = 0,
) -> float | None:
    """Sum liters for one civil-time day at ``local_tz``.

    ``days_back=0`` means "today (local)", ``days_back=1`` means
    "yesterday (local)". The window is the half-open interval
    ``[start_of_day_local, start_of_day_local + 24h)`` mapped back to
    UTC for the comparison so DST transitions stay correct (the day is
    23h or 25h on switch days; we want the civil-day length, not 24h).

    Returns ``None`` if the input is empty so the caller can omit the
    attribute rather than reporting a misleading ``0.0`` (distinguishes
    "no data yet" from "zero consumption today").
    """
    rows_list = list(rows)
    if not rows_list:
        return None

    now_local = now.astimezone(local_tz)
    day_anchor_local = (now_local - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    next_day_local = (day_anchor_local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = day_anchor_local.astimezone(UTC)
    end_utc = next_day_local.astimezone(UTC)

    total = 0.0
    matched = False
    for r in rows_list:
        ts = _ensure_utc(r.timestamp, local_tz)
        if start_utc <= ts < end_utc:
            total += r.liters
            matched = True
    return total if matched else 0.0


def sum_for_rolling_window(
    rows: Iterable[TimedReading],
    *,
    now: datetime,
    days: int,
) -> float | None:
    """Sum liters for the last ``days`` * 24h ending at ``now``.

    Rolling window means "from 30 days ago at this exact second until
    now". Different from ``sum_for_local_day(days_back=30)`` which is
    civil-day-aligned. Returns ``None`` for an empty input — see the
    rationale in ``sum_for_local_day``.
    """
    rows_list = list(rows)
    if not rows_list:
        return None

    cutoff_utc = now.astimezone(UTC) - timedelta(days=days)
    now_utc = now.astimezone(UTC)
    total = 0.0
    matched = False
    for r in rows_list:
        ts = _ensure_utc(r.timestamp, UTC)
        if cutoff_utc <= ts <= now_utc:
            total += r.liters
            matched = True
    return total if matched else 0.0


def sum_for_local_bimonth(
    rows: Iterable[TimedReading],
    *,
    bimonth_start: date,
    bimonth_end: date,
    local_tz: tzinfo,
) -> float:
    """Sum liters whose **local civil date** falls in
    ``[bimonth_start, bimonth_end)``.

    Why this is its own helper (Bug 2.3, v0.5.14):
    The earlier inline implementation in ``sensor._bimonth_consumo_m3``
    called ``r.timestamp.date()`` directly. If the reading was an
    aware-UTC timestamp, ``.date()`` returned the UTC date, NOT the
    local civil date — so on the first/last hours of a bimonth boundary
    (e.g. 2026-01-01 00:30 Madrid local = 2025-12-31 23:30 UTC) the
    same reading could be counted in the wrong bimonth, breaking the
    current-block / current-price sensors for a few hours every two
    months. Doing the conversion here, with the same ``_ensure_utc``
    style fallback for naive timestamps, keeps the math civil-time-
    aligned regardless of how the producer chose to encode the
    timestamp.

    Returns liters (not m³) so the caller decides on unit conversion.
    Returns ``0.0`` for an empty input — unlike ``sum_for_local_day``,
    the bimonth helper only feeds derived values (block / price) and
    those want a deterministic zero, not a ``None`` to silently disable
    them on first install.
    """
    total = 0.0
    for r in rows:
        ts = r.timestamp
        ts_local = ts.replace(tzinfo=local_tz) if ts.tzinfo is None else ts.astimezone(local_tz)
        if bimonth_start <= ts_local.date() < bimonth_end:
            total += r.liters
    return total


def data_age_minutes(
    last_reading_at: datetime | None,
    *,
    now: datetime,
) -> int | None:
    """Minutes between ``now`` and the most recent reading.

    Useful for templates / alerts ("notify if no fresh data in 90 min").
    Returns ``None`` if there's no last reading yet; never returns a
    negative value (a clock skew that makes ``last > now`` is clamped
    to 0 — the caller is more interested in "stale: yes/no" than in
    the absolute number).
    """
    if last_reading_at is None:
        return None
    last_utc = _ensure_utc(last_reading_at, UTC)
    delta = now.astimezone(UTC) - last_utc
    seconds = max(0.0, delta.total_seconds())
    return int(seconds // 60)


def _ensure_utc(ts: datetime, fallback_tz: tzinfo) -> datetime:
    """Return ``ts`` as timezone-aware UTC.

    Naive timestamps are assumed to be in ``fallback_tz`` — the CSV
    payload normally carries tz info, but defensively we treat naive
    timestamps as local-civil-time so a misparsed row can't yield
    off-by-hours sums silently.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=fallback_tz)
    return ts.astimezone(UTC)
