"""Cost stream publisher — runs once per ingest, never per coordinator tick.

## Why this module exists (the v0.6.0 redesign)

Until v0.5.x the cost feature was implemented as a triplet of entities
(``CanalCumulativeCostSensor``, ``CanalCurrentPriceSensor``,
``CanalCurrentBlockSensor``). The cumulative-cost sensor doubled as the
publisher of the long-term statistic ``canal_isabel_ii:cost_<contract>``,
recomputing the entire cost stream from the cache on every coordinator
update — every hour OR every POST, whichever arrived first.

That design had a structural problem: the entity owned mutable state
(``_restored_value``) that had to stay in lockstep with the recorder
series, and the cache the cost stream depends on can shift over time
(reading rotation, MAX_READINGS_PER_ENTRY trims, manual portal
backfills). Whenever the recomputed cumulative_eur drifted below the
restored value — for any reason — the panel rendered the seam as a
negative bar (the v0.5.20 bug). The fix train v0.5.20 → 21 → 22 → 23
patched four manifestations of that same underlying coupling.

The user's observation cuts the knot: water consumption from Canal de
Isabel II arrives with a **minimum 12 hour lag**. The portal publishes
hourly readings retroactively, never live. So "live cost in this
second" was never real; recomputing every tick was wasted work AND the
source of every divergence bug.

v0.6.0 splits the cost concern out of the entity layer entirely:

- The cost statistic is published by **this module**, called once per
  successful POST from ``ingest.py``. No recompute on coordinator tick.
- There is no entity that mirrors the statistic, so no in-memory state
  can diverge from the recorder. ``is_cost_stream_regression``,
  ``SIGNAL_CLEAR_COST_STATS`` and the migration flag all go away with
  the entity.
- Users see the cost in the Energy panel by picking the external
  statistic ``<install> - Canal de Isabel II coste`` directly. No
  intermediate sensor needed.

## What this module does NOT do

- It does not create entities. Cost lives only as a long-term
  statistic.
- It does not run on coordinator ticks. The coordinator is back to
  being a pure attribute-refresh mechanism for the consumption sensors.
- It does not maintain in-memory state between calls. Every invocation
  reads the recorder, merges with the new stream and pushes — the
  recorder is the single source of truth.

## Why merge_forward_and_backfill (and not append-only)

The cost stream from :func:`tariff.compute_hourly_cost_stream` always
covers the **full cached window** — earliest reading to latest. So
``items[0].ts`` is almost always ``<= last_start`` after the first
push. An append-only filter would silently drop every row.

The replay-from-zero merge is the only correct mode. It's also
spike-immune by construction (the running sum is recomputed monotonic
from the merged delta series), so the recorder series stays consistent
even across cache trims, vigencia boundary updates or tariff edits.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance as get_recorder_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CUOTA_SUPL_ALC,
    CONF_DIAMETRO_MM,
    CONF_ENABLE_COST,
    CONF_IVA_PCT,
    CONF_N_VIVIENDAS,
    STATISTICS_SOURCE,
)
from .models import Reading
from .statistics_helpers import (
    cumulative_to_deltas,
    merge_forward_and_backfill,
)
from .tariff import TariffParams, compute_hourly_cost_stream

_LOGGER = logging.getLogger(__name__)


def tariff_params_from_settings(settings: dict[str, Any]) -> TariffParams:
    """Build a :class:`TariffParams` from the cached entry settings.

    Lifted from the dead ``sensor._tariff_params_from_settings`` so the
    publisher can stay self-contained — the only caller is the ingest
    endpoint, which already has the merged settings dict in hand.
    """
    return TariffParams(
        diametro_mm=int(settings[CONF_DIAMETRO_MM]),
        n_viviendas=int(settings[CONF_N_VIVIENDAS]),
        cuota_supl_alc_eur_m3=float(settings[CONF_CUOTA_SUPL_ALC]),
        iva_pct=float(settings[CONF_IVA_PCT]),
    )


def cost_statistic_id(contract_id: str) -> str:
    """Return the long-term statistic id for a contract's cost series.

    Centralised so the migration path (purging obsolete entities) and
    the publisher agree on the spelling. Format never changes across
    versions: a rename would break every Energy panel that references
    the previous id.
    """
    return f"{STATISTICS_SOURCE}:cost_{contract_id}"


def cost_statistic_name(install_name: str) -> str:
    """Return the human-readable label for the cost statistic.

    Shown in the Energy panel's stat picker. Stable per install_name
    so users can recognise it after the v0.6.0 upgrade and re-pick if
    they had previously chosen the (now-removed) cost entity.
    """
    return f"{install_name} - Canal de Isabel II coste"


async def publish_cost_stream(
    hass: HomeAssistant,
    entry_id: str,
    contract_id: str,
    install_name: str,
    cost_settings: dict[str, Any],
    readings: list[Reading],
    *,
    currency: str | None = None,
) -> None:
    """Compute + push the cost stream for a contract.

    Called from :class:`ingest.CanalIngestView` after a successful POST,
    once per POST. Idempotent (replays from zero against the recorder),
    safe to call repeatedly.

    Parameters
    ----------
    hass
        The running HomeAssistant instance.
    entry_id
        Config-entry id — used only for log lines that group by install.
    contract_id
        Canal contract identifier (the ``Contrato`` column of the CSV).
        Drives the ``statistic_id``.
    install_name
        User-chosen label ("Casa principal"). Goes into the statistic's
        display name.
    cost_settings
        Resolved cost dict from ``__init__._resolve_cost_settings``.
        Must include ``CONF_ENABLE_COST``; if False we exit early
        without computing anything (the ingest endpoint always passes
        the cost dict so the publisher can decide).
    readings
        Full reading set for this contract. Typically every reading the
        store currently holds for this contract. The publisher takes a
        copy reference; it does not mutate.
    currency
        Optional override for the statistic's unit. Defaults to
        ``hass.config.currency or "EUR"``.

    Failure semantics
    -----------------
    Errors are logged and swallowed. The HTTP POST that triggered the
    publish should NEVER fail because of a downstream cost-stream
    issue: the user's CSV ingestion already succeeded by the time we
    run, and a partial (or skipped) cost push only delays the next
    Energy panel update by one POST cycle.
    """
    if not cost_settings.get(CONF_ENABLE_COST):
        return

    rows = [r for r in readings if r.contract == contract_id]
    if not rows:
        _LOGGER.debug(
            "[%s] cost publisher: no readings for contract %s — nothing to push",
            entry_id,
            contract_id,
        )
        return

    try:
        params = tariff_params_from_settings(cost_settings)
    except (KeyError, TypeError, ValueError):
        _LOGGER.exception(
            "[%s] cost publisher: tariff params malformed in cost_settings — skipping push",
            entry_id,
        )
        return

    local_tz = dt_util.DEFAULT_TIME_ZONE
    timed: list[tuple[datetime, float]] = []
    for r in rows:
        ts = r.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=local_tz)
        timed.append((ts, r.liters))
    # ``compute_hourly_cost_stream`` sorts internally but is cheaper
    # against an already-ordered list; cost is O(n log n) regardless.
    timed.sort(key=lambda x: x[0])

    try:
        stream = compute_hourly_cost_stream(timed, params)
    except ValueError as err:
        _LOGGER.warning(
            "[%s] cost publisher: at least one reading falls outside the known "
            "tariff vigencias (%s); skipping push. Ship a tariff update for "
            "this date range to recover.",
            entry_id,
            err,
        )
        return

    if not stream:
        _LOGGER.debug(
            "[%s] cost publisher: cost stream came back empty — nothing to push",
            entry_id,
        )
        return

    # Convert to (utc_ts, cumulative_eur) tuples for the recorder.
    items: list[tuple[datetime, float]] = []
    for hc in stream:
        ts = hc.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=local_tz)
        items.append((ts.astimezone(dt_util.UTC), hc.cumulative_eur))

    statistic_id = cost_statistic_id(contract_id)
    metadata = StatisticMetaData(
        source=STATISTICS_SOURCE,
        statistic_id=statistic_id,
        has_sum=True,
        name=cost_statistic_name(install_name),
        unit_of_measurement=currency or hass.config.currency or "EUR",
        mean_type=StatisticMeanType.NONE,
        unit_class=None,
    )

    try:
        await _push(hass, statistic_id, metadata, items, entry_id=entry_id)
    except Exception:
        _LOGGER.exception(
            "[%s] cost publisher: push failed for contract %s; the next POST "
            "will retry against the same recorder state.",
            entry_id,
            contract_id,
        )


async def _push(
    hass: HomeAssistant,
    statistic_id: str,
    metadata: StatisticMetaData,
    items: list[tuple[datetime, float]],
    *,
    entry_id: str,
) -> None:
    """Spike-immune push: cold start direct, otherwise replay-from-zero merge.

    Mirrors the algorithm from the now-deleted
    ``CanalCumulativeCostSensor._submit_running_stats`` — same
    invariants, single caller, no entity coupling.
    """
    deltas = cumulative_to_deltas(items)

    recorder = get_recorder_instance(hass)
    last_stats = await recorder.async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        statistic_id,
        True,  # convert_units
        {"sum"},
    )
    last_start: datetime | None = None
    if last_stats and statistic_id in last_stats and last_stats[statistic_id]:
        entry = last_stats[statistic_id][0]
        raw_start = entry.get("start") or entry.get("end")
        if isinstance(raw_start, (int, float)):
            last_start = dt_util.utc_from_timestamp(float(raw_start))
        elif raw_start is not None:
            last_start = dt_util.parse_datetime(str(raw_start))

    # Cold start — no prior stats. The cumulative series is already
    # monotonic, push it as ``state=cum, sum=cum`` directly.
    if last_start is None:
        if not items:
            return
        stats = [StatisticData(start=ts, state=v, sum=v) for ts, v in items]
        _LOGGER.info(
            "[%s] cost publisher: cold start — importing %d hourly cost stats (id=%s)",
            entry_id,
            len(stats),
            statistic_id,
        )
        async_add_external_statistics(hass, metadata, stats)
        return

    # Have prior stats — merge new deltas with existing recorder rows
    # (read back to one hour before the earliest new item so the
    # adjacency is preserved) and replay from zero.
    if not deltas:
        return
    earliest_new = min(ts for ts, _ in deltas)
    query_from = earliest_new - timedelta(hours=1)
    existing_raw = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        query_from,
        None,  # end_time: up to now
        {statistic_id},
        "hour",
        None,  # default units
        {"sum"},
    )

    existing_rows: list[tuple[datetime, float]] = []
    for row in (existing_raw or {}).get(statistic_id, []):
        raw_start = row.get("start") or row.get("end")
        if raw_start is None:
            continue
        if isinstance(raw_start, (int, float)):
            ts = dt_util.utc_from_timestamp(float(raw_start))
        else:
            ts = dt_util.parse_datetime(str(raw_start))
            if ts is None:
                continue
        running = row.get("sum")
        if running is None:
            continue
        existing_rows.append((ts, float(running)))

    merged = merge_forward_and_backfill(deltas, existing_rows)
    if not merged:
        _LOGGER.debug(
            "[%s] cost publisher: merge produced empty series — skipping push (id=%s)",
            entry_id,
            statistic_id,
        )
        return

    stats = [StatisticData(start=ts, state=v, sum=v) for ts, v in merged]
    _LOGGER.debug(
        "[%s] cost publisher: replaying %d hourly cost stats "
        "(merged %d new with %d existing) for id=%s",
        entry_id,
        len(stats),
        len(deltas),
        len(existing_rows),
        statistic_id,
    )
    async_add_external_statistics(hass, metadata, stats)
