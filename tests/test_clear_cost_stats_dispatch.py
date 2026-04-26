"""Regression guard for the ``clear_cost_stats`` recovery dispatcher (v0.5.22+).

## Why this test exists

v0.5.21 added a symmetric monotonic guard to ``CanalCumulativeCostSensor``:
both ``native_value`` and ``_push_cost_statistics_locked`` skip when
``latest cumulative_eur < _restored_value - 0.01``. This correctly
prevents new negative bars in the Energy panel — the recorder never
sees a regressed sum behind the entity state's back.

But it also created a new failure mode for the user-facing recovery
service ``canal_isabel_ii.clear_cost_stats``:

1. User has accumulated a large ``_restored_value`` (e.g. 150 €) from
   a previous session.
2. Cache loses old bimonths (rolling window trim, manual delete, etc.)
   and the recomputed stream now tops out at 110 €.
3. User runs ``clear_cost_stats``: recorder is wiped clean.
4. Next coordinator tick: ``native_value`` checks the guard — 110 <
   150-0.01 → **regression detected** → returns the stale 150 from
   memory. ``_push_cost_statistics_locked`` does the same — push
   skipped.
5. Result: recorder stays empty, entity stays frozen at 150, every
   subsequent push is blocked. The Energy panel shows **0 € forever**
   for the cost column. The "recovery" service made things worse.

The fix (v0.5.22): the service must, after clearing the recorder,
ALSO drop the in-memory ``_restored_value`` on the live cost sensor
so the next tick recomputes against ``None`` (no guard applies) and
pushes from cold-start.

The mechanism is a dispatcher signal:

* ``const.py`` declares ``SIGNAL_CLEAR_COST_STATS`` with a
  ``{entry_id}_{contract_id}`` format string.
* ``__init__.py``'s ``_clear_cost_stats_for_entry`` calls
  ``async_dispatcher_send`` for every contract after the recorder
  clear.
* ``sensor.py``'s ``CanalCumulativeCostSensor.async_added_to_hass``
  wires ``async_dispatcher_connect`` to ``_on_clear_cost_stats``,
  which sets ``self._restored_value = None`` and calls
  ``async_write_ha_state``.

## What this test pins

Pure source-introspection (AST + grep), no Home Assistant runtime.
Mirrors ``test_services_yaml.py``'s shape — the bug class is "two
files drift apart and silently break the user-visible flow", the
defence is "a test that fails loud the moment they drift".

The four assertions:

1. The signal constant exists in ``const.py`` and uses the
   ``{entry_id}_{contract_id}`` format placeholders (sender and
   receiver must agree on what to substitute).
2. ``__init__.py`` imports the signal and calls
   ``async_dispatcher_send`` with it inside
   ``_clear_cost_stats_for_entry``.
3. ``sensor.py`` imports the signal and calls
   ``async_dispatcher_connect`` with it inside the cost sensor's
   ``async_added_to_hass``.
4. Both callsites use the same identifier (``SIGNAL_CLEAR_COST_STATS``)
   — a typo'd send/connect pair would silently fail in production but
   pass any unit test that only checks one side.

If any of these break, the recovery procedure documented in
``docs/USE.md`` (FAQ → "El panel Energía → Agua muestra una barra
negativa") stops working and the user is back to the v0.5.21 freeze.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "custom_components" / "canal_isabel_ii"
_CONST = _PKG / "const.py"
_INIT = _PKG / "__init__.py"
_SENSOR = _PKG / "sensor.py"

_SIGNAL_NAME = "SIGNAL_CLEAR_COST_STATS"


def _module_constants(path: Path) -> dict[str, str]:
    """Collect ``NAME = "literal"`` mappings at module level."""
    tree = ast.parse(path.read_text())
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out[node.targets[0].id] = node.value.value
    return out


def _module_imports(path: Path) -> set[str]:
    """Collect every imported identifier (handles ``from X import (A, B)``)."""
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            for alias in node.names:
                out.add(alias.asname or alias.name)
    return out


def _calls_with_first_arg(tree: ast.AST, func_attr: str, name_id: str) -> list[ast.Call]:
    """Return every ``X.<func_attr>(<name_id>, …)`` call in ``tree``.

    Matches both ``async_dispatcher_send(hass, SIGNAL_X.format(...))``
    and the ``connect`` variant. We look at the first argument that
    is a ``.format`` call on the named identifier — that's the canonical
    shape the codebase uses for per-(entry, contract) signals.
    """
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Function name match — bare or attribute access.
        fname: str | None = None
        if isinstance(node.func, ast.Name):
            fname = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        if fname != func_attr:
            continue
        # Look for a SIGNAL_X.format(...) inside the argument list.
        for arg in node.args:
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "format"
                and isinstance(arg.func.value, ast.Name)
                and arg.func.value.id == name_id
            ):
                out.append(node)
                break
    return out


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


def test_signal_constant_exists_in_const_py():
    """The dispatcher signal must be declared as a string constant.

    Without this, neither sender nor receiver can import it and the
    per-(entry, contract) routing falls apart.
    """
    constants = _module_constants(_CONST)
    assert _SIGNAL_NAME in constants, (
        f"{_SIGNAL_NAME} missing from const.py — add it next to "
        f"SIGNAL_METER_RESET (same shape, same docstring style)."
    )
    value = constants[_SIGNAL_NAME]
    # Both placeholders required: a per-entry-only signal would fan out
    # across contracts inside the same entry, and a per-contract-only
    # signal would collide across entries that happen to manage the
    # same contract id (rare but possible during config-flow rework).
    assert "{entry_id}" in value, (
        f"{_SIGNAL_NAME} format string must include {{entry_id}}: got {value!r}"
    )
    assert "{contract_id}" in value, (
        f"{_SIGNAL_NAME} format string must include {{contract_id}}: got {value!r}"
    )


def test_init_py_imports_and_dispatches_signal():
    """``_clear_cost_stats_for_entry`` must send the signal."""
    assert _SIGNAL_NAME in _module_imports(_INIT), (
        f"__init__.py does not import {_SIGNAL_NAME} — add it to the "
        f"existing ``from .const import (...)`` block alongside "
        f"SIGNAL_METER_RESET."
    )
    tree = ast.parse(_INIT.read_text())
    sends = _calls_with_first_arg(tree, "async_dispatcher_send", _SIGNAL_NAME)
    assert sends, (
        f"__init__.py never calls async_dispatcher_send with "
        f"{_SIGNAL_NAME}.format(...). Without this, the live cost "
        f"sensor's _restored_value is never reset and the v0.5.21 "
        f"regression guard freezes the entity at the pre-clear value "
        f"forever (Energy panel reads 0 € indefinitely)."
    )


def test_sensor_py_imports_and_subscribes_to_signal():
    """``CanalCumulativeCostSensor`` must connect to the signal.

    Otherwise the dispatcher_send fires into a void and nothing
    resets ``_restored_value`` on the live entity.
    """
    assert _SIGNAL_NAME in _module_imports(_SENSOR), (
        f"sensor.py does not import {_SIGNAL_NAME} — add it to the "
        f"existing ``from .const import (...)`` block."
    )
    tree = ast.parse(_SENSOR.read_text())
    connects = _calls_with_first_arg(tree, "async_dispatcher_connect", _SIGNAL_NAME)
    assert connects, (
        f"sensor.py never calls async_dispatcher_connect with "
        f"{_SIGNAL_NAME}.format(...). Without this, _clear_cost_stats_for_entry's "
        f"dispatch is heard by nobody and the entity stays frozen."
    )


def test_signal_send_and_connect_share_identifier():
    """Sender and receiver must reference the same constant.

    A typo in either callsite would compile fine and pass any test
    that only inspects one side. This pins both sides to the same
    identifier (``SIGNAL_CLEAR_COST_STATS``) so a rename catches
    here, not in production.
    """
    init_tree = ast.parse(_INIT.read_text())
    sensor_tree = ast.parse(_SENSOR.read_text())
    # Both callsites must reference the identifier as a Name node (not
    # just import it — Python's AST records ``from X import Y`` as
    # ``alias`` nodes, NOT ``Name`` nodes, so a file that imports the
    # signal but never uses it would pass test_init_py_imports_and… on
    # the import side and still ship dead code. Counting Name nodes
    # specifically requires actual *use* of the identifier.
    init_uses = sum(
        1
        for n in ast.walk(init_tree)
        if isinstance(n, ast.Name) and n.id == _SIGNAL_NAME
    )
    sensor_uses = sum(
        1
        for n in ast.walk(sensor_tree)
        if isinstance(n, ast.Name) and n.id == _SIGNAL_NAME
    )
    assert init_uses >= 1, (
        f"__init__.py imports {_SIGNAL_NAME} but never uses it as a "
        f"value. The dispatcher send is missing or the import is "
        f"dead — either way the recovery flow is broken."
    )
    assert sensor_uses >= 1, (
        f"sensor.py imports {_SIGNAL_NAME} but never uses it as a "
        f"value. The dispatcher connect is missing or the import is "
        f"dead — either way the cost sensor will never reset its "
        f"_restored_value after clear_cost_stats."
    )


def test_dispatch_happens_inside_clear_cost_stats_for_entry():
    """The send must live inside the recovery handler, not elsewhere.

    A send placed in, say, ``async_setup_entry`` would fire on every
    boot and reset the guard inappropriately, breaking the
    monotonicity protection that v0.5.21 added.
    """
    tree = ast.parse(_INIT.read_text())
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "_clear_cost_stats_for_entry":
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fname = (
                sub.func.attr
                if isinstance(sub.func, ast.Attribute)
                else sub.func.id
                if isinstance(sub.func, ast.Name)
                else None
            )
            if fname != "async_dispatcher_send":
                continue
            for arg in sub.args:
                if (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == "format"
                    and isinstance(arg.func.value, ast.Name)
                    and arg.func.value.id == _SIGNAL_NAME
                ):
                    found = True
                    break
            if found:
                break
        if found:
            break
    assert found, (
        f"async_dispatcher_send({_SIGNAL_NAME}.format(...)) does not "
        f"appear inside _clear_cost_stats_for_entry. The signal must "
        f"fire from the recovery path, not from setup or another "
        f"unrelated handler."
    )


def test_subscribe_happens_inside_cost_sensor_added_to_hass():
    """The connect must live in the cost sensor's lifecycle hook.

    A connect outside ``async_added_to_hass`` would either fire too
    early (no entity to update) or never (no entity_id resolvable).
    Pin it to the right method.
    """
    tree = ast.parse(_SENSOR.read_text())
    # Find the CanalCumulativeCostSensor class body and its
    # async_added_to_hass.
    found_class = False
    found_connect_in_method = False
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        if cls.name != "CanalCumulativeCostSensor":
            continue
        found_class = True
        for method in cls.body:
            if not isinstance(method, ast.AsyncFunctionDef):
                continue
            if method.name != "async_added_to_hass":
                continue
            for sub in ast.walk(method):
                if not isinstance(sub, ast.Call):
                    continue
                fname = (
                    sub.func.attr
                    if isinstance(sub.func, ast.Attribute)
                    else sub.func.id
                    if isinstance(sub.func, ast.Name)
                    else None
                )
                if fname != "async_dispatcher_connect":
                    continue
                for arg in sub.args:
                    if (
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Attribute)
                        and arg.func.attr == "format"
                        and isinstance(arg.func.value, ast.Name)
                        and arg.func.value.id == _SIGNAL_NAME
                    ):
                        found_connect_in_method = True
                        break
                if found_connect_in_method:
                    break
            break
        break
    assert found_class, "CanalCumulativeCostSensor class not found in sensor.py"
    assert found_connect_in_method, (
        f"async_dispatcher_connect({_SIGNAL_NAME}.format(...)) does not "
        f"appear inside CanalCumulativeCostSensor.async_added_to_hass. "
        f"Wire it next to the existing SIGNAL_METER_RESET subscription."
    )


def test_handler_method_resets_restored_value():
    """``_on_clear_cost_stats`` must drop ``self._restored_value`` to None.

    A handler that logs but doesn't touch ``_restored_value`` would
    look right in code review but be a no-op for the actual recovery
    flow — the regression guard would still fire on the next tick.
    """
    tree = ast.parse(_SENSOR.read_text())
    handler_body_text: str | None = None
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        if cls.name != "CanalCumulativeCostSensor":
            continue
        for method in cls.body:
            if isinstance(method, ast.FunctionDef) and method.name == "_on_clear_cost_stats":
                handler_body_text = ast.unparse(method)
                break
        break
    assert handler_body_text is not None, (
        "CanalCumulativeCostSensor._on_clear_cost_stats handler is "
        "missing. Add it next to _on_meter_reset (same body shape)."
    )
    # Look for the actual assignment that closes the loop.
    assert "self._restored_value = None" in handler_body_text, (
        "_on_clear_cost_stats does not set self._restored_value = None. "
        "Without this assignment the handler is a no-op and the v0.5.21 "
        "regression guard keeps blocking pushes after clear_cost_stats."
    )
