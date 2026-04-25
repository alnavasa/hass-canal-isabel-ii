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
needs_backfill = _helpers.needs_backfill
merge_forward_and_backfill = _helpers.merge_forward_and_backfill
cumulative_to_deltas = _helpers.cumulative_to_deltas


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


# =====================================================================
# needs_backfill — the mode detector
# =====================================================================


class TestNeedsBackfill:
    def test_no_items_is_false(self):
        assert needs_backfill([], last_start=_hour(10)) is False

    def test_no_last_start_is_false(self):
        """No previous stats → no backfill possible, delegate to
        cold-start path in continuation_stats."""
        items = [(_hour(0), 100.0)]
        assert needs_backfill(items, last_start=None) is False

    def test_all_items_strictly_after_is_false(self):
        """Rolling-forward push — happy path, stay on the fast path."""
        items = [(_hour(11), 50.0), (_hour(12), 60.0)]
        assert needs_backfill(items, last_start=_hour(10)) is False

    def test_item_equal_to_last_start_is_backfill(self):
        """``<=`` is the same rule continuation_stats uses to skip.
        If we'd skip it there, backfill should trigger here."""
        items = [(_hour(10), 50.0)]
        assert needs_backfill(items, last_start=_hour(10)) is True

    def test_any_item_before_last_start_triggers_backfill(self):
        """User pulled January, last_start is this week. Every January
        row is before last_start → backfill."""
        items = [(_hour(0), 100.0), (_hour(24), 200.0)]
        assert needs_backfill(items, last_start=_hour(100)) is True

    def test_mixed_recent_and_old_items_triggers_backfill(self):
        """Mix of past + new: the past rows would be silently dropped
        by continuation_stats, so we route through the merge path."""
        items = [(_hour(5), 100.0), (_hour(50), 200.0)]
        assert needs_backfill(items, last_start=_hour(10)) is True


# =====================================================================
# merge_forward_and_backfill — the replay-from-zero path
# =====================================================================


