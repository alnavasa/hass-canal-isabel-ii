"""Data classes shared across the integration.

Plain dataclasses with no HA imports so the pure helpers
(``csv_parser``, ``meter_summary_parser``, ``attribute_helpers``,
``statistics_helpers``) can use them without dragging the rest of the
integration in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Reading:
    """One hourly consumption row from the portal CSV.

    Used as the unit of work between the ingest endpoint and the
    sensor entities — and as the cached row in the per-entry
    ``ReadingStore``. Frozen so it can be a dict key when we
    deduplicate by ``(contract, timestamp)``.
    """

    contract: str
    timestamp: datetime
    liters: float
    period: str
    meter: str
    address: str
    frequency: str


@dataclass(frozen=True)
class MeterSummary:
    """Absolute meter reading scraped from the consumption-page header.

    The hourly :class:`Reading` list gives per-hour deltas; this is the
    cumulative counter the household sees on its physical dial. Useful
    as a sanity cross-check and as the long-running TOTAL_INCREASING
    sensor that survives a cache wipe.
    """

    reading_liters: float
    reading_at: datetime | None
    meter: str
    address: str
    raw_reading: str
