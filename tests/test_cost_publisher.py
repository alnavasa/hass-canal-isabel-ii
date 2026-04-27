"""End-to-end coverage for ``cost_publisher.publish_cost_stream`` (v0.6.0+).

## Why this test file

v0.6.0 split cost out of the entity layer and into a pure publisher
called from ingest.py. The publisher's branching logic — disabled,
no-readings, vigencia-error, cold-start, replay-with-merge — used to
live inside ``CanalCumulativeCostSensor.async_update`` and was tested
indirectly through that entity's tests (which needed the full HA
runtime). The publisher is now standalone and *should* be testable
without spinning up HA at all.

This file does that: it stubs ``homeassistant.components.recorder``
(plus ``util.dt``) just enough that ``cost_publisher.py`` imports
cleanly, then drives ``publish_cost_stream`` with a fake ``hass`` that
captures every recorder call. The tests assert against what was
captured, which gives us:

1. **Disabled** — early return, no recorder writes.
2. **No readings** — early return, no recorder writes.
3. **Vigencia gap** — ``compute_hourly_cost_stream`` raises ``ValueError``,
   the publisher logs and swallows. No recorder writes.
4. **Malformed cost_settings** — ``tariff_params_from_settings`` raises,
   publisher logs and swallows. No recorder writes.
5. **Cold start** — no prior stats; pushes (state=cum, sum=cum) directly,
   one row per hour in the stream.
6. **Replay with prior stats** — ``get_last_statistics`` returns a row;
   publisher reads back through ``statistics_during_period``, merges
   with new deltas, replays from zero. The pushed sum series is
   monotonic non-decreasing.

The pure helpers (``cost_statistic_id``, ``cost_statistic_name``,
``tariff_params_from_settings``) are exercised at the bottom — trivial
but worth pinning so a future "let's prefix the stat id" change is
caught loudly.

## Why standalone importlib + stubs (and not the HA test harness)

``pytest-homeassistant-custom-component`` would let us spin up a real
HA instance, but it costs ~3 seconds per test and obscures what the
publisher is actually doing. The publisher's logic is "compute
deltas, read recorder, merge, push" — none of that needs a running
HA bus or aiohttp app. The stubs match the surface of the recorder API
(documented at https://developers.home-assistant.io/docs/core/entity/sensor#long-term-statistics)
exactly, and a future HA API rename would fail loudly here when the
function names stop matching.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os as _os
import sys as _sys
import types as _types
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------
# Stub HA modules that cost_publisher.py imports at module level.
# ---------------------------------------------------------------------


@dataclass
class _FakeStatisticData:
    """Mirror of ``homeassistant.components.recorder.models.StatisticData``.

    Production code uses keyword args (``StatisticData(start=..., state=...,
    sum=...)``) — a frozen dataclass with the same field names is enough
    for the publisher's call site. The captured rows are what the tests
    assert against; using a dataclass (not a MagicMock) means the
    assertions read naturally as ``rows[0].sum == 27.42``.
    """

    start: datetime
    state: float
    sum: float


@dataclass
class _FakeStatisticMetaData:
    """Mirror of ``StatisticMetaData`` — same rationale as above."""

    source: str
    statistic_id: str
    has_sum: bool
    name: str
    unit_of_measurement: str
    mean_type: Any
    unit_class: Any


def _install_stubs() -> None:
    """Install just-enough stubs that cost_publisher imports succeed.

    Idempotent: re-runs leave the existing stubs in place so a test
    that loads the module a second time picks up the same fakes.
    """
    if "homeassistant" not in _sys.modules:
        _sys.modules["homeassistant"] = _types.ModuleType("homeassistant")
    if "homeassistant.core" not in _sys.modules:
        core = _types.ModuleType("homeassistant.core")
        core.HomeAssistant = MagicMock
        # cost_publisher uses ``@callback`` nowhere, but other modules in
        # the test-package namespace might (defensive).
        core.callback = lambda f: f
        _sys.modules["homeassistant.core"] = core
    if "homeassistant.components" not in _sys.modules:
        _sys.modules["homeassistant.components"] = _types.ModuleType("homeassistant.components")

    # --- recorder ----
    if "homeassistant.components.recorder" not in _sys.modules:
        recorder = _types.ModuleType("homeassistant.components.recorder")
        # Replaced per-test by a fake that captures the hass argument.
        recorder.get_instance = MagicMock()
        _sys.modules["homeassistant.components.recorder"] = recorder
    if "homeassistant.components.recorder.models" not in _sys.modules:
        rec_models = _types.ModuleType("homeassistant.components.recorder.models")
        rec_models.StatisticData = _FakeStatisticData
        rec_models.StatisticMetaData = _FakeStatisticMetaData

        class _MeanType:
            NONE = "none"

        rec_models.StatisticMeanType = _MeanType
        _sys.modules["homeassistant.components.recorder.models"] = rec_models
    if "homeassistant.components.recorder.statistics" not in _sys.modules:
        rec_stats = _types.ModuleType("homeassistant.components.recorder.statistics")
        # Replaced per-test by capturing fakes — module-level placeholders so
        # the import inside cost_publisher succeeds.
        rec_stats.async_add_external_statistics = MagicMock()
        rec_stats.get_last_statistics = MagicMock(return_value={})
        rec_stats.statistics_during_period = MagicMock(return_value={})
        _sys.modules["homeassistant.components.recorder.statistics"] = rec_stats

    # --- util.dt ----
    if "homeassistant.util" not in _sys.modules:
        _sys.modules["homeassistant.util"] = _types.ModuleType("homeassistant.util")
    if "homeassistant.util.dt" not in _sys.modules:
        ha_dt = _types.ModuleType("homeassistant.util.dt")
        # cost_publisher uses ``DEFAULT_TIME_ZONE`` to localise naive
        # timestamps. UTC is fine for tests — we'll feed UTC datetimes
        # in directly so this assignment never bites.
        ha_dt.DEFAULT_TIME_ZONE = UTC
        ha_dt.UTC = UTC
        ha_dt.utc_from_timestamp = lambda ts: datetime.fromtimestamp(ts, tz=UTC)
        ha_dt.parse_datetime = datetime.fromisoformat
        _sys.modules["homeassistant.util.dt"] = ha_dt


def _load_cost_publisher():
    """Load cost_publisher under a fresh namespace and return the module.

    Same pattern as ``test_ingest_helpers._load_ingest_module``: we
    register a synthetic package that points at the real source dir,
    then exec each submodule into it so relative imports
    (``from .const import ...``) resolve to the test-package copy.
    """
    repo = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src_dir = repo / "custom_components" / "canal_isabel_ii"

    pkg_name = "_canal_cost_publisher_test"
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

    _load("const")
    _load("models")
    _load("statistics_helpers")
    _load("tariff")
    return _load("cost_publisher")


_install_stubs()
_pub = _load_cost_publisher()
_models = _sys.modules["_canal_cost_publisher_test.models"]
publish_cost_stream = _pub.publish_cost_stream
tariff_params_from_settings = _pub.tariff_params_from_settings
cost_statistic_id = _pub.cost_statistic_id
cost_statistic_name = _pub.cost_statistic_name
Reading = _models.Reading


# ---------------------------------------------------------------------
# Per-test fake hass + recorder.
# ---------------------------------------------------------------------


class _FakeRecorder:
    """Stand-in for ``homeassistant.components.recorder.Recorder``.

    The publisher only calls ``async_add_executor_job(fn, *args)`` on
    the recorder — it does NOT touch any other attribute. We satisfy
    that surface by running the function inline (synchronous; the test
    knows the executor jobs are pure). Production HA dispatches them
    to a thread pool, but the result is identical for these jobs.
    """

    def __init__(self) -> None:
        self.jobs: list[tuple[Any, tuple[Any, ...]]] = []

    async def async_add_executor_job(self, fn, *args):
        self.jobs.append((fn, args))
        return fn(*args)


class _FakeHass:
    """Tiny ``HomeAssistant`` stand-in.

    Only the attributes the publisher reads are present. ``config.currency``
    drives the statistic's unit; the recorder is set per-test.
    """

    def __init__(self, currency: str = "EUR") -> None:
        self.config = _types.SimpleNamespace(currency=currency)


def _patch_recorder(
    hass: _FakeHass,
    *,
    last_stats: dict[str, list[dict[str, Any]]] | None = None,
    period_stats: dict[str, list[dict[str, Any]]] | None = None,
):
    """Wire a fresh fake recorder + capture lists into the publisher's bindings.

    Returns the (recorder, captured_external) pair so the test can:
      - inspect ``recorder.jobs`` for the executor calls,
      - inspect ``captured_external`` for what the publisher pushed.

    Important: ``cost_publisher`` does ``from homeassistant.components.recorder
    import get_instance as get_recorder_instance`` (and similar for the four
    statistics helpers) at module load. Those names are snapshotted into the
    publisher's namespace before any test runs — patching the upstream stub
    module would NOT update the bound names. So we patch the publisher
    module attributes directly. Tests run sequentially in pytest, so the
    rebinds are safe.
    """
    recorder = _FakeRecorder()

    def get_last_statistics(h, n, sid, convert, kinds):
        return dict(last_stats or {})

    def statistics_during_period(h, start, end, ids, period, units, kinds):
        return dict(period_stats or {})

    _pub.get_recorder_instance = lambda h: recorder
    _pub.get_last_statistics = get_last_statistics
    _pub.statistics_during_period = statistics_during_period

    captured: list[tuple[_FakeStatisticMetaData, list[_FakeStatisticData]]] = []

    def _capture(h, metadata, stats):
        captured.append((metadata, list(stats)))

    _pub.async_add_external_statistics = _capture
    return recorder, captured


def _run(coro):
    """Drive an awaitable to completion in a fresh event loop.

    The publisher is async but does nothing event-loop-y beyond awaiting
    the recorder's executor job — which our fake runs inline. A fresh
    loop per test keeps the harness clean and avoids leftover state from
    a previous test's loop closing late.
    """
    return asyncio.new_event_loop().run_until_complete(coro)


def _reading(contract: str, ts: datetime, liters: float) -> Reading:
    """Helper to spell out a Reading with the irrelevant fields stubbed.

    The publisher only reads ``contract``, ``timestamp``, ``liters`` —
    period/meter/address/frequency exist on the dataclass for other
    callers but are unused here.
    """
    return Reading(
        contract=contract,
        timestamp=ts,
        liters=liters,
        period="",
        meter="",
        address="",
        frequency="",
    )


# ---------------------------------------------------------------------
# Default cost settings — covers the most common Doméstico-1-vivienda
# install. Each test overrides the keys it cares about.
# ---------------------------------------------------------------------

_GOOD_SETTINGS: dict[str, Any] = {
    "enable_cost": True,
    "diametro_mm": 15,
    "n_viviendas": 1,
    "cuota_supl_alc_eur_m3": 0.10,
    "iva_pct": 10.0,
}


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


def test_disabled_settings_skip_push():
    """``enable_cost = False`` → publisher returns immediately.

    Users who don't want cost (the default) must pay zero overhead per
    POST. The test asserts the publisher never even constructs a
    ``StatisticMetaData`` — i.e. ``async_add_external_statistics`` is
    never called.
    """
    hass = _FakeHass()
    _, captured = _patch_recorder(hass)
    settings = {**_GOOD_SETTINGS, "enable_cost": False}
    _run(
        publish_cost_stream(
            hass,
            entry_id="e1",
            contract_id="C1",
            install_name="MF1",
            cost_settings=settings,
            readings=[_reading("C1", datetime(2026, 4, 1, 12, 0, tzinfo=UTC), 100.0)],
        )
    )
    assert captured == []


def test_empty_readings_skip_push():
    """No readings for the requested contract → no push.

    Boot-time republish runs even before the first POST; the store can
    legitimately have zero readings or no rows for this contract. The
    publisher must short-circuit without tripping any recorder call.
    """
    hass = _FakeHass()
    _, captured = _patch_recorder(hass)
    _run(
        publish_cost_stream(
            hass,
            entry_id="e1",
            contract_id="C1",
            install_name="MF1",
            cost_settings=_GOOD_SETTINGS,
            readings=[],
        )
    )
    assert captured == []


def test_no_matching_contract_skips_push():
    """All readings belong to other contracts → no push.

    A multi-contrato store can hold C1 + C2 readings; the publisher
    runs once per contract and must not push C1's stat when only C2
    rows exist.
    """
    hass = _FakeHass()
    _, captured = _patch_recorder(hass)
    other = _reading("C2", datetime(2026, 4, 1, 12, 0, tzinfo=UTC), 100.0)
    _run(
        publish_cost_stream(
            hass,
            entry_id="e1",
            contract_id="C1",
            install_name="MF1",
            cost_settings=_GOOD_SETTINGS,
            readings=[other],
        )
    )
    assert captured == []


def test_malformed_settings_swallowed_no_push():
    """A missing required key triggers ``KeyError`` → swallowed, no push.

    The publisher must never propagate exceptions to the ingest endpoint;
    the HTTP POST that triggered it has already succeeded by this point
    and a downstream cost issue should at most delay the next refresh.
    """
    hass = _FakeHass()
    _, captured = _patch_recorder(hass)
    bad = {"enable_cost": True}  # diametro_mm etc. missing
    _run(
        publish_cost_stream(
            hass,
            entry_id="e1",
            contract_id="C1",
            install_name="MF1",
            cost_settings=bad,
            readings=[_reading("C1", datetime(2026, 4, 1, 12, 0, tzinfo=UTC), 100.0)],
        )
    )
    assert captured == []


def test_pre_vigencia_readings_swallowed_no_push():
    """A reading older than the earliest known vigencia raises
    ``ValueError`` from ``compute_hourly_cost_stream`` → swallowed.

    Real users hit this if they backfill January 2024 readings while
    the integration only models 2025+2026 vigencias. The publisher
    should log a warning and skip — pushing a partial series that
    omits the unknown range would be worse than no push.
    """
    hass = _FakeHass()
    _, captured = _patch_recorder(hass)
    # 2020 is before any modelled vigencia.
    too_old = _reading("C1", datetime(2020, 1, 1, 0, 0, tzinfo=UTC), 100.0)
    _run(
        publish_cost_stream(
            hass,
            entry_id="e1",
            contract_id="C1",
            install_name="MF1",
            cost_settings=_GOOD_SETTINGS,
            readings=[too_old],
        )
    )
    assert captured == []


def test_cold_start_pushes_full_stream_with_state_equal_sum():
    """First push (no prior stats) → ``state = sum = cumulative_eur`` for every row.

    The cold-start branch bypasses the merge path entirely. We assert:

    * exactly one ``async_add_external_statistics`` call,
    * the metadata's ``statistic_id`` matches ``cost_statistic_id``,
    * each pushed row has ``state == sum`` (the publisher writes the
      cumulative directly; both fields exist for compatibility with HA's
      stat schema), and
    * the sum series is monotonic non-decreasing (cost stream is
      monotonic by construction).
    """
    hass = _FakeHass()
    _, captured = _patch_recorder(hass)  # last_stats={} → cold start
    # Two consecutive hours of consumption, both inside the 2026 vigencia.
    rows = [
        _reading("C1", datetime(2026, 4, 1, 10, 0, tzinfo=UTC), 50.0),
        _reading("C1", datetime(2026, 4, 1, 11, 0, tzinfo=UTC), 100.0),
    ]
    _run(
        publish_cost_stream(
            hass,
            entry_id="e1",
            contract_id="C1",
            install_name="MF1",
            cost_settings=_GOOD_SETTINGS,
            readings=rows,
        )
    )
    assert len(captured) == 1, "cold start must push exactly once"
    metadata, stats = captured[0]
    assert metadata.statistic_id == "canal_isabel_ii:cost_C1"
    assert metadata.unit_of_measurement == "EUR"
    assert metadata.has_sum is True
    assert stats, "cold start must push at least one row"
    for row in stats:
        assert row.state == row.sum, "cold start writes cumulative directly to both state and sum"
    sums = [r.sum for r in stats]
    assert sums == sorted(sums), "cumulative cost is monotonic by construction"


def test_replay_with_prior_stats_calls_period_query_and_pushes():
    """Push when a prior stat exists → publisher reads back the period and merges.

    We assert the publisher:

    * called ``get_last_statistics`` (queried for the anchor),
    * called ``statistics_during_period`` (read back to merge),
    * called ``async_add_external_statistics`` once (pushed the merged series),
    * pushed a monotonic non-decreasing sum series.

    The exact merge math is covered by ``test_continuation_stats``'
    ``TestCostPushPipelineMonotonicity``; this test only verifies the
    publisher reaches the right code path when prior stats exist.
    """
    hass = _FakeHass()
    # A prior stat anchored at 09:00 — the publisher will read back to 09:00
    # for any new item dated 10:00 or later.
    last_anchor = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
    last_stats = {
        "canal_isabel_ii:cost_C1": [
            {"start": last_anchor.isoformat(), "sum": 0.5},
        ],
    }
    period_stats = {
        "canal_isabel_ii:cost_C1": [
            {"start": last_anchor.isoformat(), "sum": 0.5},
        ],
    }
    rec, captured = _patch_recorder(hass, last_stats=last_stats, period_stats=period_stats)

    rows = [
        _reading("C1", datetime(2026, 4, 1, 10, 0, tzinfo=UTC), 50.0),
        _reading("C1", datetime(2026, 4, 1, 11, 0, tzinfo=UTC), 100.0),
    ]
    _run(
        publish_cost_stream(
            hass,
            entry_id="e1",
            contract_id="C1",
            install_name="MF1",
            cost_settings=_GOOD_SETTINGS,
            readings=rows,
        )
    )

    # Two executor jobs: one for get_last_statistics, one for
    # statistics_during_period. The order matters because the period
    # query depends on the anchor.
    fns = [j[0].__name__ for j in rec.jobs]
    assert fns == ["get_last_statistics", "statistics_during_period"], (
        f"expected exactly the recorder anchor + period queries, got: {fns}"
    )

    assert len(captured) == 1, "replay must push exactly once"
    _meta, stats = captured[0]
    sums = [r.sum for r in stats]
    assert sums == sorted(sums), "merged replay must be monotonic non-decreasing"


# ---------------------------------------------------------------------
# Pure helpers — trivial but worth pinning so a stat-id rename triggers a
# loud failure (the rename would orphan every existing Energy panel that
# references the old id).
# ---------------------------------------------------------------------


def test_cost_statistic_id_format_is_stable():
    """The format ``canal_isabel_ii:cost_<contract>`` is part of the public
    contract: any user who picked the stat in the Energy panel pre-rename
    would lose their bars on rename. Pin it."""
    assert cost_statistic_id("ABCDEF") == "canal_isabel_ii:cost_ABCDEF"


def test_cost_statistic_name_uses_install_label():
    """The display name embeds the install label so multi-instance setups
    (MF1 + GH4) show up as distinct stats in the picker."""
    assert cost_statistic_name("MF1") == "MF1 - Canal de Isabel II coste"


def test_tariff_params_from_settings_passes_through():
    """The settings → params translator is a thin coerce; pin field
    spellings so a future config-key rename breaks here, not silently."""
    p = tariff_params_from_settings(_GOOD_SETTINGS)
    assert p.diametro_mm == 15
    assert p.n_viviendas == 1
    assert abs(p.cuota_supl_alc_eur_m3 - 0.10) < 1e-9
    assert abs(p.iva_pct - 10.0) < 1e-9
