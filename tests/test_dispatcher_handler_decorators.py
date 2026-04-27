"""Regression guard for ``@callback`` on dispatcher handlers (v0.5.23+).

## Why this test exists

v0.5.22 wired dispatcher signals into sync entity methods so that
running services (``reset_meter``, and previously ``clear_cost_stats``)
could clear in-memory monotonic guards. The recovery worked end-to-end
on MF1 — the Energy panel recovered from 0 to the real value — **but**
the HA log filled with ERROR tracebacks::

    ERROR (SyncWorker_2) Exception in _on_meter_reset:
    RuntimeError: Detected that custom integration 'canal_isabel_ii'
      calls async_write_ha_state from a thread other than the event
      loop ... at custom_components/canal_isabel_ii/sensor.py, line 917

Why: dispatcher handlers are sync methods. Without ``@callback`` HA's
dispatcher schedules them on the executor thread pool, and the
trailing ``async_write_ha_state()`` call then trips HA 2026.x's
thread-safety guard (``report_non_thread_safe_operation``, which now
raises ``RuntimeError`` instead of just logging a warning).

Two things saved v0.5.22 in production:

1. The state mutation (``self._restored_value = None``) ran *before*
   the ``async_write_ha_state()`` call, so the next coordinator tick
   saw the cleared guard and pushed correctly.
2. The recorder's ``async_clear_statistics`` had already executed in
   the main service handler, so the recovery was driven by *that*
   path even when the handler crashed.

But the tracebacks are real: future-HA may upgrade the warning into a
hard crash that aborts before the assignment, or block the dispatcher
entirely. The fix (v0.5.23): mark every dispatcher handler ``@callback``
so HA invokes it on the event loop directly. ``async_write_ha_state``
is then safe.

## v0.6.0 update

The cost feature lost its entity layer in v0.6.0 (cost is now a pure
long-term statistic published from ``cost_publisher.py``), so
``CanalCumulativeCostSensor`` and its ``_on_clear_cost_stats`` /
``_on_meter_reset`` handlers are gone. The surviving dispatcher
handlers are the two ``_on_meter_reset`` methods on
``CanalCumulativeConsumptionSensor`` and ``CanalMeterReadingSensor``,
and the same ``@callback`` invariant applies to them.

## What this test pins

Pure source-introspection (AST), no Home Assistant runtime. The bug
class is "developer adds a new sync handler and forgets the
decorator", the defence is "a test that fails the moment a handler
ships without ``@callback``".

The assertions:

1. ``sensor.py`` imports ``callback`` from ``homeassistant.core``
   (you cannot decorate without the import).
2. Every method named ``_on_meter_reset`` (or any future ``_on_*``
   handler added to ``_HANDLER_METHOD_NAMES``) inside a class
   definition carries ``@callback`` as one of its decorators.

If a future contributor adds a new dispatcher handler following the
``_on_*`` naming convention, this test catches the missing decorator
*before* the user sees ERROR tracebacks in their log.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "custom_components" / "canal_isabel_ii"
_SENSOR = _PKG / "sensor.py"

# Names of methods that are wired to ``async_dispatcher_connect`` in
# ``async_added_to_hass``. Update this list when a new dispatcher
# handler is added; the test will then enforce ``@callback`` on it
# automatically.
#
# v0.6.0: ``_on_clear_cost_stats`` was removed alongside
# ``CanalCumulativeCostSensor`` — the cost stat is now published from
# ``cost_publisher.py``, no entity, no dispatcher signal.
_HANDLER_METHOD_NAMES = frozenset({"_on_meter_reset"})


def _module_imports(path: Path) -> set[str]:
    """Collect every imported identifier (handles ``from X import (A, B)``)."""
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            for alias in node.names:
                out.add(alias.asname or alias.name)
    return out


def _decorator_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return decorator identifiers for a function definition.

    Handles bare ``@callback`` and attribute-access decorators
    (``@some.decorator`` → ``"decorator"``); the canonical shape in
    HA is the bare name imported from ``homeassistant.core``.
    """
    names: list[str] = []
    for dec in func.decorator_list:
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.append(dec.attr)
        elif isinstance(dec, ast.Call):
            # ``@cached_property()`` style — pull the underlying name.
            inner = dec.func
            if isinstance(inner, ast.Name):
                names.append(inner.id)
            elif isinstance(inner, ast.Attribute):
                names.append(inner.attr)
    return names


