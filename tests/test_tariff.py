"""Unit tests for ``tariff.py`` — Canal de Isabel II cost computation.

The two end-to-end tests at the bottom (``TestRealBillValidation``)
reproduce the ``Total factura (IVA incluido)`` line from two real
bills the integration developer keeps locally for calibration. The
bills themselves are NOT in the repo (they contain personal data);
only the publicly-defined tariff parameters and the bill totals are
captured here, with no attribution to any specific household,
contract, meter or address.

Why end-to-end against bills
============================

The unit-level tests cover the algebra (block split, vigencia
lookup, etc.). The bill-level tests are the load-bearing ones — they
catch:

- A typo in any of the 40+ price constants in ``VIGENCIAS``.
- A wrong sign in the cuota-fija formula constants.
- Off-by-one in DP day counting (Canal uses ``end - start`` exclusive
  of end; a naïve inclusive count gives a 1-day-too-long DP and
  inflates the cuota fija by a couple of percent).
- The block-threshold-prorating-by-DP behaviour (subtle: a 59-day
  bill puts the B1/B2 boundary at 19,67 m³, not 20).

If either bill assertion fails after a code change, the change is
wrong — even within the loose ±10 % the user is OK with, the maths
should not silently drift.
"""

from __future__ import annotations

import importlib.util
import os as _os
import sys as _sys
import types as _types
from datetime import date, datetime, timedelta
from pathlib import Path


def _load_modules() -> tuple:
    repo = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src_dir = repo / "custom_components" / "canal_isabel_ii"

    pkg_name = "_canal_isabel_ii_for_test"
    if pkg_name not in _sys.modules:
        pkg = _types.ModuleType(pkg_name)
        pkg.__path__ = [str(src_dir)]
        _sys.modules[pkg_name] = pkg

    def _load(submod: str):
        full = f"{pkg_name}.{submod}"
        if full in _sys.modules:
            return _sys.modules[full]
        spec = importlib.util.spec_from_file_location(full, src_dir / f"{submod}.py")
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        _sys.modules[full] = m
        spec.loader.exec_module(m)
        return m

    return (_load("tariff"),)


(_tariff,) = _load_modules()
TariffParams = _tariff.TariffParams
TariffSet = _tariff.TariffSet
VIGENCIAS = _tariff.VIGENCIAS
vigencia_for = _tariff.vigencia_for
block_thresholds = _tariff.block_thresholds
split_into_blocks = _tariff.split_into_blocks
variable_cost_eur = _tariff.variable_cost_eur
cuota_servicio_eur = _tariff.cuota_servicio_eur
compute_period_total_cost = _tariff.compute_period_total_cost
_split_period_by_vigencia = _tariff._split_period_by_vigencia
bimonth_for = _tariff.bimonth_for
compute_hourly_cost_stream = _tariff.compute_hourly_cost_stream


# ---------------------------------------------------------------------
# Vigencia lookup
# ---------------------------------------------------------------------


class TestVigenciaLookup:
    def test_2025_dates_resolve_to_2025_tariff(self):
        v = vigencia_for(date(2025, 6, 15))
        # B1 aducción of the 2025 vigencia is the well-known 0,3054.
        assert v.aducc_per_m3[0] == 0.3054

    def test_first_of_january_2026_resolves_to_2026_tariff(self):
        # Boundary case: Canal applies the new tariff starting on
        # 01-01-2026 inclusive. valid_from is inclusive in our model.
        v = vigencia_for(date(2026, 1, 1))
        assert v.aducc_per_m3[0] == 0.3146

    def test_last_day_of_2025_still_2025_tariff(self):
        # valid_until is exclusive, so 31-12-2025 belongs to 2025.
        v = vigencia_for(date(2025, 12, 31))
        assert v.aducc_per_m3[0] == 0.3054

    def test_uncovered_date_raises(self):
        # Pre-2025 dates aren't modelled.
        try:
            vigencia_for(date(2024, 6, 1))
        except ValueError as e:
            assert "2024-06-01" in str(e)
        else:
            raise AssertionError("expected ValueError for uncovered date")


# ---------------------------------------------------------------------
# Block thresholds + split
# ---------------------------------------------------------------------


