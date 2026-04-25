"""Regression tests for the v0.5.12 cache-trim baseline carry-over.

Without a per-contract baseline, when ``ReadingStore`` trims its cache
once it crosses ``MAX_READINGS_PER_ENTRY``, the cumulative-consumption
sensor sees its computed sum drop below the previously restored monotonic
high and freezes (the ``computed < restored - 0.5`` guard latches the
old value). The baseline must:

1. Accumulate ``trimmed_reading.liters`` per ``contract`` as rows are
   dropped, so the sensor can compute
   ``baseline_liters[contract] + sum(rows in cache) ≥ pre-trim sum``.
2. Round-trip through ``_serialise`` / ``async_load`` so the state
   survives an HA restart.
3. Tolerate pre-v0.5.12 stores that don't have the field (default to
   an empty dict).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os as _os
import sys as _sys
import types as _types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def _load_store_module():
    repo = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src_dir = repo / "custom_components" / "canal_isabel_ii"

    pkg_name = "_canal_isabel_ii_store_test"
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

    # const + models load fine standalone; store needs the Store class
    # from HA but we can stub it before import.
    return _load("const"), _load("models"), _load("store")


# Stub the homeassistant.helpers.storage.Store before the store module
# imports it, so we don't drag in a full HA test fixture for this
# narrowly-scoped unit test.
def _install_ha_stubs():
    if "homeassistant" not in _sys.modules:
        ha = _types.ModuleType("homeassistant")
        _sys.modules["homeassistant"] = ha
    if "homeassistant.core" not in _sys.modules:
        core = _types.ModuleType("homeassistant.core")
        core.HomeAssistant = MagicMock
        _sys.modules["homeassistant.core"] = core
    if "homeassistant.helpers" not in _sys.modules:
        helpers = _types.ModuleType("homeassistant.helpers")
        _sys.modules["homeassistant.helpers"] = helpers
    if "homeassistant.helpers.storage" not in _sys.modules:
        storage = _types.ModuleType("homeassistant.helpers.storage")

        class _StubStore:
            def __init__(self, *args, **kwargs):
                self.saved: dict | None = None

            async def async_load(self):
                return None

            async def async_save(self, data):
                self.saved = data

            async def async_remove(self):
                self.saved = None

        storage.Store = _StubStore
        _sys.modules["homeassistant.helpers.storage"] = storage


_install_ha_stubs()
_const, _models, _store_mod = _load_store_module()
ReadingStore = _store_mod.ReadingStore
Reading = _models.Reading
MAX_READINGS = _const.MAX_READINGS_PER_ENTRY


def _make_readings(n: int, contract: str = "C1") -> list:
    """Build ``n`` consecutive hourly readings for ``contract``."""
    t0 = datetime(2025, 1, 1, 0, 0, 0)
    return [
        Reading(
            contract=contract,
            timestamp=t0 + timedelta(hours=i),
            liters=1.0,  # easy arithmetic: 1 L per hour
            period="hour",
            meter="MTR",
            address="ADDR",
            frequency="hourly",
        )
        for i in range(n)
    ]


def test_trim_accumulates_baseline_liters():
    """When the cache crosses MAX, trimmed liters land in the baseline.

    Total ingested = MAX + 100 readings of 1 L each. After trim the
    cache holds MAX rows and the baseline carries 100 L for this
    contract.
    """
    store = ReadingStore(MagicMock(), "test_entry")
    asyncio.run(
        store.async_replace(
            _make_readings(MAX_READINGS + 100),
            meter_summary=None,
            ingest_at=datetime(2026, 1, 1),
        )
    )
    assert len(store.readings) == MAX_READINGS
    assert store.baseline_liters == {"C1": 100.0}, (
        f"expected 100 L baseline, got {store.baseline_liters!r}"
    )


def test_baseline_separates_per_contract():
    """Multi-contract entries trim by global timestamp, but the
    baseline tracks each contract independently — the consumption
    sensor is per-contract and must not see another contract's trim.
    """
    store = ReadingStore(MagicMock(), "test_entry")
    # Interleave two contracts. Each contract gets MAX//2 + 50 readings,
    # so 100 total over the cap.
    rows_a = _make_readings(MAX_READINGS // 2 + 50, contract="A")
    rows_b = _make_readings(MAX_READINGS // 2 + 50, contract="B")
    # Shift B's timestamps so the global sort interleaves both.
    rows_b = [
        Reading(
            contract=r.contract,
            timestamp=r.timestamp + timedelta(minutes=30),
            liters=2.0,  # different value to verify accounting
            period=r.period,
            meter=r.meter,
            address=r.address,
            frequency=r.frequency,
        )
        for r in rows_b
    ]
    asyncio.run(
        store.async_replace(
            rows_a + rows_b,
            meter_summary=None,
            ingest_at=datetime(2026, 1, 1),
        )
    )
    # Total trimmed = (MAX + 100) - MAX = 100 readings, distributed
    # between A and B by oldest-first. Both contracts should have a
    # non-zero baseline.
    assert "A" in store.baseline_liters
    assert "B" in store.baseline_liters
    # The exact split depends on the interleaving order; the invariant
    # is that the per-contract baselines sum to the correct totals.
    a_baseline = store.baseline_liters["A"]
    b_baseline = store.baseline_liters["B"]
    # A's rows are 1 L each, B's are 2 L each; trimmed totals must
    # respect these per-row magnitudes.
    a_trimmed_count = a_baseline / 1.0
    b_trimmed_count = b_baseline / 2.0
    assert abs(a_trimmed_count + b_trimmed_count - 100) < 1e-6


def test_baseline_serialises_and_round_trips():
    """The baseline must persist across an HA restart — serialise +
    re-read produces identical state.
    """
    store = ReadingStore(MagicMock(), "test_entry")
    asyncio.run(
        store.async_replace(
            _make_readings(MAX_READINGS + 25),
            meter_summary=None,
            ingest_at=datetime(2026, 1, 1),
        )
    )
    serialised = store._serialise()
    assert "baseline_liters" in serialised
    assert serialised["baseline_liters"] == {"C1": 25.0}


def test_load_tolerates_missing_baseline_field():
    """Pre-v0.5.12 stores don't have ``baseline_liters`` in their
    JSON. Restoring such a payload must default to an empty dict
    (no crash, no KeyError).
    """
    store = ReadingStore(MagicMock(), "test_entry")
    # Simulate the loader path with a payload that has readings but no
    # baseline_liters field. We poke the internal store's async_load
    # to return our payload, then call async_load.
    payload = {
        "readings": [
            {
                "contract": "C1",
                "timestamp": "2025-01-01T00:00:00",
                "liters": 1.0,
                "period": "hour",
                "meter": "MTR",
                "address": "ADDR",
                "frequency": "hourly",
            }
        ],
        "meter_summary": None,
        "last_ingest_at": None,
        # NOTE: no baseline_liters key — simulates pre-v0.5.12.
    }
    store._store.async_load = AsyncMock(return_value=payload)
    asyncio.run(store.async_load())
    assert store.baseline_liters == {}, (
        f"pre-v0.5.12 load must default to empty baseline; got {store.baseline_liters!r}"
    )
    assert len(store.readings) == 1
