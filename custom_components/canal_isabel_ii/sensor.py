"""Sensor platform for Canal de Isabel II."""

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
    async_import_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .attribute_helpers import (
    TimedReading,
    data_age_minutes,
    sum_for_local_day,
    sum_for_rolling_window,
)
from .const import (
    CONF_CUOTA_SUPL_ALC,
    CONF_DIAMETRO_MM,
    CONF_ENABLE_COST,
    CONF_IVA_PCT,
    CONF_N_VIVIENDAS,
    CONF_NAME,
    DEFAULT_NAME,
    DOMAIN,
    STATISTICS_SOURCE,
)
from .coordinator import CanalCoordinator
from .models import MeterSummary, Reading
from .statistics_helpers import (
    continuation_stats,
    cumulative_to_deltas,
    merge_forward_and_backfill,
    needs_backfill,
)
from .tariff import (
    TariffParams,
    bimonth_for,
    block_thresholds,
    compute_hourly_cost_stream,
    split_into_blocks,
    variable_cost_eur,
    vigencia_for,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Materialise sensors for every contract present in the store.

    On a brand-new entry the store is empty (no bookmarklet POST
    has happened yet). In that case we exit cleanly with no entities
    — the ingest endpoint reloads the entry on its first successful
    POST (see ``ingest.py``), which re-runs ``async_setup_entry``
    with data present and entities get created then.

    Result for the user:

    * Wizard finishes → integration is "loaded" but with zero
      sensors. The user is shown the bookmarklet on the device page.
    * They click the bookmarklet for the first time → the integration
      reloads and the three sensors per contract appear.
    * Subsequent clicks just refresh values (no reload).
    """
    cache = hass.data[DOMAIN][entry.entry_id]
    coordinator: CanalCoordinator = cache["coordinator"]
    install_name = (entry.data.get(CONF_NAME) or entry.title or DEFAULT_NAME).strip()
    cost_settings: dict[str, Any] = cache.get("cost") or {}

    contracts: dict[str, Reading] = {}
    for row in coordinator.data or []:
        if row.contract and row.contract not in contracts:
            contracts[row.contract] = row

    if not contracts:
        _LOGGER.info(
            "[%s] No data ingested yet — sensors will be created after the "
            "first bookmarklet POST triggers a config-entry reload.",
            entry.entry_id,
        )
        return

    entities: list[SensorEntity] = []
    for contract in contracts:
        entities.append(CanalHourlyConsumptionSensor(coordinator, entry, install_name, contract))
        entities.append(
            CanalCumulativeConsumptionSensor(coordinator, entry, install_name, contract)
        )
        entities.append(CanalMeterReadingSensor(coordinator, entry, install_name, contract))
        if cost_settings.get(CONF_ENABLE_COST):
            tariff_params = _tariff_params_from_settings(cost_settings)
            currency = hass.config.currency or "EUR"
            entities.append(
                CanalCumulativeCostSensor(
                    coordinator, entry, install_name, contract, tariff_params, currency
                )
            )
            entities.append(
                CanalCurrentPriceSensor(
                    coordinator, entry, install_name, contract, tariff_params, currency
                )
            )
            entities.append(
                CanalCurrentBlockSensor(coordinator, entry, install_name, contract, tariff_params)
            )

    async_add_entities(entities)


def _tariff_params_from_settings(settings: dict[str, Any]) -> TariffParams:
    """Build a :class:`TariffParams` from the cached entry settings.

    The cache dict is built by ``__init__._resolve_cost_settings`` and
    always has the four keys with sensible defaults, so this never
    raises on missing fields.
    """
    return TariffParams(
        diametro_mm=int(settings[CONF_DIAMETRO_MM]),
        n_viviendas=int(settings[CONF_N_VIVIENDAS]),
        cuota_supl_alc_eur_m3=float(settings[CONF_CUOTA_SUPL_ALC]),
        iva_pct=float(settings[CONF_IVA_PCT]),
    )


class _ContractSensor(CoordinatorEntity[CanalCoordinator], SensorEntity):
    """Common scaffolding for every sensor tied to a single contract.

    has_entity_name=True lets HA compose the friendly name as
    ``<device-name> <entity-name>`` automatically — the device is the
    user's chosen installation label, so the user sees e.g.
    "Casa principal Consumo última hora" without us having to splice
    the name ourselves.
    """

    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CanalCoordinator,
        entry: ConfigEntry,
        install_name: str,
        contract_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._contract_id = contract_id
        self._entry_id = entry.entry_id
        self._install_name = install_name
        # Group entities under a Device so the user sees their chosen
        # installation label (e.g. "Casa principal") in the device
        # page with the sensors below it. ``identifiers`` must be
        # stable for the lifetime of this contract — the contract id
        # is the natural choice (the user-facing label can change).
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{contract_id}")},
            name=install_name,
            manufacturer="Canal de Isabel II",
            model=f"Contrato {contract_id}",
        )

    def _rows(self) -> list[Reading]:
        return [r for r in self.coordinator.data or [] if r.contract == self._contract_id]

    def _sorted_rows(self) -> list[Reading]:
        return sorted(self._rows(), key=lambda r: r.timestamp)

    def _latest(self) -> Reading | None:
        rows = self._sorted_rows()
        return rows[-1] if rows else None

    def _common_attributes(self) -> dict[str, Any]:
        """Stable per-contract metadata + cache-derived aggregates.

        Three layers:

        1. *Stable metadata* pulled from the latest reading
           (``contract`` / ``meter`` / ``address`` / ``period`` /
           ``frequency``).
        2. *Derived aggregates* computed from the cached rows
           (``consumption_today_l``, rolling 7d/30d,
           ``data_age_minutes``, ``oldest_reading_at``). All in
           liters; the math lives in ``attribute_helpers`` so it's
           testable without HomeAssistant.
        3. *Bookmarklet freshness* — ``last_ingest_at`` /
           ``last_ingest_age_minutes`` so the user can see how long
           ago they clicked the bookmarklet (different from
           ``data_age_minutes``, which tracks the most recent reading
           — a click can succeed and still return only stale data
           if Canal hasn't published a fresh hour yet).

        Returning ``None`` for an empty cache avoids reporting
        misleading zeros — a template that checks ``is not none`` can
        distinguish "no data yet" from "zero consumption today".
        """
        rows = self._sorted_rows()
        store = self.coordinator.store
        last_ingest = store.last_ingest_at if store else None
        now = dt_util.utcnow()

        if not rows:
            attrs: dict[str, Any] = {"contract": self._contract_id}
            if last_ingest is not None:
                attrs["last_ingest_at"] = last_ingest.isoformat()
                attrs["last_ingest_age_minutes"] = data_age_minutes(last_ingest, now=now)
            return attrs

        latest = rows[-1]
        oldest = rows[0]
        local_tz = dt_util.DEFAULT_TIME_ZONE
        timed = [TimedReading(timestamp=r.timestamp, liters=r.liters) for r in rows]

        attrs = {
            "contract": self._contract_id,
            "meter": latest.meter or None,
            "address": latest.address or None,
            "period": latest.period or None,
            "frequency": latest.frequency or None,
            "last_reading_at": latest.timestamp.isoformat(),
            "oldest_reading_at": oldest.timestamp.isoformat(),
            "data_age_minutes": data_age_minutes(latest.timestamp, now=now),
            "consumption_today_l": sum_for_local_day(timed, now=now, local_tz=local_tz),
            "consumption_yesterday_l": sum_for_local_day(
                timed, now=now, local_tz=local_tz, days_back=1
            ),
            "consumption_last_7d_l": sum_for_rolling_window(timed, now=now, days=7),
            "consumption_last_30d_l": sum_for_rolling_window(timed, now=now, days=30),
            "last_ingest_at": last_ingest.isoformat() if last_ingest is not None else None,
            "last_ingest_age_minutes": (
                data_age_minutes(last_ingest, now=now) if last_ingest is not None else None
            ),
        }
        # Drop keys whose value is None so the entity card doesn't
        # display a forest of "Unknown" rows.
        return {k: v for k, v in attrs.items() if v is not None}


class CanalHourlyConsumptionSensor(_ContractSensor):
    """Most recent hourly consumption value for a contract."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:water"
    _attr_translation_key = "hourly_consumption"

    def __init__(
        self,
        coordinator: CanalCoordinator,
        entry: ConfigEntry,
        install_name: str,
        contract_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, contract_id)
        # Falls back to the translated name from translations/*.json;
        # we also set an explicit name so it works without
        # translations.
        self._attr_name = "Canal Consumo última hora"
        self._attr_unique_id = f"canal_isabel_ii_{contract_id}_hourly"

    @property
    def native_value(self) -> float | None:
        latest = self._latest()
        return latest.liters if latest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._common_attributes()


class CanalCumulativeConsumptionSensor(_ContractSensor, RestoreSensor):
    """Running total used by the Energy/Water dashboard.

    Backed by RestoreSensor so the state survives restarts even if
    the per-entry store momentarily comes back empty (cold-start
    race after a manual storage wipe). The Energy dashboard reads
    the *external statistics* we push separately — those are
    upsert-protected and remain the authoritative history. The state
    value here is mostly for the entity card and templates, so we
    make it safe rather than perfectly accurate against the cache:

    * If the freshly computed cumulative is lower than the last
      value we remember, we keep the previous one and log a warning.
      That stops TOTAL_INCREASING from interpreting a transient
      cache wipe as a meter reset.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:water-pump"
    _attr_translation_key = "cumulative_consumption"

    def __init__(
        self,
        coordinator: CanalCoordinator,
        entry: ConfigEntry,
        install_name: str,
        contract_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, contract_id)
        self._attr_name = "Canal Consumo periodo"
        self._attr_unique_id = f"canal_isabel_ii_{contract_id}_total"
        # Remembered across HA restarts so we never report below it.
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            try:
                self._restored_value = float(last.native_value)
            except (TypeError, ValueError):
                self._restored_value = None
        self._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        rows = self._sorted_rows()
        if not rows:
            return self._restored_value
        computed = sum(r.liters for r in rows)
        if self._restored_value is not None and computed < self._restored_value - 0.5:
            _LOGGER.warning(
                "Cumulative dropped (%.1f → %.1f L) — likely cache wipe; "
                "keeping previous value to avoid TOTAL_INCREASING reset",
                self._restored_value,
                computed,
            )
            return self._restored_value
        self._restored_value = computed
        return computed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._common_attributes()
        attrs["readings_count"] = len(self._sorted_rows())
        return attrs

    def _handle_coordinator_update(self) -> None:
        # Statistics push is async — fire-and-forget; no value depends
        # on its completion here, errors get logged inside.
        self.hass.async_create_task(self._push_statistics())
        super()._handle_coordinator_update()

    def _statistic_label(self) -> str:
        """User-visible label for the long-term-statistics series.

        Single contract under this install (the typical case): just
        ``"<install> - Canal de Isabel II"`` — clean for the Energy
        picker. Multi-contrato installs append the contract id so
        the two series remain distinguishable.
        """
        contracts = {r.contract for r in self.coordinator.data or [] if r.contract}
        base = f"{self._install_name} - Canal de Isabel II"
        if len(contracts) > 1:
            return f"{base} ({self._contract_id})"
        return base

    async def _push_statistics(self) -> None:
        """Push hourly readings to the long-term external statistics.

        Two distinct paths, picked dynamically per call:

        ## Rolling-forward path (the common case)

        Push only truly new hours — every timestamp strictly after
        the last stored ``(start, sum)``. The running total continues
        from ``last_sum`` so the curve joins seamlessly. Idempotent:
        re-pushing the same rows is a no-op; a shrunken local cache
        emits nothing (see ``continuation_stats`` for the full
        invariant discussion).

        ## Backfill path (user pulled a past month)

        If *any* item in the push lies at or before ``last_start``,
        we assume the user explicitly filtered the portal to a
        historical range they want imported. We:

        1. Read the FULL existing series for this ``statistic_id``
           back to the earliest timestamp we're about to write (plus
           anything already older than that, so the replay covers
           the whole chart).
        2. Invert each row's running ``sum`` into a per-hour delta.
        3. Merge those deltas with the new push (new wins on
           timestamp collision — re-downloaded CSV is
           authoritative).
        4. Recompute the running sum from zero and upsert the entire
           merged series.

        Why the full replay is safe: the Energy dashboard draws each
        bar as ``sum[n] - sum[n-1]``, which is invariant to a
        uniform shift of the running sum — replacing the series
        with a zero-anchored one leaves every rendered bar
        unchanged, while naturally interleaving the newly-inserted
        hours in their chronological position.

        ``async_add_external_statistics`` upserts by ``(statistic_id,
        start)``, so the full-replay write overwrites every row
        we've already stored with its (unchanged-modulo-offset) value
        and inserts the new rows where they belong.
        """
        rows = self._sorted_rows()
        if not rows:
            return

        statistic_id = f"{STATISTICS_SOURCE}:consumption_{self._contract_id}"
        metadata = StatisticMetaData(
            source=STATISTICS_SOURCE,
            statistic_id=statistic_id,
            has_sum=True,
            name=self._statistic_label(),
            unit_of_measurement=UnitOfVolume.LITERS,
            mean_type=StatisticMeanType.NONE,
            unit_class=VolumeConverter.UNIT_CLASS,
        )

        recorder = get_recorder_instance(self.hass)
        last_stats = await recorder.async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,  # convert_units
            {"sum"},  # types — only need the running total
        )
        last_sum = 0.0
        last_start: datetime | None = None
        if last_stats and statistic_id in last_stats and last_stats[statistic_id]:
            entry = last_stats[statistic_id][0]
            last_sum = float(entry.get("sum") or 0.0)
            raw_start = entry.get("start") or entry.get("end")
            if isinstance(raw_start, (int, float)):
                last_start = dt_util.utc_from_timestamp(float(raw_start))
            elif raw_start is not None:
                last_start = dt_util.parse_datetime(str(raw_start))

        items: list[tuple[datetime, float]] = []
        for r in rows:
            ts_local = r.timestamp
            if ts_local.tzinfo is None:
                ts_local = ts_local.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            items.append((ts_local.astimezone(dt_util.UTC), r.liters))

        if needs_backfill(items, last_start):
            await self._push_backfill(statistic_id, metadata, items)
            return

        new_pairs = continuation_stats(items, last_sum=last_sum, last_start=last_start)
        skipped = len(items) - len(new_pairs)

        if not new_pairs:
            _LOGGER.debug(
                "%s: no new hours to import (last_start=%s, skipped=%d)",
                statistic_id,
                last_start,
                skipped,
            )
            return

        stats = [StatisticData(start=ts, state=running, sum=running) for ts, running in new_pairs]
        _LOGGER.debug(
            "%s: importing %d new hourly stats (continuing sum from %.1f, skipped=%d)",
            statistic_id,
            len(stats),
            last_sum,
            skipped,
        )
        async_add_external_statistics(self.hass, metadata, stats)

    async def _push_backfill(
        self,
        statistic_id: str,
        metadata: StatisticMetaData,
        items: list[tuple[datetime, float]],
    ) -> None:
        """Read the full existing series and replay merged with ``items``.

        Separate from ``_push_statistics`` so the rolling-forward path
        stays cheap (one ``get_last_statistics`` call). This path
        costs a full ``statistics_during_period`` fetch — acceptable
        since it only fires when the user deliberately pulls
        historical data.
        """
        # Query back to the earliest timestamp in the push, so every
        # row we're going to rewrite is already in ``existing_rows``
        # when we invert-and-merge. Going earlier than that is fine
        # (and slightly more correct — it folds in any pre-existing
        # older rows that we'd otherwise miss from the replay).
        # We pick the unix epoch as "start of time" proxy — HA
        # internals cap it sensibly.
        earliest_new = min(ts for ts, _ in items)
        # Pad an hour back to make sure we don't skip the hour
        # immediately before the earliest new item.
        query_from = earliest_new - timedelta(hours=1)

        recorder = get_recorder_instance(self.hass)
        existing_raw = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            query_from,
            None,  # end_time: None means up to now
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

        merged = merge_forward_and_backfill(items, existing_rows)
        if not merged:
            _LOGGER.debug("%s: backfill produced empty merge — skipping", statistic_id)
            return

        stats = [StatisticData(start=ts, state=running, sum=running) for ts, running in merged]
        _LOGGER.info(
            "%s: backfill — replaying %d hourly stats (merging %d new with %d existing)",
            statistic_id,
            len(stats),
            len(items),
            len(existing_rows),
        )
        async_add_external_statistics(self.hass, metadata, stats)


class CanalMeterReadingSensor(_ContractSensor, RestoreSensor):
    """Absolute counter as shown on the physical meter dial.

    The hourly CSV gives per-hour deltas; this sensor mirrors the
    "Última lectura" panel the portal renders above the chart, in m³.
    Tracking it as TOTAL_INCREASING lets the user cross-check Canal's
    own bill against HA's accumulated usage and gives a meaningful
    state even before the cache holds any hourly readings.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:counter"
    _attr_translation_key = "meter_reading"

    def __init__(
        self,
        coordinator: CanalCoordinator,
        entry: ConfigEntry,
        install_name: str,
        contract_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, contract_id)
        self._attr_name = "Canal Lectura del contador"
        self._attr_unique_id = f"canal_isabel_ii_{contract_id}_meter_reading"
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            try:
                self._restored_value = float(last.native_value)
            except (TypeError, ValueError):
                self._restored_value = None

    def _summary(self) -> MeterSummary | None:
        return self.coordinator.meter_summary

    @property
    def native_value(self) -> float | None:
        summary = self._summary()
        if summary is None:
            return self._restored_value
        # Internal storage on the store is liters; expose in m³ to
        # match what the portal shows on the meter card.
        m3 = summary.reading_liters / 1000.0
        if self._restored_value is not None and m3 < self._restored_value - 0.001:
            _LOGGER.warning(
                "Meter reading dropped (%.3f → %.3f m³); keeping previous value",
                self._restored_value,
                m3,
            )
            return self._restored_value
        self._restored_value = m3
        return m3

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._common_attributes()
        summary = self._summary()
        if summary:
            if summary.meter:
                attrs["meter"] = summary.meter
            if summary.address:
                attrs["address"] = summary.address
            if summary.reading_at is not None:
                attrs["meter_reading_at"] = summary.reading_at.isoformat()
            if summary.raw_reading:
                attrs["raw_reading"] = summary.raw_reading
        return attrs


# =====================================================================
# Cost sensors (opt-in via the ``enable_cost`` config-flow checkbox)
# =====================================================================
#
# All three sensors below share a TariffParams instance built once at
# entity-creation time. If the user edits the cost params via the
# OptionsFlow, ``__init__._async_update_listener`` reloads the entry
# entirely so these entities are torn down and re-created with fresh
# params — there's no in-place param swap.
#
# The cost sensors do NOT contribute to the consumption-side
# external statistics (``canal_isabel_ii:consumption_<contract>``).
# They publish their own ``canal_isabel_ii:cost_<contract>`` series so
# the Energy panel can chart cost separately from m³.


class _CostSensorMixin:
    """Shared params + helpers for the three cost sensors.

    Every cost sensor needs the same TariffParams + a way to look at
    the current bimonth's accumulated m³, so we factor that here. Not
    a full base class because we still inherit from ``_ContractSensor``
    for the device-grouping + contract-row helpers.
    """

    _params: TariffParams

    def _bimonth_consumo_m3(self: _ContractSensor) -> float:  # type: ignore[misc]
        """Sum of m³ already consumed in the current calendar bimonth.

        Used to decide which tariff block the next m³ would fall into,
        which drives both the current-block and current-price sensors.
        """
        now = dt_util.now()
        b_start, b_end = bimonth_for(now.date())
        total_l = 0.0
        for r in self._rows():
            ts = r.timestamp
            if b_start <= ts.date() < b_end:
                total_l += r.liters
        return total_l / 1000.0


class CanalCumulativeCostSensor(_ContractSensor, RestoreSensor, _CostSensorMixin):
    """Running cost (€) for a contract — feeds the Energy panel.

    Parallels :class:`CanalCumulativeConsumptionSensor` but for money:
    same RestoreSensor pattern (state survives restarts), same
    long-term-statistics push pattern (so the Energy panel's "Money
    tracking" mode picks it up). The numeric series is built by
    :func:`compute_hourly_cost_stream`, which:

    - groups the cached readings by calendar bimonth,
    - prices each one against the right vigencia,
    - distributes cuota fija evenly across the period's hours,
    - emits a monotone cumulative-€ stream.

    The state value (``native_value``) is the most recent
    ``cumulative_eur`` from that stream — i.e. "total € spent so far",
    matching what HA shows on the entity card.

    State class is ``TOTAL`` rather than ``TOTAL_INCREASING``: HA's
    monetary device class strictly disallows ``TOTAL_INCREASING``
    (a monotone-increasing money value with auto-reset detection
    doesn't model real-world money — refunds / corrections can
    decrease it). ``TOTAL`` is the compliant choice for cumulative
    monetary values; we never publish ``last_reset`` so HA treats
    the series as a pure accumulator, which is what we want.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:cash"
    _attr_translation_key = "cumulative_cost"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: CanalCoordinator,
        entry: ConfigEntry,
        install_name: str,
        contract_id: str,
        params: TariffParams,
        currency: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, contract_id)
        self._params = params
        self._attr_native_unit_of_measurement = currency
        self._attr_name = "Canal Coste acumulado"
        self._attr_unique_id = f"canal_isabel_ii_{contract_id}_cumulative_cost"
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            try:
                self._restored_value = float(last.native_value)
            except (TypeError, ValueError):
                self._restored_value = None
        self._handle_coordinator_update()

    def _cost_stream(self) -> list:
        """Compute the full cost stream for this contract from the cache.

        Returns ``[]`` (degrade gracefully — sensor falls back to the
        last restored value) if any reading falls outside a known
        :data:`tariff.VIGENCIAS` window. Without this guard, a single
        out-of-range timestamp (e.g. an ancient backfill from before
        vigencia 2025, or a future date past the last vigencia we've
        shipped) would make :func:`compute_hourly_cost_stream` raise
        ``ValueError`` from inside ``_split_period_by_vigencia``,
        propagate up through ``_push_cost_statistics`` and trip the
        coordinator on every tick. The user would see no cost and a
        wall of tracebacks. We log a clear warning instead so the
        next release of the integration (with the missing vigencia
        appended) silently fixes things.
        """
        rows = self._sorted_rows()
        if not rows:
            return []
        local_tz = dt_util.DEFAULT_TIME_ZONE
        timed: list[tuple[datetime, float]] = []
        for r in rows:
            ts = r.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=local_tz)
            timed.append((ts, r.liters))
        try:
            return compute_hourly_cost_stream(timed, self._params)
        except ValueError as err:
            _LOGGER.warning(
                "[%s] Cost stream skipped — at least one reading falls "
                "outside the known tariff vigencias: %s. The cost sensor "
                "will keep its last value until the integration ships an "
                "updated tariff that covers this date range.",
                self._contract_id,
                err,
            )
            return []

    @property
    def native_value(self) -> float | None:
        stream = self._cost_stream()
        if not stream:
            return self._restored_value
        latest = stream[-1].cumulative_eur
        if self._restored_value is not None and latest < self._restored_value - 0.01:
            # Same rationale as the consumption sensor: a cache wipe
            # would otherwise look like a meter reset to
            # TOTAL_INCREASING.
            _LOGGER.warning(
                "Cumulative cost dropped (%.2f → %.2f); keeping previous value",
                self._restored_value,
                latest,
            )
            return self._restored_value
        self._restored_value = latest
        return round(latest, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "contract": self._contract_id,
            "diametro_mm": self._params.diametro_mm,
            "n_viviendas": self._params.n_viviendas,
            "cuota_supl_alc_eur_m3": self._params.cuota_supl_alc_eur_m3,
            "iva_pct": self._params.iva_pct,
        }
        return attrs

    def _handle_coordinator_update(self) -> None:
        # Fire-and-forget — the state value above doesn't depend on
        # the recorder push completing.
        self.hass.async_create_task(self._push_cost_statistics())
        super()._handle_coordinator_update()

    async def _push_cost_statistics(self) -> None:
        """Push the cumulative-cost stream to long-term statistics.

        We push *twice* with the same data:

        1. **External statistic** ``canal_isabel_ii:cost_<contract>`` —
           visible in the Energy panel's "cost entity" picker as
           ``"<install> - Canal de Isabel II coste"``. Power users who
           prefer the explicit external stat can pick it there.

        2. **Entity statistic** ``sensor.<…>_coste_acumulado`` — the
           recorder auto-generates stats for any ``total_increasing``
           sensor, but those auto-stats only start from the moment the
           sensor was first created. If the cost feature is enabled
           after the integration has already been collecting data, the
           entity's stats are nearly empty and the Energy panel shows
           **0 €** for any reporting period predating the cost feature
           toggle. By also importing the cumulative stream against the
           entity's own ``statistic_id``, the panel can pick the entity
           in its wizard (the obvious UX choice) and still see the full
           historical cost.

        Both writes are upserts by ``(statistic_id, start)``, so
        re-running this on every coordinator update is cheap and
        idempotent. The merge semantics inside
        :meth:`_submit_running_stats` are spike-immune: cold start is
        a direct push of the cumulative series; every subsequent push
        replays the merged delta series from zero, which keeps the
        Energy panel's per-hour bars (``sum[n] - sum[n-1]``) correct
        even if a previous version's stats are still mixed in (the
        ``__init__.py`` migration clears those once on first boot of
        v0.5.4 so this push only ever has clean values to merge with).
        """
        stream = self._cost_stream()
        if not stream:
            return

        currency = self._attr_native_unit_of_measurement or "EUR"

        # Convert the cumulative stream into (utc_ts, cumulative_eur)
        # pairs ordered chronologically. The cost stream is *already*
        # a running total — we store ``state=running, sum=running``
        # directly, no continuation offset needed.
        items: list[tuple[datetime, float]] = []
        for hc in stream:
            ts = hc.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            items.append((ts.astimezone(dt_util.UTC), hc.cumulative_eur))

        # 1) External statistic — survives even if the entity is
        #    renamed/deleted and shows up as a separately-pickable
        #    series in the Energy panel wizard.
        external_meta = StatisticMetaData(
            source=STATISTICS_SOURCE,
            statistic_id=f"{STATISTICS_SOURCE}:cost_{self._contract_id}",
            has_sum=True,
            name=f"{self._install_name} - Canal de Isabel II coste",
            unit_of_measurement=currency,
            mean_type=StatisticMeanType.NONE,
            unit_class=None,
        )
        await self._submit_running_stats(external_meta, items, async_add_external_statistics)

        # 2) Entity statistic — seeds the recorder series the user sees
        #    when they pick the sensor in the Energy panel wizard. Skip
        #    if entity_id isn't bound yet (shouldn't happen because
        #    _handle_coordinator_update only fires after the entity is
        #    added, but defensive).
        if self.entity_id:
            entity_meta = StatisticMetaData(
                source="recorder",
                statistic_id=self.entity_id,
                has_sum=True,
                name=None,  # entity's friendly_name takes over
                unit_of_measurement=currency,
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )
            await self._submit_running_stats(entity_meta, items, async_import_statistics)

    async def _submit_running_stats(
        self,
        metadata: StatisticMetaData,
        items: list[tuple[datetime, float]],
        pusher,
    ) -> None:
        """Push the cumulative-€ series with spike-immune merge semantics.

        ``items`` is ``[(ts_utc, cumulative_eur), ...]``, already
        monotonic by construction (output of
        :func:`compute_hourly_cost_stream`).

        ``pusher`` is either :func:`async_add_external_statistics` (for
        ``source != "recorder"`` external sources) or
        :func:`async_import_statistics` (for ``source == "recorder"``
        entity sources). Identical signatures — fire-and-forget.

        ## Strategy (mirrors the consumption push)

        1. **Cumulative → delta.** ``compute_hourly_cost_stream``
           emits a running total; :func:`merge_forward_and_backfill`
           consumes deltas. :func:`cumulative_to_deltas` is the
           inverse.
        2. **Cold start (no prior stats).** Push the cumulative
           series as-is — the cost stream is monotonic so
           ``state=cum, sum=cum`` is a valid LTS write that the
           Energy panel renders correctly. Cheaper than a full
           replay on the very first push.
        3. **Have prior stats.** Read the existing recorder series,
           merge the new deltas with the old (new wins on
           timestamp collision), replay from zero, push the whole
           merged series.

        ## Why always replay (after the first push)

        The cost stream covers from the **earliest cached reading**
        on every coordinator tick — not just the new hours. So
        ``items[0].ts`` is almost always ``<= last_start`` after the
        first push. A rolling-forward filter would silently drop
        every row. Always-replay is the only correct mode.

        ## Why this fixes the v0.5.2 bugs

        Pre-v0.5.4, the Energy panel rendered:

        - ``0 €`` for periods predating the cost-feature toggle
          (HA's auto-generated stats only started at toggle time;
          the explicit push only covered what the cache held).
        - **Negative bars** at the seam between auto-generated and
          explicitly-pushed values (different baselines, same
          ``statistic_id``).

        v0.5.4 first runs a one-shot migration in ``__init__.py``
        that drops the conflicting series, then this push rebuilds
        them as a single zero-anchored monotonic series. Subsequent
        pushes always replay-from-zero so any future inconsistency
        self-heals on the next coordinator tick.
        """
        statistic_id = metadata["statistic_id"]

        # 1) Cumulative € → per-hour delta.
        deltas = cumulative_to_deltas(items)

        # 2) Read the most recent stored stat to decide cold-start vs replay.
        recorder = get_recorder_instance(self.hass)
        last_stats = await recorder.async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,
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

        # 3a) Cold start — no prior stats. The cumulative series is
        # already monotonic, push it as `state=cum, sum=cum` directly.
        if last_start is None:
            if not items:
                return
            stats = [StatisticData(start=ts, state=v, sum=v) for ts, v in items]
            _LOGGER.info(
                "%s: cold start — importing %d hourly cost stats",
                statistic_id,
                len(stats),
            )
            pusher(self.hass, metadata, stats)
            return

        # 3b) Have prior stats — read them, merge with new deltas,
        # replay from zero, upsert the whole thing.
        if not deltas:
            return
        earliest_new = min(ts for ts, _ in deltas)
        # Pad an hour back so the row immediately before the earliest
        # new item is included in the replay (mirrors the consumption
        # backfill — keeps the replay seamlessly continuous).
        query_from = earliest_new - timedelta(hours=1)
        existing_raw = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
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
                "%s: merge produced empty series — skipping push",
                statistic_id,
            )
            return

        stats = [StatisticData(start=ts, state=v, sum=v) for ts, v in merged]
        _LOGGER.debug(
            "%s: replaying %d hourly cost stats (merged %d new with %d existing)",
            statistic_id,
            len(stats),
            len(deltas),
            len(existing_rows),
        )
        pusher(self.hass, metadata, stats)


class CanalCurrentPriceSensor(_ContractSensor, _CostSensorMixin):
    """€/m³ that the *next* m³ would cost — sum of all four services'
    block prices for the current bimonth's block, plus IVA + the
    suplementaria.

    Useful for templates ("how much does running the dishwasher
    cost?") and for the Energy panel's "Use a price entity" mode if
    the user prefers a live €/m³ rate over total-cost statistics.

    The block changes when consumption crosses each prorated threshold
    within a bimonth, which is why this sensor is stateless w.r.t.
    history — it always reflects the price for the *next* m³ given
    the cache's current bimonth-to-date total.

    No ``device_class`` here even though the unit involves currency:
    HA's monetary device class is for *amounts of money* (with strict
    state-class compatibility — ``TOTAL`` only). This sensor reports a
    *rate* (€/m³), not an amount, so MONETARY would mis-describe the
    semantics and force a state class incompatible with the
    instantaneous-measurement nature of the value. Leaving
    ``device_class`` unset keeps the unit string visible (`EUR/m³`)
    and lets us use ``MEASUREMENT`` honestly.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-clock"
    _attr_translation_key = "current_price"
    _attr_suggested_display_precision = 4

    def __init__(
        self,
        coordinator: CanalCoordinator,
        entry: ConfigEntry,
        install_name: str,
        contract_id: str,
        params: TariffParams,
        currency: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, contract_id)
        self._params = params
        # HA doesn't have a currency/m³ enum — concatenate so the UI
        # shows e.g. "EUR/m³". Some unit converters may complain; we
        # accept that in exchange for an obvious unit string.
        self._attr_native_unit_of_measurement = f"{currency}/m³"
        self._attr_name = "Canal Precio actual"
        self._attr_unique_id = f"canal_isabel_ii_{contract_id}_current_price"

    @property
    def native_value(self) -> float | None:
        now = dt_util.now()
        try:
            ts = vigencia_for(now.date())
        except ValueError:
            return None
        consumo_m3 = self._bimonth_consumo_m3()
        b_start, b_end = bimonth_for(now.date())
        dp_days = (b_end - b_start).days
        # Price the very next 1 m³ to figure out the marginal block
        # price. Cheaper than reverse-engineering split_into_blocks.
        before = variable_cost_eur(consumo_m3, dp_days, ts)
        after = variable_cost_eur(consumo_m3 + 1.0, dp_days, ts)
        marginal = after - before  # € for that extra m³ pre-IVA-pre-supl
        with_supl = marginal + self._params.cuota_supl_alc_eur_m3
        with_iva = with_supl * (1.0 + self._params.iva_pct / 100.0)
        return round(with_iva, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "contract": self._contract_id,
            "bimonth_consumo_m3": round(self._bimonth_consumo_m3(), 3),
        }