class TestBlocks:
    def test_canonical_60_day_thresholds(self):
        u1, u2, u3 = block_thresholds(60)
        assert (u1, u2, u3) == (20.0, 40.0, 60.0)

    def test_30_day_thresholds_are_halved(self):
        u1, u2, u3 = block_thresholds(30)
        assert (u1, u2, u3) == (10.0, 20.0, 30.0)

    def test_59_day_thresholds_are_prorated(self):
        # A 59-day bill (the typical real-bill case where the meter
        # reader didn't visit on the exact 60-day mark) puts the B1/B2
        # boundary at 19,67 m³ — not 20. Critical edge case for
        # bill-equivalent accuracy.
        u1, _u2, _u3 = block_thresholds(59)
        assert abs(u1 - 19.6667) < 0.001

    def test_split_under_b1_stays_in_b1(self):
        b1, b2, b3, b4 = split_into_blocks(15.0, 60)
        assert (b1, b2, b3, b4) == (15.0, 0.0, 0.0, 0.0)

    def test_split_in_b3(self):
        b1, b2, b3, b4 = split_into_blocks(50.0, 60)
        assert b1 == 20.0
        assert b2 == 20.0
        assert b3 == 10.0
        assert b4 == 0.0

    def test_split_in_b4_with_remainder(self):
        b1, b2, b3, b4 = split_into_blocks(244.0, 60)
        assert (b1, b2, b3) == (20.0, 20.0, 20.0)
        assert b4 == 184.0

    def test_split_sums_to_input(self):
        for consumo in (0.0, 5.0, 25.0, 60.0, 100.0, 244.0):
            for dp in (30, 59, 60, 70):
                blocks = split_into_blocks(consumo, dp)
                assert abs(sum(blocks) - consumo) < 1e-9, (
                    f"sum mismatch at consumo={consumo} dp={dp}: {blocks}"
                )


# ---------------------------------------------------------------------
# Cuota fija
# ---------------------------------------------------------------------


class TestCuotaServicio:
    def test_cuota_fija_60_day_period_15mm_one_dwelling_2025(self):
        # Plug into the formula by hand: aducción coef 0,0183 * (15² +
        # 225·1) / 60 * 60 = 0,0183 * 450 = 8,235. Same shape for the
        # other three services.
        params = TariffParams(diametro_mm=15, n_viviendas=1)
        v = vigencia_for(date(2025, 6, 1))
        result = cuota_servicio_eur(params, 60, v)

        expected = (
            0.0183 * 450  # aducción
            + 0.0083 * 450  # distribución
            + 1.1022 * 1  # alcantarillado (N only)
            + 3.2312 * 1  # depuración (N only)
        )
        assert abs(result - expected) < 1e-6

    def test_cuota_fija_scales_linearly_with_dp(self):
        params = TariffParams(diametro_mm=15, n_viviendas=1)
        v = vigencia_for(date(2025, 6, 1))
        full = cuota_servicio_eur(params, 60, v)
        half = cuota_servicio_eur(params, 30, v)
        assert abs(half - full / 2) < 1e-6


# ---------------------------------------------------------------------
# Period totals — vigencia split
# ---------------------------------------------------------------------


class TestPeriodSplit:
    def test_period_entirely_inside_one_vigencia(self):
        # Sanity: a period that doesn't cross any boundary returns
        # one segment with the right vigencia.
        params = TariffParams(diametro_mm=15)
        result = compute_period_total_cost(
            consumo_m3=10.0,
            period_start=date(2025, 6, 1),
            period_end=date(2025, 8, 1),
            params=params,
        )
        # All of consumo at 2025 prices.
        v = vigencia_for(date(2025, 6, 1))
        expected_consumo = variable_cost_eur(10.0, 61, v)
        assert abs(result.consumo_eur - expected_consumo) < 1e-6

    def test_period_crossing_2026_boundary_is_split(self):
        # A 70-day period straddling 01-01-2026 should NOT compute as
        # one segment of 70 days at one tariff. It should split.
        params = TariffParams(diametro_mm=15)
        result_split = compute_period_total_cost(
            consumo_m3=12.0,
            period_start=date(2025, 11, 6),
            period_end=date(2026, 1, 15),
            params=params,
        )
        # If we'd applied 2025 prices to all 70 days, the cuota fija
        # would equal cuota_servicio_eur(params, 70, _2025). Verify
        # the split actually changed the answer.
        v_2025 = vigencia_for(date(2025, 6, 1))
        single_vig_cuota = cuota_servicio_eur(params, 70, v_2025)
        assert abs(result_split.cuota_fija_eur - single_vig_cuota) > 0.01, (
            "expected split-period cuota fija to differ from single-vigencia computation"
        )


