"""Per-entry persistence for ingested hourly readings + meter summary.

Persists JSON under ``<config>/.storage/canal_isabel_ii.<entry_id>``
via HA's standard :class:`homeassistant.helpers.storage.Store`.
Survives HA restart so the user doesn't have to re-bookmarklet the
moment the box reboots.

Only one writer (the ingest endpoint) and only one reader per tick
(the coordinator), so we don't need a lock — the asyncio event loop
serialises us by construction.

Dedup key: ``(contract, timestamp)``. Re-ingesting the same hour
overwrites whatever was stored for that slot, which is what we want:
the bookmarklet always re-fetches the full visible window, so the
only signal that a row needs updating is a NEW value at an existing
timestamp (e.g. Canal corrected a stale reading post-hoc).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import MAX_READINGS_PER_ENTRY, STORAGE_KEY_PREFIX, STORAGE_VERSION
from .models import MeterSummary, Reading

_LOGGER = logging.getLogger(__name__)


class ReadingStore:
    """Holds the cached readings + meter summary for a single config entry.

    API:

    * ``async_load()`` — call once during ``async_setup_entry`` to
      restore from disk (no-op on first install).
    * ``readings`` / ``meter_summary`` — read-only properties for
      the coordinator + sensors.
    * ``async_replace(...)`` — called by the ingest endpoint when a
      bookmarklet POST lands. Merges new readings with existing, caps
      the size, persists to disk.
    * ``last_ingest_at`` — wall-clock timestamp of the last
      successful POST (used by the freshness attribute on sensors).
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store: Store = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}.{entry_id}",
        )
        self._readings: dict[tuple[str, datetime], Reading] = {}
        self._meter_summary: MeterSummary | None = None
        self._last_ingest_at: datetime | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_load(self) -> None:
        """Restore from ``<config>/.storage/canal_isabel_ii.<entry_id>``."""
        data = await self._store.async_load()
        if not data:
            return
        for row in data.get("readings", []):
            try:
                reading = _reading_from_dict(row)
            except (KeyError, TypeError, ValueError):
                continue
            self._readings[(reading.contract, reading.timestamp)] = reading
        self._meter_summary = _meter_summary_from_dict(data.get("meter_summary"))
        last = data.get("last_ingest_at")
        if last:
            try:
                self._last_ingest_at = datetime.fromisoformat(str(last))
            except (TypeError, ValueError):
                self._last_ingest_at = None
        _LOGGER.debug(
            "[%s] Store loaded: %d readings, meter=%s, last_ingest=%s",
            self._entry_id,
            len(self._readings),
            self._meter_summary,
            self._last_ingest_at,
        )

    async def async_save(self) -> None:
        """Persist the in-memory state to disk."""
        await self._store.async_save(self._serialise())

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    @property
    def readings(self) -> list[Reading]:
        """Sorted by ``(contract, timestamp)`` — the order sensors expect."""
        return sorted(self._readings.values(), key=lambda r: (r.contract, r.timestamp))

    @property
    def meter_summary(self) -> MeterSummary | None:
        return self._meter_summary

    @property
    def last_ingest_at(self) -> datetime | None:
        return self._last_ingest_at

    @property
    def contracts(self) -> set[str]:
        return {r.contract for r in self._readings.values() if r.contract}

    # ------------------------------------------------------------------
    # Mutations (ingest endpoint only)
    # ------------------------------------------------------------------
    async def async_replace(
        self,
        new_readings: list[Reading],
        meter_summary: MeterSummary | None,
        ingest_at: datetime,
    ) -> int:
        """Merge a fresh batch from the bookmarklet POST into the store.

        Returns the number of NEW readings (not counting in-place
        updates). The caller logs this for ops visibility.

        Behaviour:

        * Each (contract, timestamp) is upserted — newer payload
          wins on collision, but we count NEW slots only.
        * After merge we cap at ``MAX_READINGS_PER_ENTRY`` by
          dropping the oldest timestamps, keeping the most recent
          window. Prevents unbounded growth if a user repeatedly
          backfills 365-day windows.
        * ``meter_summary`` is replaced wholesale if not None;
          otherwise the previous value is preserved (don't lose the
          absolute reading just because one POST didn't include it).
        * ``last_ingest_at`` always advances to the new value.
        * Persists to disk before returning.
        """
        new_count = 0
        for r in new_readings:
            key = (r.contract, r.timestamp)
            if key not in self._readings:
                new_count += 1
            self._readings[key] = r

        # Trim oldest if over the cap. We sort the keys by timestamp,
        # not by (contract, timestamp), so multi-contract entries
        # don't have one contract starve the other.
        if len(self._readings) > MAX_READINGS_PER_ENTRY:
            sorted_keys = sorted(self._readings.keys(), key=lambda k: k[1])
            excess = len(self._readings) - MAX_READINGS_PER_ENTRY
            for k in sorted_keys[:excess]:
                del self._readings[k]

        if meter_summary is not None:
            self._meter_summary = meter_summary
        self._last_ingest_at = ingest_at
        await self.async_save()
        return new_count

    async def async_clear(self) -> None:
        """Wipe the store completely — used on entry removal."""
        self._readings.clear()
        self._meter_summary = None
        self._last_ingest_at = None
        await self._store.async_remove()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def _serialise(self) -> dict[str, Any]:
        return {
            "readings": [_reading_to_dict(r) for r in self.readings],
            "meter_summary": _meter_summary_to_dict(self._meter_summary),
            "last_ingest_at": (self._last_ingest_at.isoformat() if self._last_ingest_at else None),
        }


# ----------------------------------------------------------------------
# Free-function (de)serialisers — kept module-level so they're trivially
# unit-testable without instantiating ReadingStore (which would need a
# Hass instance).
# ----------------------------------------------------------------------


def _reading_to_dict(r: Reading) -> dict[str, Any]:
    return {
        "contract": r.contract,
        "timestamp": r.timestamp.isoformat(),
        "liters": r.liters,
        "period": r.period,
        "meter": r.meter,
        "address": r.address,
        "frequency": r.frequency,
    }


def _reading_from_dict(row: dict[str, Any]) -> Reading:
    return Reading(
        contract=str(row.get("contract") or ""),
        timestamp=datetime.fromisoformat(str(row["timestamp"])),
        liters=float(row.get("liters") or 0),
        period=str(row.get("period") or ""),
        meter=str(row.get("meter") or ""),
        address=str(row.get("address") or ""),
        frequency=str(row.get("frequency") or ""),
    )


def _meter_summary_to_dict(m: MeterSummary | None) -> dict[str, Any] | None:
    if m is None:
        return None
    return {
        "reading_liters": m.reading_liters,
        "reading_at": m.reading_at.isoformat() if m.reading_at else None,
        "meter": m.meter,
        "address": m.address,
        "raw_reading": m.raw_reading,
    }


def _meter_summary_from_dict(raw: Any) -> MeterSummary | None:
    if not isinstance(raw, dict):
        return None
    try:
        liters = float(raw["reading_liters"])
    except (KeyError, TypeError, ValueError):
        return None
    reading_at: datetime | None
    raw_at = raw.get("reading_at")
    if raw_at:
        try:
            reading_at = datetime.fromisoformat(str(raw_at))
        except (TypeError, ValueError):
            reading_at = None
    else:
        reading_at = None
    return MeterSummary(
        reading_liters=liters,
        reading_at=reading_at,
        meter=str(raw.get("meter") or ""),
        address=str(raw.get("address") or ""),
        raw_reading=str(raw.get("raw_reading") or ""),
    )
