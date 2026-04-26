"""Coverage extension for ``ReadingStore`` beyond the v0.5.12 baseline tests.

``test_store_baseline.py`` already covers the cache-trim baseline
carry-over (the v0.5.12 fix). This file fills the remaining gaps so a
future refactor of the store can't silently break:

* **Dedup semantics** — re-ingesting the same ``(contract, timestamp)``
  must overwrite, not duplicate (the bookmarklet always re-fetches the
  full visible window, so every POST contains overlap with the
  previous one).
* **Meter-summary preservation** — a POST without ``meter_summary``
  must not erase a previously stored one (rare cache-only fast paths
  in the bookmarklet would otherwise wipe the absolute counter).
* **``last_ingest_at`` always advances** — sensors expose this as the
  freshness attribute; if it ever sticks the user can't tell whether
  a POST landed.
* **``async_clear`` wipes everything**, including ``baseline_liters``
  and the on-disk file — used by ``async_remove_entry`` so the next
  install starts from a clean slate.
* **``async_reset_baseline`` wipes one contract's baseline only** —
  used by the v0.5.16 ``reset_meter`` service when the user has
  physically swapped the meter; readings, meter summary and other
  contracts must stay untouched.
* **``contracts`` property** — the ingest endpoint and the
  ``clear_cost_stats`` service iterate this to know which sensors to
  refresh.
* **Meter-summary round-trip** — including a non-trivial
  ``reading_at`` so the loader's datetime parser is exercised.
* **``_meter_summary_from_dict`` and ``_reading_from_dict`` malformed
  input tolerance** — the loader uses these and is expected to skip
  bad rows rather than crash on a corrupted store file.

All tests reuse the HA-storage stubs from ``test_store_baseline.py``
(via importing the loader) so they don't pull HA into the test deps.
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


# Reuse the same stubbing pattern as test_store_baseline so we don't
# need to import HA. Kept inline (not imported from the sibling test)
# so this file can be deleted/moved without breaking the other.
def _install_ha_stubs() -> None:
    if "homeassistant" not in _sys.modules:
        _sys.modules["homeassistant"] = _types.ModuleType("homeassistant")
    if "homeassistant.core" not in _sys.modules:
        core = _types.ModuleType("homeassistant.core")
        core.HomeAssistant = MagicMock
        _sys.modules["homeassistant.core"] = core
    if "homeassistant.helpers" not in _sys.modules:
        _sys.modules["homeassistant.helpers"] = _types.ModuleType("homeassistant.helpers")
    if "homeassistant.helpers.storage" not in _sys.modules:
        storage = _types.ModuleType("homeassistant.helpers.storage")

        class _StubStore:
            def __init__(self, *args, **kwargs):
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


def _load_modules():
    repo = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src_dir = repo / "custom_components" / "canal_isabel_ii"

    pkg_name = "_canal_isabel_ii_store_extras_test"
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

    return _load("const"), _load("models"), _load("store")


_install_ha_stubs()
_const, _models, _store_mod = _load_modules()
ReadingStore = _store_mod.ReadingStore
Reading = _models.Reading
MeterSummary = _models.MeterSummary
_meter_summary_from_dict = _store_mod._meter_summary_from_dict
_reading_from_dict = _store_mod._reading_from_dict


_T0 = datetime(2026, 1, 1, 0, 0, 0)


def _r(contract: str, hour: int, liters: float = 1.0) -> Reading:
    """Build a reading at ``_T0 + hour hours`` — works for any
    non-negative ``hour`` (we pass values up to MAX_READINGS_PER_ENTRY
    in the trim tests).
    """
    return Reading(
        contract=contract,
        timestamp=_T0 + timedelta(hours=hour),
        liters=liters,
        period="hour",
        meter="MTR",
        address="ADDR",
        frequency="hourly",
    )


# ---------------------------------------------------------------------
# Dedup + merge
# ---------------------------------------------------------------------


def test_async_replace_returns_count_of_new_slots_only():
    """First POST: 3 NEW. Second POST overlaps 2 of them: only 1 NEW."""
    store = ReadingStore(MagicMock(), "e1")
    first = [_r("C1", 0), _r("C1", 1), _r("C1", 2)]
    second = [_r("C1", 1), _r("C1", 2), _r("C1", 3)]  # overlap on hours 1,2

    n1 = asyncio.run(store.async_replace(first, None, datetime(2026, 1, 1)))
    n2 = asyncio.run(store.async_replace(second, None, datetime(2026, 1, 1, 1)))
    assert n1 == 3
    assert n2 == 1, f"expected 1 NEW slot in the second POST, got {n2}"
    assert len(store.readings) == 4  # hours 0,1,2,3


def test_async_replace_overwrites_in_place_for_existing_slot():
    """Re-ingesting the same (contract, timestamp) updates liters in place.

    Canal occasionally publishes a corrected reading for a past hour;
    the bookmarklet's next POST must update the cached value, not
    duplicate the row.
    """
    store = ReadingStore(MagicMock(), "e1")
    asyncio.run(store.async_replace([_r("C1", 5, liters=10.0)], None, datetime(2026, 1, 1)))
    asyncio.run(store.async_replace([_r("C1", 5, liters=42.0)], None, datetime(2026, 1, 1, 1)))
    assert len(store.readings) == 1
    assert store.readings[0].liters == 42.0


# ---------------------------------------------------------------------
# Meter summary
# ---------------------------------------------------------------------


def test_meter_summary_preserved_when_subsequent_post_omits_it():
    """A POST without meter_summary must NOT erase the previous one."""
    store = ReadingStore(MagicMock(), "e1")
    ms = MeterSummary(
        reading_liters=1234.5,
        reading_at=datetime(2026, 1, 1, 12),
        meter="MTR",
        address="ADDR",
        raw_reading="1,234.5 m³",
    )
    asyncio.run(store.async_replace([_r("C1", 0)], ms, datetime(2026, 1, 1)))
    asyncio.run(store.async_replace([_r("C1", 1)], None, datetime(2026, 1, 1, 1)))
    assert store.meter_summary is ms, "meter_summary lost when POST omitted it"


def test_meter_summary_replaced_when_subsequent_post_includes_it():
    """A new meter_summary in a POST replaces the old one wholesale."""
    store = ReadingStore(MagicMock(), "e1")
    ms_old = MeterSummary(100.0, datetime(2026, 1, 1), "M", "A", "100m³")
    ms_new = MeterSummary(200.0, datetime(2026, 1, 2), "M", "A", "200m³")
    asyncio.run(store.async_replace([_r("C1", 0)], ms_old, datetime(2026, 1, 1)))
    asyncio.run(store.async_replace([_r("C1", 1)], ms_new, datetime(2026, 1, 2)))
    assert store.meter_summary is ms_new


# ---------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------


def test_last_ingest_at_always_advances():
    """Two POSTs in a row → last_ingest_at must reflect the second one."""
    store = ReadingStore(MagicMock(), "e1")
    t1 = datetime(2026, 1, 1, 10)
    t2 = datetime(2026, 1, 1, 11)
    asyncio.run(store.async_replace([_r("C1", 0)], None, t1))
    asyncio.run(store.async_replace([_r("C1", 1)], None, t2))
    assert store.last_ingest_at == t2


# ---------------------------------------------------------------------
# Contracts property
# ---------------------------------------------------------------------


def test_contracts_property_returns_unique_set():
    """``contracts`` skips empty contract ids and dedups."""
    store = ReadingStore(MagicMock(), "e1")
    asyncio.run(
        store.async_replace(
            [
                _r("A", 0),
                _r("A", 1),  # dup contract
                _r("B", 2),
                _r("", 3),  # empty contract — must be skipped
            ],
            None,
            datetime(2026, 1, 1),
        )
    )
    assert store.contracts == {"A", "B"}


# ---------------------------------------------------------------------
# Clear (entry removal)
# ---------------------------------------------------------------------


def test_async_clear_wipes_everything_and_removes_file():
    """async_clear must drop readings + meter + baseline + on-disk file."""
    store = ReadingStore(MagicMock(), "e1")
    asyncio.run(
        store.async_replace(
            [_r("C1", h) for h in range(_const.MAX_READINGS_PER_ENTRY + 5)],
            MeterSummary(1.0, None, "M", "A", "1"),
            datetime(2026, 1, 1),
        )
    )
    assert store.readings  # populated
    assert store.meter_summary
    assert store.baseline_liters  # trim happened
    assert store.last_ingest_at

    asyncio.run(store.async_clear())

    assert store.readings == []
    assert store.meter_summary is None
    assert store.last_ingest_at is None
    assert store.baseline_liters == {}
    # The stub Store records removal — verify we called it.
    assert store._store.removed is True


# ---------------------------------------------------------------------
# Reset baseline (v0.5.16 reset_meter service)
# ---------------------------------------------------------------------


def test_reset_baseline_drops_one_contract_only():
    """async_reset_baseline must wipe ONE contract's baseline,
    leaving readings, meter summary and other contracts intact.
    """
    store = ReadingStore(MagicMock(), "e1")
    # Force a trim so both contracts have a non-zero baseline.
    rows = []
    for h in range(_const.MAX_READINGS_PER_ENTRY // 2 + 50):
        rows.append(_r("A", h))
    for h in range(_const.MAX_READINGS_PER_ENTRY // 2 + 50):
        # B's hours offset so global timestamp sort interleaves both.
        rows.append(
            Reading(
                contract="B",
                timestamp=datetime(2026, 1, 1, 0) + timedelta(minutes=30 + h * 60),
                liters=2.0,
                period="hour",
                meter="MTR",
                address="ADDR",
                frequency="hourly",
            )
        )
    ms = MeterSummary(99.0, None, "M", "A", "99")
    asyncio.run(store.async_replace(rows, ms, datetime(2026, 2, 1)))
    assert "A" in store.baseline_liters
    assert "B" in store.baseline_liters
    readings_before = len(store.readings)
    b_baseline_before = store.baseline_liters["B"]

    asyncio.run(store.async_reset_baseline("A"))

    assert "A" not in store.baseline_liters, "A's baseline should be gone"
    assert store.baseline_liters["B"] == b_baseline_before, (
        "B's baseline must not change when A is reset"
    )
    assert len(store.readings) == readings_before, (
        "readings must not be touched by async_reset_baseline"
    )
    assert store.meter_summary is ms, "meter summary must survive reset_baseline"


def test_reset_baseline_unknown_contract_is_noop():
    """Resetting a contract that has no baseline is a no-op (no crash)."""
    store = ReadingStore(MagicMock(), "e1")
    # Persist write tracker — async_save is what hits disk.
    store._store.saved = None
    asyncio.run(store.async_reset_baseline("never-seen-contract"))
    # No baseline change, no save call (nothing to persist).
    assert store.baseline_liters == {}
    assert store._store.saved is None, (
        "reset_baseline on an unknown contract must not write to disk"
    )


# ---------------------------------------------------------------------
# Round-trip with meter_summary
# ---------------------------------------------------------------------


def test_serialise_round_trip_includes_meter_summary():
    """Serialise + reload reconstructs the meter summary identically."""
    store = ReadingStore(MagicMock(), "e1")
    ms = MeterSummary(
        reading_liters=56735.0,
        reading_at=datetime(2026, 4, 22, 3, 0, 0),
        meter="Y20HK123456",
        address="C/ Test 1",
        raw_reading="56,735 m³",
    )
    asyncio.run(store.async_replace([_r("C1", 0)], ms, datetime(2026, 4, 22)))

    serialised = store._serialise()
    # Push it back through async_load on a fresh store.
    store2 = ReadingStore(MagicMock(), "e1")
    store2._store.async_load = AsyncMock(return_value=serialised)
    asyncio.run(store2.async_load())

    assert store2.meter_summary is not None
    assert store2.meter_summary.reading_liters == ms.reading_liters
    assert store2.meter_summary.reading_at == ms.reading_at
    assert store2.meter_summary.meter == ms.meter
    assert store2.meter_summary.address == ms.address
    assert store2.meter_summary.raw_reading == ms.raw_reading
    # Reading round-trip too.
    assert len(store2.readings) == 1
    assert store2.readings[0].timestamp == datetime(2026, 1, 1, 0)


# ---------------------------------------------------------------------
# Free-function (de)serialiser tolerance — corrupted store files
# ---------------------------------------------------------------------


def test_meter_summary_from_dict_returns_none_on_garbage():
    """Anything other than a well-formed dict yields None, not a crash."""
    assert _meter_summary_from_dict(None) is None
    assert _meter_summary_from_dict("not a dict") is None
    assert _meter_summary_from_dict({}) is None  # missing reading_liters
    assert _meter_summary_from_dict({"reading_liters": "not a number"}) is None


def test_meter_summary_from_dict_tolerates_bad_reading_at():
    """A garbage ``reading_at`` falls back to None instead of crashing."""
    out = _meter_summary_from_dict(
        {
            "reading_liters": 100.0,
            "reading_at": "not an iso timestamp",
            "meter": "M",
            "address": "A",
            "raw_reading": "100",
        }
    )
    assert out is not None
    assert out.reading_at is None
    assert out.reading_liters == 100.0


def test_reading_from_dict_raises_on_missing_timestamp():
    """``_reading_from_dict`` is expected to raise on bad input — the
    loader's caller is the one that catches and skips. This pins the
    raise surface so the caller's try/except keeps working.
    """
    # No ``timestamp`` key → KeyError when accessed.
    try:
        _reading_from_dict({"contract": "C1", "liters": 1.0})
    except (KeyError, TypeError, ValueError):
        return  # expected
    raise AssertionError("expected KeyError/TypeError/ValueError on missing timestamp")


def test_reading_from_dict_raises_on_garbage_timestamp():
    """A non-ISO timestamp must raise so the loader skips the row."""
    try:
        _reading_from_dict(
            {
                "contract": "C1",
                "timestamp": "not-an-iso-date",
                "liters": 1.0,
                "period": "hour",
                "meter": "M",
                "address": "A",
                "frequency": "hourly",
            }
        )
    except (KeyError, TypeError, ValueError):
        return
    raise AssertionError("expected ValueError on bad timestamp")


def test_load_skips_corrupted_reading_rows():
    """``async_load`` must skip rows that fail to parse, keeping the
    rest. Models real-world corruption (a half-flushed JSON write).
    """
    store = ReadingStore(MagicMock(), "e1")
    payload = {
        "readings": [
            # Good row.
            {
                "contract": "C1",
                "timestamp": "2026-01-01T00:00:00",
                "liters": 1.0,
                "period": "hour",
                "meter": "MTR",
                "address": "ADDR",
                "frequency": "hourly",
            },
            # Bad timestamp — must be skipped.
            {
                "contract": "C1",
                "timestamp": "garbage",
                "liters": 2.0,
                "period": "hour",
                "meter": "MTR",
                "address": "ADDR",
                "frequency": "hourly",
            },
            # Missing timestamp — must be skipped.
            {
                "contract": "C1",
                "liters": 3.0,
            },
            # Another good row.
            {
                "contract": "C1",
                "timestamp": "2026-01-01T01:00:00",
                "liters": 4.0,
                "period": "hour",
                "meter": "MTR",
                "address": "ADDR",
                "frequency": "hourly",
            },
        ],
        "meter_summary": None,
        "last_ingest_at": None,
        "baseline_liters": {},
    }
    store._store.async_load = AsyncMock(return_value=payload)
    asyncio.run(store.async_load())
    # 2 good rows survived; 2 garbage rows skipped.
    assert len(store.readings) == 2
    assert {r.liters for r in store.readings} == {1.0, 4.0}


def test_load_tolerates_garbage_baseline_field():
    """If ``baseline_liters`` is present but malformed (not a dict, or
    contains non-numeric values), the loader must default to empty for
    the bad bits — never crash on a corrupted store."""
    store = ReadingStore(MagicMock(), "e1")
    payload = {
        "readings": [],
        "meter_summary": None,
        "last_ingest_at": None,
        # Mix of good and bad entries.
        "baseline_liters": {"good": 12.5, "bad": "not a number", "also-good": 7.5},
    }
    store._store.async_load = AsyncMock(return_value=payload)
    asyncio.run(store.async_load())
    assert store.baseline_liters == {"good": 12.5, "also-good": 7.5}