class TestMergeForwardAndBackfill:
    def test_empty_existing_behaves_like_cold_start(self):
        """No prior stats → just running sum over new items, same
        output as continuation_stats cold start."""
        items = [(_hour(0), 100.0), (_hour(1), 50.0)]
        out = merge_forward_and_backfill(items, existing_rows=[])
        assert out == [(_hour(0), 100.0), (_hour(1), 150.0)]

    def test_empty_new_items_replays_existing(self):
        """Existing rows round-trip (delta → running sum from zero)."""
        # Existing stored as running sums: 100, 150, 175
        existing = [(_hour(0), 100.0), (_hour(1), 150.0), (_hour(2), 175.0)]
        out = merge_forward_and_backfill([], existing)
        assert out == [(_hour(0), 100.0), (_hour(1), 150.0), (_hour(2), 175.0)]

    def test_backfill_past_hours_before_existing_series(self):
        """User backfilling January into a series that only has
        February onwards. All new hours are inserted at the front
        of the rendered series."""
        # Existing: Feb 1 at hour(100), running sum started anywhere
        existing = [(_hour(100), 500.0), (_hour(101), 510.0)]
        # New: January hours 0 and 1, 10 L each.
        items = [(_hour(0), 10.0), (_hour(1), 10.0)]
        out = merge_forward_and_backfill(items, existing)
        # The series now has 4 rows, chronologically sorted. Running
        # sum from zero: 10, 20, then +500 delta gives 520, +10 = 530.
        # Key invariant: bar heights (sum_n - sum_{n-1}) match the
        # corresponding deltas (10, 10, 500, 10).
        assert [ts for ts, _ in out] == [_hour(0), _hour(1), _hour(100), _hour(101)]
        sums = [s for _, s in out]
        bars = [sums[0]] + [sums[i] - sums[i - 1] for i in range(1, len(sums))]
        assert bars == [10.0, 10.0, 500.0, 10.0]

    def test_backfill_overlapping_hours_new_wins_on_collision(self):
        """Re-downloading the same month after a fix in the portal:
        timestamp collisions → new value replaces old."""
        # Existing: hour(5) has a running sum implying 25 L delta.
        existing = [(_hour(0), 100.0), (_hour(5), 125.0)]
        # New: same hour(5) but portal now shows 77 L for that slot.
        items = [(_hour(5), 77.0)]
        out = merge_forward_and_backfill(items, existing)
        # Bars: first row 100 (from existing delta), second row 77
        # (from new) — NOT 25.
        sums = [s for _, s in out]
        bars = [sums[0]] + [sums[i] - sums[i - 1] for i in range(1, len(sums))]
        assert bars == [100.0, 77.0]

    def test_backfill_interleaves_with_existing_series(self):
        """User backfills hour 5 into an existing [0, 10] series —
        the merged replay places hour 5 in its chronological slot."""
        existing = [(_hour(0), 100.0), (_hour(10), 200.0)]
        items = [(_hour(5), 42.0)]
        out = merge_forward_and_backfill(items, existing)
        # Chronological order: 0, 5, 10
        assert [ts for ts, _ in out] == [_hour(0), _hour(5), _hour(10)]
        # Running sum from zero: 100, 142, 242 — last row's delta is
        # the original 200 - 100 = 100 (existing row preserved).
        sums = [s for _, s in out]
        bars = [sums[0]] + [sums[i] - sums[i - 1] for i in range(1, len(sums))]
        assert bars == [100.0, 42.0, 100.0]

    def test_result_is_monotonically_non_decreasing(self):
        """Sanity: running sum from zero over non-negative deltas
        can never decrease — matches TOTAL_INCREASING expectations."""
        existing = [(_hour(0), 50.0), (_hour(2), 80.0)]  # deltas 50, 30
        items = [(_hour(1), 0.0), (_hour(3), 100.0)]  # deltas 0, 100
        out = merge_forward_and_backfill(items, existing)
        sums = [s for _, s in out]
        assert sums == sorted(sums)

    def test_defensive_negative_existing_delta_pinned_to_zero(self):
        """Corrupt stored series (e.g. manual recorder edit) that
        regresses must not propagate the regression into the replay.
        The affected row becomes a 0 L bar; everything else stays
        monotonic."""
        # Second row's sum is LOWER than first — a regression.
        existing = [(_hour(0), 100.0), (_hour(1), 50.0), (_hour(2), 60.0)]
        out = merge_forward_and_backfill([], existing)
        sums = [s for _, s in out]
        # First delta: 100 (from zero). Second: max(50-100, 0) = 0.
        # Third: 60 - 50 = 10.
        bars = [sums[0]] + [sums[i] - sums[i - 1] for i in range(1, len(sums))]
        assert bars == [100.0, 0.0, 10.0]
        assert sums == sorted(sums)

    def test_backfill_with_unsorted_inputs(self):
        """Both new items and existing rows may arrive unsorted —
        the merge must sort before replaying."""
        existing = [(_hour(10), 200.0), (_hour(0), 100.0)]  # unsorted
        items = [(_hour(5), 42.0), (_hour(3), 7.0)]  # unsorted
        out = merge_forward_and_backfill(items, existing)
        assert [ts for ts, _ in out] == [_hour(0), _hour(3), _hour(5), _hour(10)]

    def test_bar_heights_preserved_modulo_new_inserts(self):
        """The central invariant of the whole backfill design: every
        existing row's rendered bar (sum_n - sum_{n-1}) is preserved
        after the replay, modulo adjacent inserts which get their own
        bars. A shift of the running sum baseline is invisible to the
        Energy dashboard."""
        # Existing: 5 hours with deltas 10, 20, 30, 40, 50 = cumulative 10/30/60/100/150
        existing = [
            (_hour(10), 10.0),
            (_hour(11), 30.0),
            (_hour(12), 60.0),
            (_hour(13), 100.0),
            (_hour(14), 150.0),
        ]
        # New: backfill hours 0-4 with 5 L each (10 L hour-4 collides… no, hour 0-4 vs 10-14).
        items = [(_hour(h), 5.0) for h in range(5)]
        out = merge_forward_and_backfill(items, existing)
        ts_list = [ts for ts, _ in out]
        sums = [s for _, s in out]
        # Bars for every row = delta from previous (first = sum[0])
        bars = [sums[0]] + [sums[i] - sums[i - 1] for i in range(1, len(sums))]
        # First 5 rows are the new ones, each 5 L.
        # Next 5 rows are the original deltas (10, 20, 30, 40, 50).
        assert bars[:5] == [5.0, 5.0, 5.0, 5.0, 5.0]
        assert bars[5:] == [10.0, 20.0, 30.0, 40.0, 50.0]
        # Chronology preserved.
        assert ts_list == [_hour(h) for h in list(range(5)) + list(range(10, 15))]