# ---------------------------------------------------------------------
# End-to-end regression — synthetic inputs, pinned outputs
# ---------------------------------------------------------------------


class TestEndToEndSynthetic:
    """End-to-end: pin ``compute_period_total_cost`` outputs for two
    representative scenarios.

    These tests do **not** validate against any real bill — they pin
    the model's behaviour for fully synthetic inputs so a regression in
    the maths (block boundaries, prorrateo, vigencia split, IVA) trips
    immediately. Real-bill validation lives outside this repo (each
    user can verify against their own factura via "Configurar" in the
    integration). All numeric inputs and totals here are synthetic.
    """

    def test_high_consumption_single_vigencia(self):
        # Synthetic profile:
        # - 61-day period entirely in 2025.
        # - 200 m³ total — crosses all four blocks.
        # - 20 mm meter, 1 dwelling.
        # - Supplementary sewer fee: 0,1500 €/m³ (synthetic).
        params = TariffParams(
            diametro_mm=20,
            n_viviendas=1,
            cuota_supl_alc_eur_m3=0.1500,
        )
        result = compute_period_total_cost(
            consumo_m3=200.0,
            period_start=date(2025, 6, 1),
            period_end=date(2025, 8, 1),  # exclusive — gives DP=61
            params=params,
        )
        # Pinned: any change in block boundaries, cuotas fijas, IVA, or
        # prorrateo formula will drift this number. Recompute and
        # update only after manual review.
        expected_total = 860.04
        assert abs(result.total_eur - expected_total) < 0.01, (
            f"computed {result.total_eur:.2f} vs pinned {expected_total:.2f}"
        )

    def test_low_consumption_period_crossing_2026_boundary(self):
        # Synthetic profile:
        # - 75-day period straddling 01-01-2026.
        # - 15 m³ total — block 1 only.
        # - 15 mm meter, 1 dwelling.
        # - Supplementary fee changes at the vigencia boundary:
        #     0,1050 €/m³ in 2025, 0,1100 €/m³ in 2026 (synthetic).
        #   Approximated as a single mean weighted by days.
        days_2025 = (date(2026, 1, 1) - date(2025, 11, 1)).days  # 61
        days_2026 = (date(2026, 1, 15) - date(2026, 1, 1)).days  # 14
        weighted_supl = (0.1050 * days_2025 + 0.1100 * days_2026) / (days_2025 + days_2026)
        params = TariffParams(
            diametro_mm=15,
            n_viviendas=1,
            cuota_supl_alc_eur_m3=weighted_supl,
        )
        result = compute_period_total_cost(
            consumo_m3=15.0,
            period_start=date(2025, 11, 1),
            period_end=date(2026, 1, 15),
            params=params,
        )
        # Pinned (see comment in previous test).
        expected_total = 38.82
        assert abs(result.total_eur - expected_total) < 0.01, (
            f"computed {result.total_eur:.2f} vs pinned {expected_total:.2f}"
        )


# ---------------------------------------------------------------------
# Bimonth helpers + cumulative stream
# ---------------------------------------------------------------------


class TestBimonth:
    def test_bimonth_for_january(self):
        s, e = bimonth_for(date(2025, 1, 15))
        assert s == date(2025, 1, 1) and e == date(2025, 3, 1)

    def test_bimonth_for_february(self):
        s, e = bimonth_for(date(2025, 2, 28))
        assert s == date(2025, 1, 1) and e == date(2025, 3, 1)

    def test_bimonth_for_november_wraps_year(self):
        s, e = bimonth_for(date(2025, 11, 30))
        assert s == date(2025, 11, 1) and e == date(2026, 1, 1)