class CanalCurrentBlockSensor(_ContractSensor, _CostSensorMixin):
    """Which of Canal's four tariff blocks the *next* m³ would land in.

    State is an int 1..4. Goes hand-in-hand with the price sensor —
    when this rolls from 1 to 2, the price entity jumps to the B2 rate.

    Block thresholds are prorated by the bimonth's actual length (60
    days for a calendar bimonth, but :func:`block_thresholds` accepts
    any DP if the user is on a shifted cycle in the future).
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:format-list-numbered"
    _attr_translation_key = "current_block"
    # Override the L unit inherited from _ContractSensor — block is a
    # dimensionless integer from 1 to 4.
    _attr_native_unit_of_measurement = None

    def __init__(
        self,
        coordinator: CanalCoordinator,
        entry: ConfigEntry,
        install_name: str,
        contract_id: str,
        params: TariffParams,
    ) -> None:
        super().__init__(coordinator, entry, install_name, contract_id)
        self._params = params
        self._attr_name = "Canal Bloque tarifario actual"
        self._attr_unique_id = f"canal_isabel_ii_{contract_id}_current_block"

    @property
    def native_value(self) -> int | None:
        now = dt_util.now()
        b_start, b_end = bimonth_for(now.date())
        dp_days = (b_end - b_start).days
        consumo_m3 = self._bimonth_consumo_m3()
        u1, u2, u3 = block_thresholds(dp_days)
        if consumo_m3 < u1:
            return 1
        if consumo_m3 < u2:
            return 2
        if consumo_m3 < u3:
            return 3
        return 4

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.now()
        b_start, b_end = bimonth_for(now.date())
        dp_days = (b_end - b_start).days
        u1, u2, u3 = block_thresholds(dp_days)
        consumo_m3 = self._bimonth_consumo_m3()
        b1, b2, b3, b4 = split_into_blocks(consumo_m3, dp_days)
        return {
            "contract": self._contract_id,
            "bimonth_consumo_m3": round(consumo_m3, 3),
            "block_1_threshold_m3": round(u1, 3),
            "block_2_threshold_m3": round(u2, 3),
            "block_3_threshold_m3": round(u3, 3),
            "consumed_in_block_1_m3": round(b1, 3),
            "consumed_in_block_2_m3": round(b2, 3),
            "consumed_in_block_3_m3": round(b3, 3),
            "consumed_in_block_4_m3": round(b4, 3),
            "bimonth_start": b_start.isoformat(),
            "bimonth_end": b_end.isoformat(),
        }