# =====================================================================
# cumulative_to_deltas — the cost-stream inverse used by v0.5.4 cost push
# =====================================================================


class TestCumulativeToDeltas:
    """``compute_hourly_cost_stream`` produces a cumulative-€ series; the
    cost push needs deltas so it can feed ``merge_forward_and_backfill``.
    These tests pin the inverse transform behaviour.
    """

    def test_empty_input_returns_empty(self):
        assert cumulative_to_deltas([]) == []

    def test_first_row_delta_equals_its_cumulative(self):
        """Treats the series as starting from zero — so the first
        row's delta is its absolute value. This matches what
        ``merge_forward_and_backfill`` expects (it replays from zero
        too)."""
        items = [(_hour(0), 10.0), (_hour(1), 25.0), (_hour(2), 30.0)]
        out = cumulative_to_deltas(items)
        assert out == [(_hour(0), 10.0), (_hour(1), 15.0), (_hour(2), 5.0)]

    def test_round_trip_through_merge_recovers_bars(self):
        """The whole point: feed cumulative → deltas → merge_replay →
        bars equal the deltas. This is the property the cost push
        relies on for the Energy panel to render correct heights."""
        # Cumulative cost stream with three increments: 5, 7, 3 €.
        items = [(_hour(0), 5.0), (_hour(1), 12.0), (_hour(2), 15.0)]
        deltas = cumulative_to_deltas(items)
        # Empty existing → cold replay through the merge.
        merged = merge_forward_and_backfill(deltas, existing_rows=[])
        sums = [s for _, s in merged]
        bars = [sums[0]] + [sums[i] - sums[i - 1] for i in range(1, len(sums))]
        assert bars == [5.0, 7.0, 3.0]

    def test_zero_delta_hour_emitted(self):
        """A flat hour (no cuota fija accrual happens to align with no
        consumption — exotic but possible) emits a 0.0-delta row, NOT
        a skipped slot. Skipping would create a gap in the recorder
        series."""
        items = [(_hour(0), 5.0), (_hour(1), 5.0), (_hour(2), 7.0)]
        out = cumulative_to_deltas(items)
        assert out == [(_hour(0), 5.0), (_hour(1), 0.0), (_hour(2), 2.0)]

    def test_defensive_negative_delta_clamped_to_zero(self):
        """The cost stream is monotonic by construction, but if a future
        caller (or rounding artefact) feeds a non-monotonic series, the
        delta must clamp to 0.0 — never propagate a negative bar into
        the merge replay."""
        items = [(_hour(0), 10.0), (_hour(1), 8.0), (_hour(2), 12.0)]
        out = cumulative_to_deltas(items)
        # Second row would be -2.0 raw → clamped to 0.0. Third row
        # follows from the *raw* prev (8.0), not the clamped one,
        # giving 12 - 8 = 4.0 — keeps the series faithful to where
        # the cumulative actually went, just without negative bars.
        assert out == [(_hour(0), 10.0), (_hour(1), 0.0), (_hour(2), 4.0)]

    def test_preserves_input_order(self):
        """Sorting is the merge function's responsibility (it sorts
        internally). This helper keeps input order so callers can spot
        an unsorted input by reading the output without reasoning
        about a hidden sort."""
        items = [(_hour(2), 10.0), (_hour(0), 3.0), (_hour(1), 7.0)]
        out = cumulative_to_deltas(items)
        # Input order: index-by-index, not chronological.
        assert [ts for ts, _ in out] == [_hour(2), _hour(0), _hour(1)]
