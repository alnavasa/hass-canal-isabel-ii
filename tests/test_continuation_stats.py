"""Unit tests for ``continuation_stats`` — the spike-immune statistics helper.

This is the keystone of the Energy-dashboard correctness story (the
2025-10-25 spike incident — see the docstring of ``continuation_stats``
and the longer discussion in ``_push_statistics`` of ``sensor.py``).
The tests below freeze the contract so any future "let's just recompute
running from zero" regression fires red immediately.

We import the helper directly from the file (no full HA test rig
needed) following the same standalone-loader pattern as
``test_meter_summary_parser.py``.
"""

from __future__ import annotations

import importlib.util
import os as _os
import sys as _sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------
# Module loader (sidestep custom_components.canal_isabel_ii.__init__)
# ---------------------------------------------------------------------


def _load_helper_module():
    repo = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src = repo / "custom_components" / "canal_isabel_ii" / "statistics_helpers.py"
    spec = importlib.util.spec_from_file_location("_canal_stats_helpers_for_test", src)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_helpers = _load_helper_module()
continuation_stats = _helpers.continuation_stats


# Same conftest.py override trick as in test_meter_summary_parser.py:
# the autouse fixture from pytest-homeassistant-custom-component isn't
# installed in the lite test environment, so we no-op it locally.
@pytest.fixture
def enable_custom_integrations():
    yield


# ---------------------------------------------------------------------
# Helpers for compact, readable test bodies
# ---------------------------------------------------------------------


_BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _hour(h: int) -> datetime:
    """Build a UTC timestamp ``h`` hours after a fixed base datetime.

    Pinned to a fixed base so test failures are easy to read; the
    actual date is irrelevant to the algorithm under test. ``h`` may
    exceed 23 — we just add a timedelta — so callers can index a
    multi-day window without contortions.
    """
    return _BASE + timedelta(hours=h)


# ---------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------


def test_empty_input_returns_empty():
    assert continuation_stats([]) == []


def test_empty_input_with_anchor_returns_empty():
    assert continuation_stats([], last_sum=12345.0, last_start=_hour(5)) == []


# ---------------------------------------------------------------------
# Cold start (no previous statistics in HA)
# ---------------------------------------------------------------------


def test_cold_start_runs_from_zero():
    """First push ever: no anchor, sum starts at 0 and accumulates."""
    items = [(_hour(0), 100.0), (_hour(1), 50.0), (_hour(2), 25.0)]
    out = continuation_stats(items)
    assert out == [
        (_hour(0), 100.0),
        (_hour(1), 150.0),
        (_hour(2), 175.0),
    ]


def test_unsorted_input_is_sorted_internally():
    """Coordinator output isn't guaranteed in chronological order;
    the helper sorts so callers can pass raw rows."""
    items = [(_hour(2), 25.0), (_hour(0), 100.0), (_hour(1), 50.0)]
    out = continuation_stats(items)
    assert [ts for ts, _ in out] == [_hour(0), _hour(1), _hour(2)]
    assert out[-1][1] == 175.0


# ---------------------------------------------------------------------
# The core spike-immune contract
# ---------------------------------------------------------------------


def test_re_pushing_same_items_is_a_no_op():
    """Coordinator ticks every N minutes. The same hourly readings
    are present run after run — pushing them must not duplicate or
    rewrite anything."""
    items = [(_hour(0), 100.0), (_hour(1), 50.0)]

    first = continuation_stats(items)  # cold start
    assert first == [(_hour(0), 100.0), (_hour(1), 150.0)]

    # Second push: HA's recorder now has last_sum=150 at last_start=hour(1).
    last_start = first[-1][0]
    last_sum = first[-1][1]
    second = continuation_stats(items, last_sum=last_sum, last_start=last_start)
    assert second == []


def test_appends_only_new_hours_continuing_from_last_sum():
    """Cache grew by one new hour — emit just that one slot, with
    its sum continuing the previous running total."""
    items = [(_hour(0), 100.0), (_hour(1), 50.0), (_hour(2), 25.0)]
    out = continuation_stats(items, last_sum=150.0, last_start=_hour(1))
    assert out == [(_hour(2), 175.0)]


def test_cache_shrunk_to_subset_of_already_imported_emits_nothing():
    """The 2025-10-25 trap. Local cache wiped → next push only sees
    a subset of what HA already has. We must NOT recompute from zero
    and overwrite. We must emit nothing."""
    # HA recorder already has hours 0-23 with last_sum=24000 at hour(23).
    # Cache wipe leaves the next push with only the most recent ~3 rows.
    items = [(_hour(21), 50.0), (_hour(22), 60.0), (_hour(23), 70.0)]
    out = continuation_stats(items, last_sum=24000.0, last_start=_hour(23))
    assert out == [], (
        "Shrunken cache pushed nothing new — must not rewrite stored slots "
        "(would draw a giant negative bar in the Energy dashboard)"
    )


