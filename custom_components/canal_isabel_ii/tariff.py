"""Cost computation for Canal de Isabel II water bills.

Pure stdlib module — no Home Assistant imports — so its tests can
exercise the maths in isolation. ``sensor.py`` calls into here when
the cost feature is enabled in the config flow.

What this module knows
======================

The Canal de Isabel II bill structure for **uso doméstico (1 vivienda)**
as published in BOCM 129 of 31-05-2025 (effective 2025) plus the
01-01-2026 update visible in real bills crossing the boundary. We
encode three things:

1. **Tariff sets** (``TariffSet``) — the €/m³ prices and cuota-fija
   coefficients in force during a "vigencia" (a period between two
   BOCM tariff publications). Today: 2025 vigencia + 2026 vigencia.
   When BOCM publishes the next one, append a new ``TariffSet`` to
   :data:`VIGENCIAS` and the rest follows automatically.

2. **Block thresholds** — Canal applies four price tiers per service
   based on **bimestral** consumption: B1 ≤ 20 m³, B2 20-40 m³, B3
   40-60 m³, B4 > 60 m³. When the actual billing period is not exactly
   60 days (it usually isn't — the meter reader doesn't visit on a
   strict cycle), Canal **prorates the thresholds linearly**:
   ``threshold(n, dp) = (20 · n) * dp / 60``. We mirror this exactly.

3. **Pro-rating across vigencia boundaries** — if a billing period
   straddles 01-01-2026 (or any future boundary), Canal splits days +
   m³ proportionally and applies each side's prices to its segment.
   :func:`compute_period_total_cost` implements this.

What this module does NOT know
==============================

- **The user's bimestral cycle**. Canal doesn't tell us when *your*
  meter is read. We assume calendar bimonths (Jan-Feb, Mar-Apr, …,
  Nov-Dec) by default, which is what most installations follow. The
  config flow could expose an offset later if a user reports their
  cycle is shifted; the resulting per-period block-totals will be
  off but the **annual** total stays exact (you eventually consume
  the same m³ regardless of where the bimestral cuts fall).

- **Other ``uso`` types** (Doméstico 2+ viviendas, Industrial,
  Comercial). The block thresholds and price tables are different.
  v0.5.0 only supports "Doméstico 1 vivienda"; the config flow
  refuses other selections (we'd need a sample bill to model them).

Validation
==========

The module ships with two tests in ``tests/test_tariff.py`` that
reproduce real bills (anonymised) end-to-end:

- A high-consumption bimestral bill (≥ 60 m³, hits all four blocks).
- A low-consumption period straddling 01-01-2026 (validates the
  vigencia split logic and the prorated cuota fija).

Both reproduce the actual bill total to within ±1 cent, which is
~0,05 % deviation — well inside the user-imposed ≤ 10 % budget.

Why a separate module
=====================

Cost is opt-in. Keeping the maths self-contained means:

- Users without the cost feature pay zero import time / runtime cost.
- The maths is unit-testable against bills without spinning up HA.
- A single source of truth for prices: when BOCM publishes new
  tariffs, you edit one tuple and re-run the validation tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------
# Tariff sets (vigencias)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class TariffSet:
    """Prices in effect during one vigencia (BOCM publication window).

    All prices in €/m³, IVA NOT included (10 % is added separately at
    the end of :func:`compute_period_total_cost`). Cuota-fija
    coefficients plug into the formulas printed on the bill:

    - aducción / distribución: ``coef * (D² + 225·N) / 60 * DP``
    - alcantarillado / depuración: ``coef * N / 60 * DP``

    where ``D`` = diameter (mm), ``N`` = nº viviendas, ``DP`` = días
    del periodo.
    """

    valid_from: date
    """Inclusive lower bound of the vigencia window."""

    valid_until: date | None
    """Exclusive upper bound; ``None`` means open-ended (current vigencia)."""

    aducc_per_m3: tuple[float, float, float, float]
    """€/m³ for ADUCCIÓN, blocks 1..4."""

    distrib_per_m3: tuple[float, float, float, float]
    """€/m³ for DISTRIBUCIÓN, blocks 1..4."""

    alcant_per_m3: tuple[float, float, float, float]
    """€/m³ for ALCANTARILLADO, blocks 1..4."""

    depur_per_m3: tuple[float, float, float, float]
    """€/m³ for DEPURACIÓN, blocks 1..4."""

    aducc_fix_coef: float
    """Coefficient in the aducción cuota-fija formula."""

    distrib_fix_coef: float
    """Coefficient in the distribución cuota-fija formula."""

    alcant_fix_coef: float
    """Coefficient in the alcantarillado cuota-fija formula."""

    depur_fix_coef: float
    """Coefficient in the depuración cuota-fija formula."""


# Tariff in force during 2025 (BOCM 129 of 31-05-2025).
_TARIFA_2025 = TariffSet(
    valid_from=date(2025, 1, 1),
    valid_until=date(2026, 1, 1),
    aducc_per_m3=(0.3054, 0.7625, 2.3592, 2.7131),
    distrib_per_m3=(0.1375, 0.2339, 0.5994, 0.6893),
    alcant_per_m3=(0.1127, 0.1338, 0.1759, 0.2023),
    depur_per_m3=(0.3208, 0.3955, 0.6489, 0.7462),
    aducc_fix_coef=0.0183,
    distrib_fix_coef=0.0083,
    alcant_fix_coef=1.1022,
    depur_fix_coef=3.2312,
)

# Tariff in force from 01-01-2026.
#
# CAVEAT — only B1 prices (and the cuota-fija coefficients) are taken
# from a real bill that crossed the 2025/2026 boundary. B2/B3/B4 are
# **extrapolated** by applying the same percentage delta as B1 to each
# of the 2025 values. The error this introduces is bounded:
#
# - For users who never enter B2+ (vast majority of doméstico 1 vivienda
#   households consuming < 20 m³ bimestral), the extrapolation is
#   irrelevant — they only ever see B1 prices.
# - For users who do enter B2+, the deviation vs the real bill is
#   capped at the deviation between the actual BOCM update for B2-B4
#   and a uniform pct change. Historically Canal applies similar pct
#   updates across all blocks, so the error should stay well inside
#   the ±10 % budget.
#
# When a real bill with > 20 m³ in 2026 surfaces, replace these B2-B4
# values with the printed numbers and re-run the validation tests.
_TARIFA_2026_B1_PCT = 0.3146 / 0.3054 - 1  # ≈ +3,01 % observed on aducción B1
_TARIFA_2026 = TariffSet(
    valid_from=date(2026, 1, 1),
    valid_until=None,
    aducc_per_m3=(
        0.3146,  # observed
        round(0.7625 * (1 + _TARIFA_2026_B1_PCT), 4),
        round(2.3592 * (1 + _TARIFA_2026_B1_PCT), 4),
        round(2.7131 * (1 + _TARIFA_2026_B1_PCT), 4),
    ),
    distrib_per_m3=(
        0.1416,  # observed
        round(0.2339 * (1 + _TARIFA_2026_B1_PCT), 4),
        round(0.5994 * (1 + _TARIFA_2026_B1_PCT), 4),
        round(0.6893 * (1 + _TARIFA_2026_B1_PCT), 4),
    ),
    alcant_per_m3=(
        0.1161,  # observed
        round(0.1338 * (1 + _TARIFA_2026_B1_PCT), 4),
        round(0.1759 * (1 + _TARIFA_2026_B1_PCT), 4),
        round(0.2023 * (1 + _TARIFA_2026_B1_PCT), 4),
    ),
    depur_per_m3=(
        0.3304,  # observed
        round(0.3955 * (1 + _TARIFA_2026_B1_PCT), 4),
        round(0.6489 * (1 + _TARIFA_2026_B1_PCT), 4),
        round(0.7462 * (1 + _TARIFA_2026_B1_PCT), 4),
    ),
    aducc_fix_coef=0.0188,
    distrib_fix_coef=0.0085,
    alcant_fix_coef=1.1353,
    depur_fix_coef=3.3281,
)

VIGENCIAS: tuple[TariffSet, ...] = (_TARIFA_2025, _TARIFA_2026)
"""All known vigencias, ordered by ``valid_from``.

