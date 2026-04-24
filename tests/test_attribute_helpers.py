"""Unit tests for ``attribute_helpers`` — entity attribute aggregations.

These pin the timezone-sensitive math behind the per-entity attributes
(``consumption_today_l``, rolling 7d/30d, ``data_age_minutes``). Most
of the surface is trivial sums; the value of the suite is freezing the
DST behaviour and the empty-vs-zero distinction.

Loader follows the same pattern as ``test_continuation_stats.py``: load
the module by file path so we don't go through
``custom_components.canal_isabel_ii.__init__`` (which pulls in
HomeAssistant).
"""

from __future__ import annotations

import importlib.util
import os as _os
import sys as _sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------


def _load_helper_module():
    repo = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src = repo / "custom_components" / "canal_isabel_ii" / "attribute_helpers.py"
    spec = importlib.util.spec_from_file_location("_canal_attr_helpers_for_test", src)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_helpers = _load_helper_module()
TimedReading = _helpers.TimedReading
sum_for_local_day = _helpers.sum_for_local_day
sum_for_rolling_window = _helpers.sum_for_rolling_window
data_age_minutes = _helpers.data_age_minutes


# Madrid civil time. We hand-build a tzinfo so the test doesn't depend
# on system zoneinfo data — fixed +01:00/+02:00 offsets per scenario.
MADRID_WINTER = timezone(timedelta(hours=1))  # CET, no DST
MADRID_SUMMER = timezone(timedelta(hours=2))  # CEST


@pytest.fixture
def enable_custom_integrations():
    """Override autouse fixture from pytest-homeassistant-custom-component."""
    yield


# =====================================================================
# sum_for_local_day
# =====================================================================


class TestSumForLocalDay:
    def test_empty_input_returns_none(self):
        assert sum_for_local_day([], now=datetime.now(UTC), local_tz=MADRID_WINTER) is None

    def test_today_collects_only_todays_rows(self):
        # Now: 2026-01-15 14:00 Madrid winter (= 13:00 UTC)
        now = datetime(2026, 1, 15, 13, 0, tzinfo=UTC)
        rows = [
            # Yesterday Madrid (2026-01-14 23:00 local = 22:00 UTC) — out
            TimedReading(datetime(2026, 1, 14, 22, 0, tzinfo=UTC), 100.0),
            # Today Madrid 00:00 local = 2026-01-14 23:00 UTC — in
            TimedReading(datetime(2026, 1, 14, 23, 0, tzinfo=UTC), 50.0),
            # Today Madrid 12:00 local = 11:00 UTC — in
            TimedReading(datetime(2026, 1, 15, 11, 0, tzinfo=UTC), 30.0),
            # Tomorrow Madrid 00:00 local = today 23:00 UTC — out
            TimedReading(datetime(2026, 1, 15, 23, 0, tzinfo=UTC), 200.0),
        ]
        assert sum_for_local_day(rows, now=now, local_tz=MADRID_WINTER) == 80.0

    def test_yesterday_collects_only_yesterdays_rows(self):
        now = datetime(2026, 1, 15, 13, 0, tzinfo=UTC)
        rows = [
            # Day before yesterday — out
            TimedReading(datetime(2026, 1, 12, 22, 0, tzinfo=UTC), 999.0),
            # Yesterday Madrid 00:00 local = 2026-01-13 23:00 UTC — in
            TimedReading(datetime(2026, 1, 13, 23, 0, tzinfo=UTC), 10.0),
            # Yesterday Madrid 23:00 local = 2026-01-14 22:00 UTC — in
            TimedReading(datetime(2026, 1, 14, 22, 0, tzinfo=UTC), 20.0),
            # Today — out
            TimedReading(datetime(2026, 1, 14, 23, 0, tzinfo=UTC), 30.0),
        ]
        assert sum_for_local_day(rows, now=now, local_tz=MADRID_WINTER, days_back=1) == 30.0

    def test_today_with_no_matching_rows_returns_zero_not_none(self):
        # Cache HAS data, just none from today — distinguishes from
        # "no data ever" which returns None.
        now = datetime(2026, 1, 15, 13, 0, tzinfo=UTC)
        rows = [
            TimedReading(datetime(2026, 1, 10, 12, 0, tzinfo=UTC), 5.0),
        ]
        assert sum_for_local_day(rows, now=now, local_tz=MADRID_WINTER) == 0.0

    def test_naive_timestamp_treated_as_local(self):
        # Provider sometimes ships naive datetimes; assume local civil
        # time so a misconfigured producer doesn't yield off-by-hours.
        now = datetime(2026, 1, 15, 13, 0, tzinfo=UTC)
        rows = [
            TimedReading(datetime(2026, 1, 15, 12, 0), 42.0),  # naive → 11:00 UTC, in
        ]
        assert sum_for_local_day(rows, now=now, local_tz=MADRID_WINTER) == 42.0

    def test_summer_offset_handled(self):
        # July: Madrid = UTC+2. Today should be the local day, not UTC.
        now = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)  # 2026-07-16 00:00 local
        rows = [
            # 2026-07-15 23:00 local = 21:00 UTC — yesterday (already past midnight local)
            TimedReading(datetime(2026, 7, 15, 21, 0, tzinfo=UTC), 99.0),
            # 2026-07-16 00:30 local = 22:30 UTC — today
            TimedReading(datetime(2026, 7, 15, 22, 30, tzinfo=UTC), 5.0),
        ]
        assert sum_for_local_day(rows, now=now, local_tz=MADRID_SUMMER) == 5.0