def test_cache_shrunk_then_recovers_with_new_hours():
    """After a cache wipe, the next bookmarklet POST re-seeds the
    cache and a few hours later has new readings past the previous
    last_start. Only the truly new hours should be emitted, with
    running starting from last_sum so the join is seamless (no
    negative bar, no jump backwards)."""
    # HA already has hour 23 with sum 24000. Cache then sees hours 22-25
    # (22 and 23 are old; 24 and 25 are new).
    items = [
        (_hour(22), 60.0),
        (_hour(23), 70.0),  # already imported
        (_hour(24), 80.0),  # NEW
        (_hour(25), 90.0),  # NEW
    ]
    out = continuation_stats(items, last_sum=24000.0, last_start=_hour(23))
    assert out == [
        (_hour(24), 24080.0),
        (_hour(25), 24170.0),
    ]


def test_boundary_equal_to_last_start_is_skipped():
    """``ts <= last_start`` is the rule. The exact boundary slot is
    *already in HA*, so it must be skipped — re-emitting it would
    overwrite with a possibly-different running sum."""
    items = [(_hour(10), 999.0)]
    out = continuation_stats(items, last_sum=5000.0, last_start=_hour(10))
    assert out == []


def test_boundary_one_second_after_last_start_is_emitted():
    """One tick past the boundary is genuinely new — emit it."""
    just_after = _hour(10) + timedelta(seconds=1)
    items = [(just_after, 42.0)]
    out = continuation_stats(items, last_sum=5000.0, last_start=_hour(10))
    assert out == [(just_after, 5042.0)]


# ---------------------------------------------------------------------
# Running sum continuity & monotonicity
# ---------------------------------------------------------------------


def test_running_sum_is_non_decreasing_in_output():
    """Liters are >= 0 by definition (water doesn't run backwards out
    of the meter), so the emitted running sum must be non-decreasing
    within a single push."""
    items = [(_hour(h), 10.0) for h in range(24)]

    # With anchor — running starts above zero and grows monotonically.
    anchored = continuation_stats(items, last_sum=1000.0)
    anchored_sums = [s for _, s in anchored]
    assert anchored_sums == sorted(anchored_sums)
    assert anchored_sums[0] == 1010.0
    assert anchored_sums[-1] == 1240.0

    # Without anchor — same monotonicity, starting from 0.
    cold = continuation_stats(items)
    cold_sums = [s for _, s in cold]
    assert cold_sums == sorted(cold_sums)
    assert cold_sums[0] == 10.0
    assert cold_sums[-1] == 240.0


def test_zero_liter_hour_does_not_break_continuity():
    """A perfectly idle hour (the user is away) still produces a slot
    with the same running sum as the previous one. The Energy dashboard
    will draw a 0-height bar and that's correct."""
    items = [(_hour(0), 100.0), (_hour(1), 0.0), (_hour(2), 50.0)]
    out = continuation_stats(items)
    assert out == [
        (_hour(0), 100.0),
        (_hour(1), 100.0),
        (_hour(2), 150.0),
    ]


# ---------------------------------------------------------------------
# Anchor (last_sum) semantics
# ---------------------------------------------------------------------


def test_last_sum_is_used_when_last_start_is_none():
    """Edge case: HA returned a sum but no parseable start (older HA
    versions sometimes did this). Use the sum as anchor and emit
    everything (we can't filter without a start to compare against)."""
    items = [(_hour(0), 100.0), (_hour(1), 50.0)]
    out = continuation_stats(items, last_sum=500.0, last_start=None)
    assert out == [(_hour(0), 600.0), (_hour(1), 650.0)]


def test_default_last_sum_is_zero():
    """No explicit anchor → cold-start behaviour."""
    items = [(_hour(0), 100.0)]
    out = continuation_stats(items)
    assert out == [(_hour(0), 100.0)]


def test_last_sum_zero_with_last_start_filters_correctly():
    """A clean install can hit last_sum=0 with a real last_start the
    very second push — must still skip past hours."""
    items = [(_hour(0), 100.0), (_hour(1), 50.0)]
    out = continuation_stats(items, last_sum=0.0, last_start=_hour(0))
    assert out == [(_hour(1), 50.0)]
