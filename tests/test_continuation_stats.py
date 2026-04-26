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
is_cost_stream_regression = _helpers.is_cost_stream_regression


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


# =====================================================================
# End-to-end cost-push pipeline regression — the bug a v0.5.7 user hit
# =====================================================================
#
# User report (v0.5.7): the Energy panel's water-cost view showed
# NEGATIVE totals (e.g. -4,54 €, -1336,46 €) when the selected date
# range included days for which Canal had not yet served consumption
# data. The screenshots showed positive numbers shrinking and going
# negative as the range was extended further into the past.
#
# Hypothesised mechanism: ``compute_hourly_cost_stream`` re-derives
# ``per_m3_with_iva`` per push using the bimonth-to-date ``period_m3``.
# As more consumption data arrives during an active bimonth, period_m3
# grows, the block tariff progresses (B1 → B2 → B3 → B4), and the
# blended ``per_m3_with_iva`` rate JUMPS upward. Every previously-
# emitted hour in that bimonth gets a HIGHER ``cumulative_eur`` value
# in the next push. The push pipeline then runs:
#
#     deltas   = cumulative_to_deltas(new_stream)         # new bars
#     existing = (read recorder from earliest_new - 1h)   # old sums
#     merged   = merge_forward_and_backfill(deltas, existing)
#     pusher(...)                                          # upsert
#
# The tests below pin the invariant the recorder & Energy panel rely on
# across that two-push sequence: even when ``per_m3_with_iva`` jumps
# between pushes, the recorder series after the SECOND push must still
# be monotone-non-decreasing AND every previously-pushed hour's sum
# must be ≥ its old sum (no regression at any timestamp). A regression
# would be the recipe for ``sum_at_end - sum_at_start = NEGATIVE`` on
# the panel's range query.
#
# Why this lives in test_continuation_stats.py: it exercises the merge
# pipeline end-to-end with REAL cost-stream input (not synthetic delta
# tuples) — that's the integration the existing TestCumulativeToDeltas
# tests stop short of covering.


def _load_tariff_module():
    """Load tariff.py with the same standalone trick as test_tariff.py.

    Sidesteps custom_components/__init__.py side effects so we don't
    need a full HA test rig to exercise the cost computation.
    """
    import importlib.util as _ilu
    import os as _os2
    import sys as _sys2
    import types as _types
    from pathlib import Path as _Path

    repo = _Path(_os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__))))
    src_dir = repo / "custom_components" / "canal_isabel_ii"
    pkg_name = "_canal_isabel_ii_for_test_pipeline"
    if pkg_name not in _sys2.modules:
        pkg = _types.ModuleType(pkg_name)
        pkg.__path__ = [str(src_dir)]
        _sys2.modules[pkg_name] = pkg
    full = f"{pkg_name}.tariff"
    if full in _sys2.modules:
        return _sys2.modules[full]
    spec = _ilu.spec_from_file_location(full, src_dir / "tariff.py")
    assert spec and spec.loader
    m = _ilu.module_from_spec(spec)
    _sys2.modules[full] = m
    spec.loader.exec_module(m)
    return m


_tariff = _load_tariff_module()
compute_hourly_cost_stream = _tariff.compute_hourly_cost_stream


def _build_readings_at_constant_rate(
    start: datetime,
    n_hours: int,
    liters_per_hour: float,
) -> list[tuple[datetime, float]]:
    """Hourly stream of constant-consumption readings — the driver for
    the two-push regression below."""
    return [(start + timedelta(hours=i), liters_per_hour) for i in range(n_hours)]


def _stream_to_recorder_rows(stream) -> list[tuple[datetime, float]]:
    """Convert a ``compute_hourly_cost_stream`` output into the
    ``(ts, sum)`` shape that the recorder hands back via
    ``statistics_during_period``. The cost-stream output IS already the
    cumulative sum, so this is a pure re-shape — no math.
    """
    return [(hc.timestamp, hc.cumulative_eur) for hc in stream]