Append a new ``TariffSet`` here when BOCM publishes the next tariff
update. Make sure the previous open-ended vigencia gets its
``valid_until`` set so :func:`vigencia_for` keeps returning unique
matches.
"""


def vigencia_for(d: date) -> TariffSet:
    """Return the tariff set in force on the given date.

    Raises ``ValueError`` if no vigencia covers ``d`` — that means we
    forgot to ship a tariff update and the user's bills will be wrong.
    Failing loudly at compute time beats silently using stale prices.
    """
    for v in VIGENCIAS:
        in_lower = v.valid_from <= d
        in_upper = v.valid_until is None or d < v.valid_until
        if in_lower and in_upper:
            return v
    raise ValueError(
        f"No Canal de Isabel II tariff vigencia covers {d.isoformat()} — "
        "the integration needs a tariff update for this date."
    )


# ---------------------------------------------------------------------
# User-editable parameters (from config_flow / options_flow)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class TariffParams:
    """Per-entry tariff inputs the user fills in at install time.

    Only ``cuota_supl_alc_eur_m3`` is municipality-dependent — Canal's
    cuota suplementaria de alcantarillado is approved by each town hall
    individually, so the same household sees different rates on its
    bills depending where the property is. Default 0 means "no
    suplementaria" which is fine for municipalities that don't apply
    one; users that do should copy the value from their bill's
    "CUOTA SUPLEM. ALCANTARILLADO" line.
    """

    diametro_mm: int = 15
    """Meter diameter in mm. Common values: 13, 15, 20, 25, 30."""

    n_viviendas: int = 1
    """Nº de viviendas, locales y usos asimilados (almost always 1)."""

    cuota_supl_alc_eur_m3: float = 0.0
    """€/m³ supplementary fee approved by the town hall. Read from
    your last bill's "CUOTA SUPLEM. ALCANTARILLADO" row."""

    iva_pct: float = 10.0
    """IVA percentage applied to the whole bill. 10 % for water in
    Spain — exposed as a parameter purely so a hypothetical IVA
    change doesn't require a code update."""


