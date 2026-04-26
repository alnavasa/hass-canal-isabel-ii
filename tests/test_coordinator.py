"""Coverage for ``CanalCoordinator``'s store delegation.

The coordinator is intentionally thin — it delegates everything to
the per-entry ``ReadingStore`` so there's a single source of truth
for cached state. That delegation is the API the sensors rely on:

* ``coordinator.meter_summary`` → ``store.meter_summary``
* ``coordinator.baseline_liters`` → ``store.baseline_liters``
* ``coordinator._async_update_data()`` → ``store.readings``

If a refactor accidentally inlines a ``self._meter_summary`` field
onto the coordinator (instead of delegating), sensors will read stale
data after an ingest POST — invisible until the user notices the
counter has stopped advancing on the entity card while the recorder
keeps importing fresh stats. Same with ``baseline_liters``: a stale
copy on the coordinator would freeze the cumulative-consumption
sensor across cache trims (the v0.5.12 regression).

This test stubs the bare minimum of HA so we can instantiate
``DataUpdateCoordinator`` without booting a fixture: a no-op
``HomeAssistant`` and a stand-in ``DataUpdateCoordinator`` whose
``__init__`` accepts the kwargs ours passes. The coordinator's logic
is entirely in the property/method bodies we own — the HA superclass
contributes nothing we exercise here.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os as _os
import sys as _sys
import types as _types
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock


def _install_stubs() -> None:
    if "homeassistant" not in _sys.modules:
        _sys.modules["homeassistant"] = _types.ModuleType("homeassistant")

    if "homeassistant.core" not in _sys.modules:
        core = _types.ModuleType("homeassistant.core")
        core.HomeAssistant = MagicMock
        _sys.modules["homeassistant.core"] = core

    if "homeassistant.config_entries" not in _sys.modules:
        ce = _types.ModuleType("homeassistant.config_entries")
        ce.ConfigEntry = MagicMock
        _sys.modules["homeassistant.config_entries"] = ce

    if "homeassistant.helpers" not in _sys.modules:
        _sys.modules["homeassistant.helpers"] = _types.ModuleType("homeassistant.helpers")

    if "homeassistant.helpers.storage" not in _sys.modules:
        # ``coordinator`` imports ``.store`` which imports
        # ``homeassistant.helpers.storage.Store``. We expose ``saved``
        # and ``removed`` attributes on the stub so test files that
        # share this stubbed module (sys.modules is process-global —
        # the first test to install wins) can still inspect Store
        # behaviour. Concretely: ``test_store_extras`` asserts
        # ``store._store.removed is True`` after ``async_clear``; if
        # ``test_coordinator`` runs first and installs a leaner stub,
        # that assertion blows up with AttributeError. Keep the shape
        # rich enough for every consumer.
        storage = _types.ModuleType("homeassistant.helpers.storage")

        class _StubStore:
            def __init__(self, *a, **kw):
                self.saved: dict | None = None
                self.removed = False

            async def async_load(self):
                return None

            async def async_save(self, data):
                self.saved = data

            async def async_remove(self):
                self.saved = None
                self.removed = True

        storage.Store = _StubStore
        _sys.modules["homeassistant.helpers.storage"] = storage

    if "homeassistant.helpers.update_coordinator" not in _sys.modules:
        uc = _types.ModuleType("homeassistant.helpers.update_coordinator")

        class _DataUpdateCoordinator:
            """No-op stand-in. We only need the constructor signature
            to absorb the kwargs we pass — the real class's machinery
            (scheduling, listener fan-out) isn't exercised here."""

            def __init__(self, hass, logger, *, name, update_interval):
                self.hass = hass
                self.logger = logger
                self.name = name
                self.update_interval = update_interval

            def __class_getitem__(cls, _item):
                # Subscripting (DataUpdateCoordinator[list[Reading]])
                # used as a generic hint — return cls so the original
                # ``class CanalCoordinator(DataUpdateCoordinator[...])``
                # syntax works.
                return cls

        uc.DataUpdateCoordinator = _DataUpdateCoordinator
        _sys.modules["homeassistant.helpers.update_coordinator"] = uc


def _load_modules():
    repo = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src_dir = repo / "custom_components" / "canal_isabel_ii"

    pkg_name = "_canal_isabel_ii_coordinator_test"
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

    return _load("const"), _load("models"), _load("coordinator")


_install_stubs()
_const, _models, _coord_mod = _load_modules()
CanalCoordinator = _coord_mod.CanalCoordinator
Reading = _models.Reading
MeterSummary = _models.MeterSummary


def _fake_reading(hour: int) -> Reading:
    return Reading(
        contract="C1",
        timestamp=datetime(2026, 1, 1, hour),
        liters=1.0,
        period="hour",
        meter="MTR",
        address="ADDR",
        frequency="hourly",
    )


def _fake_store(*, readings=None, meter=None, baseline=None) -> MagicMock:
    """A store stand-in with just the three properties the coordinator reads."""
    store = MagicMock()
    type(store).readings = property(lambda self: readings or [])
    type(store).meter_summary = property(lambda self: meter)
    type(store).baseline_liters = property(lambda self: dict(baseline or {}))
    return store


def _make_coord(store) -> CanalCoordinator:
    hass = MagicMock()
    entry = MagicMock()
    entry.title = "test entry"
    return CanalCoordinator(hass, entry, store)


def test_meter_summary_property_delegates_to_store():
    ms = MeterSummary(123.0, datetime(2026, 1, 1), "M", "A", "123")
    coord = _make_coord(_fake_store(meter=ms))
    assert coord.meter_summary is ms


def test_meter_summary_returns_none_when_store_has_none():
    coord = _make_coord(_fake_store(meter=None))
    assert coord.meter_summary is None


def test_baseline_liters_property_delegates_to_store():
    coord = _make_coord(_fake_store(baseline={"A": 12.5, "B": 7.5}))
    assert coord.baseline_liters == {"A": 12.5, "B": 7.5}


def test_baseline_liters_property_returns_a_copy_via_store():
    """The store returns a copy already; verify the coordinator passes
    that contract through (mutating the result must not affect the
    next read).
    """
    store = _fake_store(baseline={"A": 1.0})
    coord = _make_coord(store)
    snapshot = coord.baseline_liters
    snapshot["A"] = 999.0  # mutate
    # Next read still returns the original value (because the property
    # rebuilds from the store each call).
    assert coord.baseline_liters == {"A": 1.0}


def test_async_update_data_returns_store_readings():
    """The coordinator's update returns whatever the store currently has —
    no fetch, no transformation, no I/O.
    """
    rs = [_fake_reading(0), _fake_reading(1), _fake_reading(2)]
    coord = _make_coord(_fake_store(readings=rs))
    out = asyncio.run(coord._async_update_data())
    assert out == rs


def test_async_update_data_returns_empty_list_on_fresh_store():
    """A fresh store with no readings → empty list, not None or error."""
    coord = _make_coord(_fake_store(readings=[]))
    out = asyncio.run(coord._async_update_data())
    assert out == []


def test_coordinator_keeps_reference_to_entry_and_store():
    """The coordinator exposes ``entry`` and ``store`` for service
    handlers (``clear_cost_stats``, ``reset_meter``) to reach in."""
    store = _fake_store()
    coord = _make_coord(store)
    assert coord.store is store
    assert coord.entry is not None