def _collect_dispatcher_handlers() -> list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Walk ``sensor.py`` and return every (class, method, node).

    A method is a "dispatcher handler" if its name is in
    ``_HANDLER_METHOD_NAMES``. We surface (class_name, method_name,
    node) tuples so the failure message can point at the exact spot.
    """
    tree = ast.parse(_SENSOR.read_text())
    out: list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef):
            continue
        for body_item in class_node.body:
            if not isinstance(body_item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if body_item.name in _HANDLER_METHOD_NAMES:
                out.append((class_node.name, body_item.name, body_item))
    return out


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


def test_callback_imported_from_homeassistant_core():
    """Without the import, no ``@callback`` decorator can resolve.

    HA's ``callback`` is a no-op-at-runtime marker that the dispatcher
    inspects via ``hasattr(func, '_hass_callback')`` to decide whether
    to invoke a handler on the event loop or on the executor. Forgetting
    the import would either ``NameError`` at module load or silently
    fall back to executor scheduling (the bug v0.5.23 is fixing).
    """
    imports = _module_imports(_SENSOR)
    assert "callback" in imports, (
        "sensor.py must import ``callback`` from ``homeassistant.core``. "
        "Without it, the @callback decorator on dispatcher handlers "
        "raises NameError at module import. Add it to the existing "
        "``from homeassistant.core import HomeAssistant`` line."
    )


def test_every_dispatcher_handler_is_marked_callback():
    """Every ``_on_meter_reset`` needs ``@callback``.

    Without the decorator HA 2026.x raises ``RuntimeError`` from
    ``async_write_ha_state`` because the dispatcher schedules the
    handler on the executor thread pool. The state mutation
    (``_restored_value = None``) runs first so the recovery side
    effect is preserved, but the ERROR traceback fills the log on
    every dispatch and a future HA release may upgrade the warning
    into a hard abort that runs before the assignment.

    See ``CanalCumulativeConsumptionSensor._on_meter_reset``'s
    docstring for the full thread-safety rationale.
    """
    handlers = _collect_dispatcher_handlers()

    # Sanity: at least the two known handlers must be present, otherwise
    # the test is silently passing because nothing matched the filter.
    # v0.6.0 collapsed the cost entity, so the cost-side handler
    # expectations are gone — only the consumption-cumulative and
    # meter-reading sensors still need the dispatcher wiring.
    expected = {
        ("CanalCumulativeConsumptionSensor", "_on_meter_reset"),
        ("CanalMeterReadingSensor", "_on_meter_reset"),
    }
    found = {(cls, method) for cls, method, _ in handlers}
    missing = expected - found
    assert not missing, (
        f"Expected dispatcher handlers not found in sensor.py: {sorted(missing)}. "
        f"Did the class/method get renamed? Update _HANDLER_METHOD_NAMES "
        f"or the ``expected`` set in this test."
    )

    failures: list[str] = []
    for class_name, method_name, node in handlers:
        decs = _decorator_names(node)
        if "callback" not in decs:
            failures.append(
                f"  - {class_name}.{method_name} (line {node.lineno}): decorators = {decs or '∅'}"
            )

    assert not failures, (
        "The following dispatcher handlers are missing ``@callback``:\n"
        + "\n".join(failures)
        + "\n\nWithout @callback, HA's dispatcher schedules them on the "
        "executor thread pool and ``async_write_ha_state()`` raises "
        "RuntimeError on HA 2026.x. Add ``@callback`` from "
        "``homeassistant.core`` above each affected method."
    )