# ---------------------------------------------------------------------
# Block thresholds + variable cost
# ---------------------------------------------------------------------


def block_thresholds(dp_days: int) -> tuple[float, float, float]:
    """Return the upper bounds of blocks 1, 2 and 3 (m³) for a billing
    period of ``dp_days`` days.

    Block 4 is "everything above the third bound". Canon: 20/40/60 m³
    for the standard 60-day bimestral period; **prorated linearly by
    actual period length** otherwise.
    """
    factor = dp_days / 60.0
    return 20.0 * factor, 40.0 * factor, 60.0 * factor


def split_into_blocks(consumo_m3: float, dp_days: int) -> tuple[float, float, float, float]:
    """Distribute ``consumo_m3`` across the four blocks for a period
    of ``dp_days``. Returns ``(b1, b2, b3, b4)`` in m³, summing to
    ``consumo_m3``.
    """
    u1, u2, u3 = block_thresholds(dp_days)
    b1 = max(0.0, min(consumo_m3, u1))
    b2 = max(0.0, min(consumo_m3, u2) - u1)
    b3 = max(0.0, min(consumo_m3, u3) - u2)
    b4 = max(0.0, consumo_m3 - u3)
    return b1, b2, b3, b4


def variable_cost_eur(consumo_m3: float, dp_days: int, ts: TariffSet) -> float:
    """€ for the consumption part of the bill, summed across all four
    services (aducción + distribución + alcantarillado + depuración),
    no IVA. Uses ``ts``'s prices throughout — caller is responsible
    for splitting periods at vigencia boundaries.
    """
    blocks = split_into_blocks(consumo_m3, dp_days)
    total = 0.0
    for prices in (
        ts.aducc_per_m3,
        ts.distrib_per_m3,
        ts.alcant_per_m3,
        ts.depur_per_m3,
    ):
        for vol, price in zip(blocks, prices, strict=True):
            total += vol * price
    return total


# ---------------------------------------------------------------------
# Cuota fija (cuota de servicio)
# ---------------------------------------------------------------------


def cuota_servicio_eur(params: TariffParams, dp_days: int, ts: TariffSet) -> float:
    """Cuota fija total for a period of ``dp_days``, no IVA.

    Sum of the four services' cuota-fija formulas. Aducción and
    distribución use ``(D² + 225·N) / 60 * DP``; alcantarillado and
    depuración use ``N / 60 * DP``. Each multiplied by its
    service-specific coefficient from ``ts``.
    """
    d = float(params.diametro_mm)
    n = float(params.n_viviendas)
    base_diam = (d * d + 225.0 * n) / 60.0 * dp_days
    base_n = n / 60.0 * dp_days
    return (
        ts.aducc_fix_coef * base_diam
        + ts.distrib_fix_coef * base_diam
        + ts.alcant_fix_coef * base_n
        + ts.depur_fix_coef * base_n
    )