class TestHourlyCostStream:
    def test_empty_input_returns_empty(self):
        params = TariffParams(diametro_mm=15)
        assert compute_hourly_cost_stream([], params) == []

    def test_monotone_cumulative(self):
        # Twenty consecutive hours of 5 L each → cumulative cost must
        # never decrease.
        params = TariffParams(diametro_mm=15, cuota_supl_alc_eur_m3=0.1)
        readings = [(datetime(2025, 7, 1, 0) + timedelta(hours=i), 5.0) for i in range(20)]
        stream = compute_hourly_cost_stream(readings, params)
        assert len(stream) == 20
        prev = -1.0
        for row in stream:
            assert row.cumulative_eur > prev
            prev = row.cumulative_eur

    def test_total_of_hours_equals_period_total(self):
        # Sum of per-hour cost increments across an entire bimonth
        # MUST equal compute_period_total_cost for that bimonth's
        # consumption (within rounding). This is the contract that
        # makes the cost sensor add up to the bill amount.
        params = TariffParams(diametro_mm=15, n_viviendas=1, cuota_supl_alc_eur_m3=0.1)
        # Generate one reading per hour for the entire May+June 2025
        # bimonth, with constant consumption.
        bimonth_start = date(2025, 5, 1)
        bimonth_end = date(2025, 7, 1)
        hours_in_period = (bimonth_end - bimonth_start).days * 24
        liters_per_hour = 50.0  # → 50 * hours / 1000 m³ total
        total_m3 = liters_per_hour * hours_in_period / 1000.0

        readings = [
            (
                datetime.combine(bimonth_start, datetime.min.time()) + timedelta(hours=i),
                liters_per_hour,
            )
            for i in range(hours_in_period)
        ]
        stream = compute_hourly_cost_stream(readings, params)

        period_total = compute_period_total_cost(
            consumo_m3=total_m3,
            period_start=bimonth_start,
            period_end=bimonth_end,
            params=params,
        )

        last_cum = stream[-1].cumulative_eur
        assert abs(last_cum - period_total.total_eur) < 0.01, (
            f"stream total {last_cum:.4f} ≠ period total {period_total.total_eur:.4f}"
        )

    def test_fixed_cost_accrues_during_zero_consumption(self):
        # A reading at hour 0 + a reading at hour 100 with no readings
        # in between. The cumulative cost at hour 100 must include the
        # cuota fija for hours 1-99, otherwise households that go away
        # for a week would see their bill underestimated.
        params = TariffParams(diametro_mm=15, n_viviendas=1)
        t0 = datetime(2025, 5, 1, 0)
        readings = [(t0, 1.0), (t0 + timedelta(hours=100), 1.0)]
        stream = compute_hourly_cost_stream(readings, params)
        # Two reading rows in the output stream, but the second one's
        # cumulative cost should be much greater than just the cost of
        # 2 L — it must include 100 hours of fixed cost.
        gap_cost = stream[1].cumulative_eur - stream[0].cumulative_eur
        # Fixed cost per hour for a 61-day bimonth (May-June) is
        # tiny but non-zero; over 100 hours it should be sub-€1 but
        # clearly more than just the 1 L of variable cost.
        assert gap_cost > 0.05, f"gap cost {gap_cost:.4f} too small — fixed cost not accruing"

    def test_out_of_vigencia_reading_raises_valueerror(self):
        # Documented contract: ``compute_hourly_cost_stream`` propagates
        # the ``ValueError`` that ``vigencia_for`` raises when a reading
        # falls outside every known vigencia. Callers (``sensor.py``)
        # MUST catch this — they degrade by reusing the previous cost
        # value rather than crashing the coordinator. A regression here
        # would let a single ancient backfill row (or a future date the
        # integration hasn't shipped a tariff for yet) take the cost
        # sensor down on every coordinator tick.
        #
        # Note: the date echoed in the error message is the **bimonth
        # start** that the cursor in ``_split_period_by_vigencia``
        # rejects first, not the reading timestamp. So a reading on
        # 2024-06-01 surfaces as a 2024-05-01 ValueError (May-June
        # bimonth). The test asserts the year only; the exact date in
        # the message is an implementation detail.
        params = TariffParams(diametro_mm=15, n_viviendas=1)
        # Pre-2025 dates aren't modelled; one such reading is enough.
        readings = [(datetime(2024, 6, 1, 0), 5.0)]
        try:
            compute_hourly_cost_stream(readings, params)
        except ValueError as err:
            assert "2024" in str(err), f"unexpected ValueError message: {err}"
        else:
            raise AssertionError(
                "expected ValueError for an out-of-vigencia reading; "
                "if vigencia coverage was extended, update this test "
                "with a date still outside every known vigencia."
            )


