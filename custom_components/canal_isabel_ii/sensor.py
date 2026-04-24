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
from .const import CONF_NAME, DEFAULT_NAME, DOMAIN, STATISTICS_SOURCE
from .coordinator import CanalCoordinator
from .models import MeterSummary, Reading
from .statistics_helpers import (
    continuation_stats,
    merge_forward_and_backfill,
    needs_backfill,
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
    coordinator: CanalCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    install_name = (entry.data.get(CONF_NAME) or entry.title or DEFAULT_NAME).strip()

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

    async_add_entities(entities)


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
        # Group entities under a Device so the user sees "Casa Las
        # Rozas" in the device page with the sensors below it.
        # ``identifiers`` must be stable for the lifetime of this
        # contract — the contract id is the natural choice (the
        # user-facing label can change).
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
        self._attr_name = "Consumo última hora"
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
        self._attr_name = "Consumo periodo"
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
        self._attr_name = "Lectura del contador"
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