# ---------------------------------------------------------------------
# Period totals (entry point for bill-equivalent cost)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class CostBreakdown:
    """Itemised cost of a billing period, mirroring the bill layout.

    Fields are in €. ``base_imponible`` is what the bill prints under
    "Base sometida al tipo del 10 % de IVA" (consumo + cuota fija +
    suplementaria); ``total`` is "Total factura (IVA incluido)".
    """

    consumo_eur: float
    cuota_fija_eur: float
    cuota_suplementaria_eur: float
    base_imponible_eur: float
    iva_eur: float
    total_eur: float


def compute_period_total_cost(
    consumo_m3: float,
    period_start: date,
    period_end: date,
    params: TariffParams,
) -> CostBreakdown:
    """Compute the total bill amount for a single bimestral period.

    Mirrors what Canal prints on the bill, including:

    - Per-block variable cost across four services.
    - Cuota de servicio for four services, prorated by ``DP``.
    - Cuota suplementaria de alcantarillado (€/m³ * consumo).
    - 10 % IVA on everything.

    If ``[period_start, period_end)`` straddles a vigencia boundary
    (e.g. spans 01-01-2026), the period is split into segments and
    each segment is priced at its own vigencia. ``consumo_m3`` is
    prorated across segments by days — this is what Canal does when
    the meter wasn't read exactly on the boundary.

    ``period_end`` is **exclusive** (matches Canal's day counting on
    real bills: a bill labelled "21/07/2025 al 18/09/2025" has
    DP = 59 = (date(2025,9,18) - date(2025,7,21)).days, not 60).
    """
    total_dp = (period_end - period_start).days
    if total_dp <= 0:
        raise ValueError(f"period_end ({period_end}) must be after period_start ({period_start})")

    consumo_total = 0.0
    cuota_fija_total = 0.0
    cuota_supl_total = 0.0

    for seg_start, seg_end, vigencia in _split_period_by_vigencia(period_start, period_end):
        seg_dp = (seg_end - seg_start).days
        seg_m3 = consumo_m3 * (seg_dp / total_dp)

        consumo_total += variable_cost_eur(seg_m3, seg_dp, vigencia)
        cuota_fija_total += cuota_servicio_eur(params, seg_dp, vigencia)
        cuota_supl_total += seg_m3 * params.cuota_supl_alc_eur_m3

    base = consumo_total + cuota_fija_total + cuota_supl_total
    iva = base * (params.iva_pct / 100.0)
    return CostBreakdown(
        consumo_eur=consumo_total,
        cuota_fija_eur=cuota_fija_total,
        cuota_suplementaria_eur=cuota_supl_total,
        base_imponible_eur=base,
        iva_eur=iva,
        total_eur=base + iva,
    )


def _split_period_by_vigencia(start: date, end: date) -> list[tuple[date, date, TariffSet]]:
    """Split ``[start, end)`` into ``(seg_start, seg_end, vigencia)``
    triplets so that each segment lives entirely inside one vigencia.
    """
    out: list[tuple[date, date, TariffSet]] = []
    cursor = start
    while cursor < end:
        v = vigencia_for(cursor)
        seg_end = end if v.valid_until is None else min(end, v.valid_until)
        out.append((cursor, seg_end, v))
        cursor = seg_end
    return out


# ---------------------------------------------------------------------
# Cumulative cost stream (for the recorder external statistics)
# ---------------------------------------------------------------------


def bimonth_for(d: date) -> tuple[date, date]:
    """Return ``(start, end_exclusive)`` of the calendar bimestral
    period containing ``d``.

    Periods anchor at odd months: Jan-Feb, Mar-Apr, May-Jun, Jul-Aug,
    Sep-Oct, Nov-Dec. This is the convention most installations
    follow; v0.5.0 doesn't expose an offset. Users whose actual cycle
    is shifted will see slightly off per-period block totals but
    correct annual totals (you eventually consume the same m³).
    """
    odd_month = d.month if d.month % 2 == 1 else d.month - 1
    start = date(d.year, odd_month, 1)
    end = date(d.year + 1, 1, 1) if odd_month == 11 else date(d.year, odd_month + 2, 1)
    return start, end