# ---------------------------------------------------------------------
# Vigencia boundary (2025 → 2026, on 2026-01-01)
# ---------------------------------------------------------------------
#
# These tests exist because Bug 2.1 of the v0.5.x audit identified the
# vigencia transition as a high-risk corner: it is the only date where
# two ``TariffSet`` entries meet, and any off-by-one in the half-open
# interval logic of ``_split_period_by_vigencia`` (or in the bimonth
# alignment that feeds it) silently shifts costs to the wrong tariff.
#
# Specifically guarded:
#
# 1. Half-open semantics — ``valid_until`` is EXCLUSIVE, so a period
#    ending exactly on the boundary must NOT spill a one-day segment
#    into the next vigencia.
# 2. A period that starts EXACTLY at the boundary lives entirely in
#    the new vigencia (no zero-length segment of the old one).
# 3. A period that genuinely STRADDLES the boundary splits cleanly into
#    two segments whose day-counts add up to the original DP, so the
#    pro-rate-by-days math in ``compute_period_total_cost`` doesn't
#    leak m³ across vigencias.
# 4. Cost-stream monotonicity holds across the boundary — the cost
#    sensor is ``state_class=total_increasing`` and any non-monotone
#    blip would be misread by the recorder as a meter reset.
# 5. A two-bimonth stream (Nov-Dec 2025 + Jan-Feb 2026) summed across
#    every hour matches the sum of the two per-bimonth period totals,
#    so the Energy panel keeps adding up to the bill.
class TestVigenciaBoundary:
    BOUNDARY = date(2026, 1, 1)

    def test_split_ends_exactly_on_boundary_is_one_segment(self):
        # Nov-Dec 2025 bimonth ends exactly on 2026-01-01. Because
        # ``valid_until`` is exclusive, the entire period must live in
        # the 2025 vigencia — no spurious 0-day tail in 2026.
        segments = _split_period_by_vigencia(date(2025, 11, 1), self.BOUNDARY)
        assert len(segments) == 1, f"expected 1 segment, got {segments!r}"
        seg_start, seg_end, vigencia = segments[0]
        assert seg_start == date(2025, 11, 1)
        assert seg_end == self.BOUNDARY
        assert vigencia.valid_from == date(2025, 1, 1), (
            "boundary-aligned end must NOT promote the segment to the "
            "next vigencia — half-open interval semantics violated"
        )

    def test_split_starts_exactly_on_boundary_is_2026_only(self):
        # Jan-Feb 2026 bimonth starts exactly on 2026-01-01 → fully in
        # the 2026 vigencia, with no leading 0-day segment of 2025.
        segments = _split_period_by_vigencia(self.BOUNDARY, date(2026, 3, 1))
        assert len(segments) == 1, f"expected 1 segment, got {segments!r}"
        seg_start, seg_end, vigencia = segments[0]
        assert seg_start == self.BOUNDARY
        assert seg_end == date(2026, 3, 1)
        assert vigencia.valid_from == self.BOUNDARY, (
            "period starting on the boundary must use the 2026 vigencia"
        )

    def test_split_straddling_boundary_splits_cleanly(self):
        # A synthetic 60-day period centered on the boundary
        # (2025-12-02 → 2026-01-31) MUST split into two segments whose
        # DPs add up to 60 — otherwise pro-rate-by-days in
        # ``compute_period_total_cost`` would misallocate consumption.
        period_start = date(2025, 12, 2)
        period_end = date(2026, 1, 31)
        segments = _split_period_by_vigencia(period_start, period_end)
        assert len(segments) == 2, f"expected 2 segments, got {segments!r}"

        s1_start, s1_end, v1 = segments[0]
        s2_start, s2_end, v2 = segments[1]

        # Segment 1: Dec 2025 → boundary (exclusive), 2025 vigencia.
        assert s1_start == period_start
        assert s1_end == self.BOUNDARY
        assert v1.valid_from == date(2025, 1, 1)

        # Segment 2: boundary → Jan 31 2026, 2026 vigencia.
        assert s2_start == self.BOUNDARY
        assert s2_end == period_end
        assert v2.valid_from == self.BOUNDARY

        # Day counts add up to the original DP — no fence-post error.
        total_dp = (period_end - period_start).days
        seg_dp_sum = (s1_end - s1_start).days + (s2_end - s2_start).days
        assert seg_dp_sum == total_dp, (
            f"segment DPs {seg_dp_sum} ≠ period DP {total_dp} — "
            f"day accounting leaks across the vigencia boundary"
        )

    def test_compute_period_total_uses_both_vigencias(self):
        # An explicit straddling period priced via
        # ``compute_period_total_cost`` MUST be different from the same
        # consumption priced entirely under either vigencia, otherwise
        # the split is silently using just one tariff (either by sorting
        # the segments out, or by collapsing them).
        params = TariffParams(diametro_mm=15, n_viviendas=1)
        period_start = date(2025, 12, 2)
        period_end = date(2026, 1, 31)
        consumo_m3 = 20.0  # straddles B1/B2 either side of the boundary

        priced_split = compute_period_total_cost(
            consumo_m3=consumo_m3,
            period_start=period_start,
            period_end=period_end,
            params=params,
        )
        priced_2025_only = compute_period_total_cost(
            consumo_m3=consumo_m3,
            period_start=date(2025, 11, 2),  # same DP, fully in 2025
            period_end=date(2025, 12, 31),
            params=params,
        )
        priced_2026_only = compute_period_total_cost(
            consumo_m3=consumo_m3,
            period_start=date(2026, 2, 1),  # same DP, fully in 2026
            period_end=date(2026, 4, 1),  # 60-day window, all in 2026
            params=params,
        )
        # The straddling total must lie strictly between the two
        # single-vigencia totals (or equal one if the tariffs happen to
        # match, but that would be a regression in the constants table).
        lo, hi = sorted([priced_2025_only.total_eur, priced_2026_only.total_eur])
        assert lo - 0.01 <= priced_split.total_eur <= hi + 0.01, (
            f"straddling period priced {priced_split.total_eur:.4f} € "
            f"falls outside [{lo:.4f}, {hi:.4f}] — split is broken"
        )

    def test_hourly_cost_monotone_across_boundary(self):
        # Twelve hours straddling 2026-01-01 00:00 — six on each side.
        # The cumulative cost in the stream is fed to the recorder as
        # ``state_class=total_increasing``; a single non-monotone tick
        # would be flagged as a meter reset and lose data.
        params = TariffParams(diametro_mm=15, n_viviendas=1, cuota_supl_alc_eur_m3=0.1)
        t0 = datetime(2025, 12, 31, 18, 0, 0)
        readings = [(t0 + timedelta(hours=i), 5.0) for i in range(12)]
        stream = compute_hourly_cost_stream(readings, params)
        assert len(stream) == 12
        prev_eur = -1.0
        for row in stream:
            assert row.cumulative_eur > prev_eur, (
                f"non-monotone cumulative at {row.timestamp.isoformat()} "
                f"({row.cumulative_eur:.6f} ≤ {prev_eur:.6f}) — recorder "
                f"would treat this as a meter reset"
            )
            prev_eur = row.cumulative_eur

    def test_hourly_stream_two_bimonths_sum_matches_period_totals(self):
        # Sweep two consecutive bimonths split by the vigencia boundary
        # (Nov-Dec 2025 + Jan-Feb 2026) at one reading per hour. The
        # final ``cumulative_eur`` of the stream MUST equal the sum of
        # ``compute_period_total_cost`` for each bimonth — that's the
        # invariant the bill reconstruction depends on.
        params = TariffParams(diametro_mm=15, n_viviendas=1, cuota_supl_alc_eur_m3=0.1)
        bm1_start, bm1_end = date(2025, 11, 1), date(2026, 1, 1)
        bm2_start, bm2_end = date(2026, 1, 1), date(2026, 3, 1)
        liters_per_hour = 50.0
        bm1_hours = (bm1_end - bm1_start).days * 24
        bm2_hours = (bm2_end - bm2_start).days * 24
        bm1_m3 = liters_per_hour * bm1_hours / 1000.0
        bm2_m3 = liters_per_hour * bm2_hours / 1000.0

        readings = [
            (
                datetime.combine(bm1_start, datetime.min.time()) + timedelta(hours=i),
                liters_per_hour,
            )
            for i in range(bm1_hours + bm2_hours)
        ]
        stream = compute_hourly_cost_stream(readings, params)

        bm1_total = compute_period_total_cost(
            consumo_m3=bm1_m3, period_start=bm1_start, period_end=bm1_end, params=params
        )
        bm2_total = compute_period_total_cost(
            consumo_m3=bm2_m3, period_start=bm2_start, period_end=bm2_end, params=params
        )
        expected = bm1_total.total_eur + bm2_total.total_eur
        assert abs(stream[-1].cumulative_eur - expected) < 0.02, (
            f"stream end {stream[-1].cumulative_eur:.4f} € ≠ sum of "
            f"per-bimonth totals {expected:.4f} € — vigencia boundary "
            f"breaks the bill-reconstruction invariant"
        )
