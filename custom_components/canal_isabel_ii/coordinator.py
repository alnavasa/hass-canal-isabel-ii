"""Data update coordinator for Canal de Isabel II.

The coordinator does no I/O of its own — data arrives via the
``CanalIngestView`` endpoint and lives in a per-entry
:class:`ReadingStore`. The coordinator is reduced to:

1. **Fan out to sensors** when fresh data lands (the ingest endpoint
   calls ``async_request_refresh()``).
2. **Tick periodically** so time-derived sensor attributes
   (``consumption_today_l`` flips at midnight, ``data_age_minutes``
   grows monotonically, rolling windows slide) refresh without
   waiting for the next user-driven POST.

There's no HTTP here — ``_async_update_data`` just returns the
current store contents. The ``DataUpdateCoordinator`` superclass
does the dedup + sensor notification machinery.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, UPDATE_INTERVAL
from .models import MeterSummary, Reading
from .store import ReadingStore

_LOGGER = logging.getLogger(__name__)


class CanalCoordinator(DataUpdateCoordinator[list[Reading]]):
    """Thin wrapper over :class:`ReadingStore` with periodic refresh."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: ReadingStore) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=UPDATE_INTERVAL,
        )
        self.entry = entry
        self.store = store

    @property
    def meter_summary(self) -> MeterSummary | None:
        """Latest meter summary from the store (may be None on first boot)."""
        return self.store.meter_summary

    async def _async_update_data(self) -> list[Reading]:
        # No I/O — just surface whatever the store currently holds.
        # The store is the single source of truth; the ingest endpoint
        # writes, everyone else reads.
        return self.store.readings