@dataclass(frozen=True)
class HourlyCost:
    """One row of the cost stream pushed to recorder external stats.

    ``cumulative_eur`` is monotone-increasing across all bimestral
    periods — the cost sensor is ``state_class=total_increasing`` so
    the Energy panel takes diffs to render per-hour cost.
    """

    timestamp: datetime
    cumulative_eur: float


def compute_hourly_cost_stream(
    readings: list[tuple[datetime, float]],
    params: TariffParams,
) -> list[HourlyCost]:
    """Walk readings chronologically and produce a cumulative-cost
    timeline suitable for ``async_add_external_statistics``.

    ``readings`` is a list of ``(timestamp, liters_in_that_hour)``
    tuples. We don't take the full ``Reading`` model here because that
    would couple the test surface to the rest of the integration —
    callers in ``sensor.py`` adapt their own ``Reading`` instances.

    Algorithm:

    1. Group readings by calendar bimonth.
    2. For each bimonth, compute the period's total cost (variable
       per actual m³ * block prices + cuota fija prorated by days +
       suplementaria) using :func:`compute_period_total_cost`.
    3. Distribute that total across the bimonth's hours:

       - Variable + suplementaria: proportional to that hour's m³.
       - Cuota fija (with IVA): uniform per hour over the bimonth.

    4. Accumulate into a monotone series.

    The per-hour distribution is an approximation: Canal doesn't
    actually itemise costs hourly, only at the bill grain. But because
    the **sum across all hours of the bimonth equals the bill total
    exactly**, the Energy panel gets correct daily/weekly/bimestral
    totals — only intra-day cost smoothing is approximated.
    """
    if not readings:
        return []

    by_period: dict[tuple[date, date], list[tuple[datetime, float]]] = {}
    for ts, liters in sorted(readings, key=lambda x: x[0]):
        period = bimonth_for(ts.date())
        by_period.setdefault(period, []).append((ts, liters))

    out: list[HourlyCost] = []
    cum_eur = 0.0
    iva_factor = 1.0 + params.iva_pct / 100.0

    for (p_start, p_end), rows in sorted(by_period.items()):
        period_m3 = sum(liters for _, liters in rows) / 1000.0
        period_hours = (p_end - p_start).days * 24
        if period_hours <= 0:
            continue

        breakdown = compute_period_total_cost(period_m3, p_start, p_end, params)

        # Cuota fija (with IVA) is spread evenly across every hour of
        # the period — it's a flat service availability charge that
        # accrues whether you consume or not.
        fixed_per_hour = breakdown.cuota_fija_eur * iva_factor / period_hours

        # Per-m³ rate (variable + suplementaria, with IVA) so each
        # hour pays its share of the total bill in proportion to its
        # consumption.
        if period_m3 > 0:
            variable_eur_with_iva = (
                breakdown.consumo_eur + breakdown.cuota_suplementaria_eur
            ) * iva_factor
            per_m3_with_iva = variable_eur_with_iva / period_m3
        else:
            per_m3_with_iva = 0.0

        # Accrue fixed cost for every hour of the period so far,
        # whether or not it had a reading. Without this, a household
        # that's away for a week would skip 168 hours of cuota fija
        # and the cumulative total would underflow the real bill.
        cursor = (
            datetime.combine(p_start, datetime.min.time(), tzinfo=rows[0][0].tzinfo)
            if rows[0][0].tzinfo
            else datetime.combine(p_start, datetime.min.time())
        )
        for ts, liters in rows:
            # Catch up fixed cost for any empty hours between cursor
            # and this reading.
            while cursor < ts:
                cum_eur += fixed_per_hour
                cursor += timedelta(hours=1)
            # This hour's cost = its m³ priced + 1 hour of fixed.
            cum_eur += (liters / 1000.0) * per_m3_with_iva + fixed_per_hour
            out.append(HourlyCost(timestamp=ts, cumulative_eur=cum_eur))
            cursor = ts + timedelta(hours=1)

    return out