# =====================================================================
# sum_for_rolling_window
# =====================================================================


class TestSumForRollingWindow:
    def test_empty_returns_none(self):
        assert sum_for_rolling_window([], now=datetime.now(UTC), days=7) is None

    def test_7d_window_includes_endpoints(self):
        now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        rows = [
            TimedReading(datetime(2026, 1, 8, 12, 0, tzinfo=UTC), 7.0),  # exactly 7d ago — in
            TimedReading(datetime(2026, 1, 8, 11, 59, tzinfo=UTC), 99.0),  # just outside — out
            TimedReading(datetime(2026, 1, 12, 0, 0, tzinfo=UTC), 3.0),  # in
            TimedReading(datetime(2026, 1, 15, 12, 0, tzinfo=UTC), 1.0),  # exactly now — in
        ]
        assert sum_for_rolling_window(rows, now=now, days=7) == 11.0

    def test_30d_window_typical(self):
        now = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        rows = [
            TimedReading(datetime(2026, 2, 28, 23, 59, tzinfo=UTC), 999.0),  # >30d — out
            TimedReading(datetime(2026, 3, 5, 12, 0, tzinfo=UTC), 100.0),
            TimedReading(datetime(2026, 3, 25, 12, 0, tzinfo=UTC), 200.0),
        ]
        assert sum_for_rolling_window(rows, now=now, days=30) == 300.0

    def test_with_data_but_none_in_window_returns_zero(self):
        now = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        rows = [
            TimedReading(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 50.0),  # ancient
        ]
        assert sum_for_rolling_window(rows, now=now, days=7) == 0.0


# =====================================================================
# data_age_minutes
# =====================================================================


class TestDataAgeMinutes:
    def test_none_when_no_last(self):
        assert data_age_minutes(None, now=datetime.now(UTC)) is None

    def test_basic_minutes(self):
        now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        last = datetime(2026, 1, 15, 11, 30, tzinfo=UTC)
        assert data_age_minutes(last, now=now) == 30

    def test_naive_timestamp_treated_as_utc(self):
        now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        last = datetime(2026, 1, 15, 11, 30)  # naive
        assert data_age_minutes(last, now=now) == 30

    def test_clock_skew_clamped_to_zero(self):
        # Clock skew can put last_reading in the future — the user
        # cares about "stale or fresh", not about negative ages.
        now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        future = datetime(2026, 1, 15, 12, 5, tzinfo=UTC)
        assert data_age_minutes(future, now=now) == 0

    def test_seconds_floor_to_minutes(self):
        # 89 seconds = 1 minute, not 1.48
        now = datetime(2026, 1, 15, 12, 1, 29, tzinfo=UTC)
        last = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert data_age_minutes(last, now=now) == 1