def _full_push_cycle(
    new_stream,
    existing_recorder_rows: list[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """Reproduce the production push pipeline end-to-end.

    This mirrors ``CanalCumulativeCostSensor._submit_running_stats``
    one-for-one (sans the actual recorder I/O) for the
    "have-prior-stats" branch — which is the branch every push after
    the very first one takes, and the branch the user's bug surfaces in.
    """
    new_items = [(hc.timestamp, hc.cumulative_eur) for hc in new_stream]
    deltas = cumulative_to_deltas(new_items)
    return merge_forward_and_backfill(deltas, existing_recorder_rows)


class TestCostPushPipelineMonotonicity:
    """End-to-end: two consecutive cost pushes against the same
    bimonth, where the second push has more cache than the first AND
    the extra consumption pushes ``period_m3`` across a block-tariff
    boundary.

    Pins the contract the Energy panel depends on: every recorder sum
    after the second push is ≥ every previous recorder sum, AND no
    hour's sum REGRESSES from push 1 to push 2.
    """

    _params = _tariff.TariffParams(
        diametro_mm=15,
        n_viviendas=1,
        cuota_supl_alc_eur_m3=0.1,
        iva_pct=10.0,
    )

    # Mar-Apr 2026 bimonth: Mar 1 -> May 1 (61 days x 24h = 1464 hours).
    # Block thresholds in this bimonth (DP=61): B1 ≤ 20.33 m³,
    # B2 (20.33, 40.67], B3 (40.67, 61.0], B4 > 61.0. We pick a
    # consumption rate that puts the SECOND push squarely in B2 so the
    # blended per_m3 jumps relative to the first push (which is B1-only).
    _BIMONTH_START = datetime(2026, 3, 1, 0, 0, 0)

    def test_second_push_does_not_regress_any_hour_from_first(self):
        """The user's exact failure mode pinned: a hour's recorder sum
        after push 2 must be ≥ that same hour's sum after push 1.

        If this fails, the Energy panel computing
        ``sum_at_end - sum_at_start`` for ANY range that brackets that
        hour can render a NEGATIVE total. That's the negative-€ bar
        the v0.5.7 user reported.
        """
        # Push 1: 30 days of cache, low rate → all in B1.
        # 30 days x 24 hours x 14 L = 10.08 m^3 over the 30-day window.
        push1_readings = _build_readings_at_constant_rate(
            self._BIMONTH_START, n_hours=30 * 24, liters_per_hour=14.0
        )
        push1_stream = compute_hourly_cost_stream(push1_readings, self._params)
        push1_recorder = _full_push_cycle(push1_stream, existing_recorder_rows=[])

        # Push 2: 50 days of cache (extends 20 days into Apr) at a
        # higher rate so period_m3 crosses 20.33 m³ → enters B2. The
        # extension represents the user pulling the bookmarklet again
        # after consumption picked up.
        push2_readings = _build_readings_at_constant_rate(
            self._BIMONTH_START, n_hours=50 * 24, liters_per_hour=22.0
        )
        push2_stream = compute_hourly_cost_stream(push2_readings, self._params)
        push2_recorder = _full_push_cycle(push2_stream, existing_recorder_rows=push1_recorder)

        # Index push 1 sums by timestamp for the cross-push comparison.
        push1_by_ts = dict(push1_recorder)
        # Build the same index for push 2.
        push2_by_ts = dict(push2_recorder)
        # Every hour that was in push 1 must still be in push 2 (no
        # rows lost) AND its sum must be ≥ the push-1 value.
        regressed: list[tuple[datetime, float, float]] = []
        for ts, old_sum in push1_by_ts.items():
            assert ts in push2_by_ts, (
                f"hour {ts.isoformat()} present in push 1 vanished from push 2 "
                f"— the recorder would still hold the stale row, violating "
                f"the merge replay's totality"
            )
            new_sum = push2_by_ts[ts]
            if new_sum < old_sum - 1e-9:
                regressed.append((ts, old_sum, new_sum))
        assert not regressed, (
            "cost-push pipeline regressed at "
            f"{len(regressed)}/{len(push1_by_ts)} hour(s); first 3: "
            f"{[(ts.isoformat(), f'{o:.4f}€', f'{n:.4f}€') for ts, o, n in regressed[:3]]}"
        )

    def test_merged_recorder_series_is_monotone_after_second_push(self):
        """Even if individual hours are revised upward between pushes,
        the FINAL recorder series must be monotone-non-decreasing in
        chronological order. A single non-monotone tick reads as a
        meter reset to TOTAL_INCREASING (and as a negative bar to
        anything that takes consecutive diffs)."""
        push1_readings = _build_readings_at_constant_rate(
            self._BIMONTH_START, n_hours=30 * 24, liters_per_hour=14.0
        )
        push1_stream = compute_hourly_cost_stream(push1_readings, self._params)
        push1_recorder = _full_push_cycle(push1_stream, existing_recorder_rows=[])

        push2_readings = _build_readings_at_constant_rate(
            self._BIMONTH_START, n_hours=50 * 24, liters_per_hour=22.0
        )
        push2_stream = compute_hourly_cost_stream(push2_readings, self._params)
        push2_recorder = _full_push_cycle(push2_stream, existing_recorder_rows=push1_recorder)

        prev = -1.0
        for ts, sm in push2_recorder:
            assert sm >= prev - 1e-9, (
                f"non-monotone at {ts.isoformat()} ({sm:.4f} € < prev {prev:.4f} €)"
            )
            prev = sm

    def test_no_regression_when_cache_extends_into_a_new_bimonth(self):
        """Push 1 covers only Jan-Feb. Push 2 extends into Mar-Apr.
        The Jan-Feb hours' sums must not change between pushes (their
        bimonth is closed); the new Mar hours must continue monotonically
        from where Jan-Feb left off.

        Critical because cum_eur is a GLOBAL running sum across all
        bimonths in a single push; if the boundary handling loses or
        duplicates the Jan-Feb final cum_eur, every Mar-Apr hour comes
        out shifted relative to push 1's view of the world.
        """
        push1_readings = _build_readings_at_constant_rate(
            datetime(2026, 1, 1, 0), n_hours=59 * 24, liters_per_hour=14.0
        )
        push1_stream = compute_hourly_cost_stream(push1_readings, self._params)
        push1_recorder = _full_push_cycle(push1_stream, existing_recorder_rows=[])

        # Push 2 = push 1 + 25 days of Mar at the same rate.
        push2_readings = push1_readings + _build_readings_at_constant_rate(
            datetime(2026, 3, 1, 0), n_hours=25 * 24, liters_per_hour=14.0
        )
        push2_stream = compute_hourly_cost_stream(push2_readings, self._params)
        push2_recorder = _full_push_cycle(push2_stream, existing_recorder_rows=push1_recorder)

        push1_by_ts = dict(push1_recorder)
        push2_by_ts = dict(push2_recorder)
        regressed: list[tuple[datetime, float, float]] = []
        for ts, old_sum in push1_by_ts.items():
            new_sum = push2_by_ts.get(ts)
            if new_sum is None:
                continue
            if new_sum < old_sum - 1e-9:
                regressed.append((ts, old_sum, new_sum))
        assert not regressed, (
            f"{len(regressed)} hour(s) regressed across the bimonth boundary; "
            f"first 3: {[(ts.isoformat(), f'{o:.4f}€', f'{n:.4f}€') for ts, o, n in regressed[:3]]}"
        )
        # And the final series stays monotone.
        prev = -1.0
        for ts, sm in push2_recorder:
            assert sm >= prev - 1e-9, (
                f"non-monotone at {ts.isoformat()} ({sm:.4f} € < prev {prev:.4f} €)"
            )
            prev = sm

    def test_no_regression_when_cache_starts_mid_bimonth(self):
        """User's first bookmarklet pull happens mid-bimonth — cache
        has Mar 10-Mar 30 only, no Mar 1-9. Push 2 then extends back
        to Mar 5 (user filters portal for an earlier window).

        ``compute_hourly_cost_stream`` always anchors its ``cursor`` at
        the bimonth's p_start (Mar 1) regardless of the first reading's
        timestamp — it accumulates ``fixed_per_hour`` for every empty
        leading hour to keep the cuota fija reflected in the first
        emitted row's cum_eur. Across pushes, the FIRST emitted row's
        cum_eur shifts — push 1's first row is Mar 10, push 2's is
        Mar 5 (5 fewer days of fixed catchup). Common Mar 10+ hours
        must still match within the per-m³ rate revision.
        """
        push1_readings = _build_readings_at_constant_rate(
            datetime(2026, 3, 10, 0), n_hours=20 * 24, liters_per_hour=14.0
        )
        push1_stream = compute_hourly_cost_stream(push1_readings, self._params)
        push1_recorder = _full_push_cycle(push1_stream, existing_recorder_rows=[])

        # Push 2: backfill Mar 5-9 PLUS keep Mar 10-30. Period_m3 of
        # Mar-Apr grows by 5*24*14 = 1680 L = 1.68 m³ — still in B1
        # (B1 ≤ 20.33 m³ for DP=61) so per_m3_with_iva stays the same.
        push2_readings = _build_readings_at_constant_rate(
            datetime(2026, 3, 5, 0), n_hours=25 * 24, liters_per_hour=14.0
        )
        push2_stream = compute_hourly_cost_stream(push2_readings, self._params)
        push2_recorder = _full_push_cycle(push2_stream, existing_recorder_rows=push1_recorder)

        push2_by_ts = dict(push2_recorder)
        # Common hours (Mar 10-30) — push 2's sum must not regress
        # below push 1's sum.
        regressed: list[tuple[datetime, float, float]] = []
        for ts, old_sum in push1_recorder:
            new_sum = push2_by_ts.get(ts)
            if new_sum is None:
                continue
            if new_sum < old_sum - 1e-9:
                regressed.append((ts, old_sum, new_sum))
        assert not regressed, (
            f"{len(regressed)} hour(s) regressed when cache backfilled earlier "
            f"in same bimonth; first 3: "
            f"{[(ts.isoformat(), f'{o:.4f}€', f'{n:.4f}€') for ts, o, n in regressed[:3]]}"
        )

    def test_no_regression_when_cache_has_internal_gap(self):
        """Mid-bimonth gap: cache has Mar 1-15 and Mar 25-30, NOTHING
        for Mar 16-24 (user didn't pull, or sensor was offline). Push
        2 fills the gap with the missing hours.

        compute_hourly_cost_stream's ``cursor`` walk over the gap
        accumulates fixed_per_hour but emits NO rows for those gap
        hours. Push 2's added rows for Mar 16-24 must slot into the
        recorder without making any subsequent hour's sum go down.
        """
        # Push 1 — gappy cache.
        push1_readings = _build_readings_at_constant_rate(
            datetime(2026, 3, 1, 0), n_hours=15 * 24, liters_per_hour=14.0
        ) + _build_readings_at_constant_rate(
            datetime(2026, 3, 25, 0), n_hours=6 * 24, liters_per_hour=14.0
        )
        push1_stream = compute_hourly_cost_stream(push1_readings, self._params)
        push1_recorder = _full_push_cycle(push1_stream, existing_recorder_rows=[])

        # Push 2 — gap filled.
        push2_readings = _build_readings_at_constant_rate(
            datetime(2026, 3, 1, 0), n_hours=30 * 24, liters_per_hour=14.0
        )
        push2_stream = compute_hourly_cost_stream(push2_readings, self._params)
        push2_recorder = _full_push_cycle(push2_stream, existing_recorder_rows=push1_recorder)

        push2_by_ts = dict(push2_recorder)
        regressed = []
        for ts, old_sum in push1_recorder:
            new_sum = push2_by_ts.get(ts)
            if new_sum is None:
                continue
            if new_sum < old_sum - 1e-9:
                regressed.append((ts, old_sum, new_sum))
        assert not regressed, f"{len(regressed)} hour(s) regressed when gap filled in same bimonth"
        prev = -1.0
        for ts, sm in push2_recorder:
            assert sm >= prev - 1e-9, (
                f"non-monotone at {ts.isoformat()} ({sm:.4f} € < prev {prev:.4f} €)"
            )
            prev = sm

    def test_no_regression_when_period_m3_drops_due_to_cache_trim(self):
        """The trim case (most likely the v0.5.7 user's bug):

        - Push 1: cache has 80 days of Jan-Feb-Mar at constant rate.
          period_m3 of Jan-Feb = 13.44 m³ (in B1).
        - Push 2: cache has been trimmed — first 30 days of Jan are
          gone (MAX_READINGS_PER_ENTRY exceeded). Now period_m3 of
          Jan-Feb is only 5.04 m³.

        period_m3 didn't cross a block boundary (B1→B1) so per_m3 is
        unchanged, but the cuota fija catchup over the missing leading
        hours of Jan now happens against a SMALLER period_hours
        denominator effectively — wait no, period_hours = (p_end-p_start).days
        * 24, fixed regardless of trim.

        The real risk: the FIRST emitted row's cum_eur changes between
        pushes (push 1's first row is Jan 1, push 2's is Feb 1) so the
        delta produced by ``cumulative_to_deltas`` for that row is
        different. Recorder rows for trimmed-out hours stay stale in
        the recorder (untouched by push 2). The seam between the
        untouched-old rows and push 2's freshly-replayed rows can be
        the source of a negative bar.

        This is the smoking-gun test for the user's bug: if the
        recorder series after push 2 isn't monotone OR push 2 regresses
        any hour relative to push 1, the bug is real.
        """
        # Push 1 — full Jan-Feb bimonth + half of Mar-Apr.
        push1_readings = _build_readings_at_constant_rate(
            datetime(2026, 1, 1, 0), n_hours=80 * 24, liters_per_hour=14.0
        )
        push1_stream = compute_hourly_cost_stream(push1_readings, self._params)
        push1_recorder = _full_push_cycle(push1_stream, existing_recorder_rows=[])

        # Push 2 — first 30 days of cache TRIMMED OUT (cap exceeded);
        # cache now starts at Feb 1 and extends 60 days (so Feb 1 →
        # Apr 1: covers end of Jan-Feb bimonth + half of Mar-Apr).
        push2_readings = _build_readings_at_constant_rate(
            datetime(2026, 1, 31, 0), n_hours=60 * 24, liters_per_hour=14.0
        )
        push2_stream = compute_hourly_cost_stream(push2_readings, self._params)
        push2_recorder = _full_push_cycle(push2_stream, existing_recorder_rows=push1_recorder)

        push2_by_ts = dict(push2_recorder)
        # The trimmed-out hours (Jan 1 - Jan 30) MUST keep their old
        # sum from push 1 (they're untouched in the recorder). The
        # surviving common hours (Jan 31 - end of push 1) must not
        # regress.
        regressed: list[tuple[datetime, float, float]] = []
        for ts, old_sum in push1_recorder:
            new_sum = push2_by_ts.get(ts)
            if new_sum is None:
                # Hour not in push 2 — recorder still has push 1's
                # value. That's fine on its own; the issue is only if
                # the SEAM between this hour and the next push-2 hour
                # produces a regression.
                continue
            if new_sum < old_sum - 1e-9:
                regressed.append((ts, old_sum, new_sum))
        assert not regressed, (
            f"{len(regressed)}/{len(push1_recorder)} hour(s) regressed across "
            f"a cache trim; first 3: "
            f"{[(ts.isoformat(), f'{o:.4f}€', f'{n:.4f}€') for ts, o, n in regressed[:3]]}"
        )

        # And — the smoking gun assertion. The combined view of the
        # recorder after push 2 (untouched-old + freshly-replayed) must
        # itself be monotone-non-decreasing in chronological order. We
        # simulate that combined view by overlaying push 2 onto push 1.
        combined: dict[datetime, float] = dict(push1_recorder)  # untouched
        combined.update(push2_by_ts)  # push 2 wins on collision
        combined_sorted = sorted(combined.items(), key=lambda kv: kv[0])
        prev_ts = None
        prev_sum = -1.0
        non_monotone: list[tuple[datetime, float, datetime, float]] = []
        for ts, sm in combined_sorted:
            if sm < prev_sum - 1e-9:
                non_monotone.append((prev_ts, prev_sum, ts, sm))
            prev_ts = ts
            prev_sum = sm
        assert not non_monotone, (
            f"recorder series after push 2 is NON-MONOTONE in "
            f"{len(non_monotone)} place(s) — Energy panel will render "
            f"NEGATIVE bars at each. First 3: "
            f"{[(p.isoformat(), f'{ps:.4f}€', n.isoformat(), f'{ns:.4f}€') for p, ps, n, ns in non_monotone[:3]]}"
        )


# ---------------------------------------------------------------------
# is_cost_stream_regression — symmetric guard for state + push paths
# ---------------------------------------------------------------------
#
# This predicate is the v0.5.21 fix for the recurring negative-bar bug
# in the Energy panel. Pre-v0.5.21, only ``CanalCumulativeCostSensor.
# native_value`` had the regression guard; ``_push_cost_statistics_locked``
# did not. The asymmetry meant the entity state would hold at the old
# (higher) value while the recorder push wrote the new (lower) series,
# producing a seam in the long-term statistics that the Energy panel
# rendered as a large negative bar (~ -38 € for MF1 in v0.5.20).
#
# Centralising the predicate in ``statistics_helpers.is_cost_stream_regression``
# and calling it from both paths eliminates the divergence by
# construction. These tests pin the predicate's exact contract: any
# future "let's just compare directly" inlining or threshold tweak
# must keep both sides in lockstep, and the tests fire if not.


def test_regression_returns_false_when_restored_is_none():
    """Fresh install (no prior state) — never a regression so the
    cold-start push proceeds unconditionally."""
    assert is_cost_stream_regression(latest=0.0, restored=None) is False
    assert is_cost_stream_regression(latest=42.0, restored=None) is False


def test_regression_returns_false_when_latest_equals_restored():
    """Idempotent recompute (same input → same output) is not a
    regression — push proceeds and re-asserts the same values to the
    recorder. Cheap upsert; keeps merge logic calibrated."""
    assert is_cost_stream_regression(latest=27.42, restored=27.42) is False


def test_regression_returns_false_when_latest_just_below_restored():
    """Within the 1-cent default threshold — floating-point noise from
    period-by-period recomputation must not flag as a regression.
    Without this tolerance the sensor would freeze on imperceptible
    sub-cent diffs (e.g. tariff segment splits at vigencia boundaries
    rounding differently when liters change by 1 mL between runs)."""
    # 0.005 € below — well inside the 0.01 € threshold.
    assert is_cost_stream_regression(latest=27.415, restored=27.42) is False
    # Right at the threshold edge — also not a regression (strict <).
    assert is_cost_stream_regression(latest=27.41, restored=27.42) is False


def test_regression_returns_true_when_latest_meaningfully_below():
    """Real regressions (cents to euros) are flagged so both the
    state-side and push-side guards skip and preserve the previous
    value. The MF1 v0.5.20 incident was a 38.70 € drop — the predicate
    must catch that without ambiguity."""
    # The MF1 v0.5.20 incident magnitude.
    assert is_cost_stream_regression(latest=111.84, restored=150.54) is True
    # A small but unambiguous regression (10 cents).
    assert is_cost_stream_regression(latest=27.32, restored=27.42) is True


def test_regression_threshold_can_be_overridden():
    """The default 1-cent threshold absorbs FP noise but is exposed
    as a parameter so a future caller (or test harness) can tighten
    or loosen it without forking the helper. Below default → flagged
    earlier; above default → flagged later."""
    # Tighter threshold (1 millicent) — even sub-cent diffs trip it.
    assert is_cost_stream_regression(latest=27.415, restored=27.42, threshold=0.0001) is True
    # Looser threshold (1 €) — only big drops trip it.
    assert is_cost_stream_regression(latest=27.0, restored=27.42, threshold=1.0) is False


def test_regression_returns_false_when_latest_above_restored():
    """Stream growing forward (the normal case) — never a regression."""
    assert is_cost_stream_regression(latest=27.50, restored=27.42) is False
    assert is_cost_stream_regression(latest=151.00, restored=150.54) is False


def test_regression_handles_zero_restored():
    """A restored value of exactly 0.0 is a legitimate state (the
    cumulative cost was zero at restore time, e.g. fresh boot of the
    very first bimonth). Anything < -threshold below it would flag,
    but cum_eur is non-negative by construction so the practical case
    is ``latest >= 0`` → never a regression. Pin the contract anyway."""
    assert is_cost_stream_regression(latest=0.0, restored=0.0) is False
    assert is_cost_stream_regression(latest=0.05, restored=0.0) is False
    # Pathological: a negative latest below threshold WOULD trip — but
    # ``compute_hourly_cost_stream`` guarantees non-negative cum_eur,
    # so this is a contract test for the predicate, not a real path.
    assert is_cost_stream_regression(latest=-0.5, restored=0.0) is True
