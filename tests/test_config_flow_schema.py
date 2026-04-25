"""Regression guard for the config flow's NumberSelector schemas.

We don't pull Home Assistant into the test deps (would add ~50 MB and
~90 s to every CI run for a single test), so this guard is **AST-based**:
it walks ``config_flow.py``, finds every ``NumberSelectorConfig(...)``
call, and verifies the ``step=`` keyword satisfies HA core's hard
constraint:

    step is "any", or step is a number >= 1e-3

That's the validator in ``homeassistant.helpers.selector.NumberSelector``
(``CONFIG_SCHEMA``):

    vol.Optional("step", default=1): vol.Any(
        "any", vol.All(vol.Coerce(float), vol.Range(min=1e-3))
    )

This was the v0.5.1 → v0.5.2 production bug: ``step=0.0001`` for the
cuota suplementaria field made the entire config flow throw
``MultipleInvalid`` the moment the user ticked "Calcular precio (€)",
which HA surfaced as a generic *"Unknown error occurred"* on the
previous step. v0.5.3 fixes the value (``step="any"``) and this test
nails it down.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CONFIG_FLOW = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "canal_isabel_ii"
    / "config_flow.py"
)

HA_STEP_MIN = 1e-3  # mirrors HA core's NumberSelector CONFIG_SCHEMA


def _number_selector_calls(tree: ast.AST) -> list[ast.Call]:
    """All ``NumberSelectorConfig(...)`` call nodes in the AST."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "NumberSelectorConfig"
    ]


def _resolve_constants(tree: ast.AST) -> dict[str, float | str]:
    """Collect module-level ``NAME = literal`` so ``step=_FOO`` works.

    Only resolves direct numeric/string assignments — anything more
    complex (tuples, expressions) is skipped. Good enough for our
    config_flow's flat range constants.
    """
    out: dict[str, float | str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _step_value(call: ast.Call, constants: dict[str, float | str]) -> object:
    """Extract the literal/resolved value of the ``step=`` kwarg, or None."""
    for kw in call.keywords:
        if kw.arg != "step":
            continue
        if isinstance(kw.value, ast.Constant):
            return kw.value.value
        if isinstance(kw.value, ast.Name) and kw.value.id in constants:
            return constants[kw.value.id]
        # Anything we can't resolve statically (expression, attribute, …)
        # — return a sentinel so the test fails loudly rather than silently
        # passing a value we never inspected.
        return ast.dump(kw.value)
    return None  # No step= kwarg → HA defaults to step=1, which is fine.


def test_number_selector_step_within_ha_bounds() -> None:
    """Every NumberSelectorConfig(step=…) must be 'any' or ≥ 1e-3.

    Regression test for the v0.5.1 cost-form crash: setting
    ``step=0.0001`` on the cuota field made HA's selector schema reject
    the form, which surfaced as *"Unknown error occurred"* and blocked
    every new install that opted into cost tracking.
    """
    src = CONFIG_FLOW.read_text(encoding="utf-8")
    tree = ast.parse(src)
    constants = _resolve_constants(tree)
    calls = _number_selector_calls(tree)
    assert calls, "no NumberSelectorConfig(...) calls found — did the file move?"

    bad: list[tuple[int, object]] = []
    for call in calls:
        step = _step_value(call, constants)
        if step is None:
            continue  # HA default (1) is always valid
        if step == "any":
            continue
        if isinstance(step, (int, float)) and float(step) >= HA_STEP_MIN:
            continue
        bad.append((call.lineno, step))

    if bad:
        pytest.fail(
            "NumberSelectorConfig(step=…) values must be 'any' or ≥ 1e-3 "
            "(HA core constraint). Offenders:\n"
            + "\n".join(f"  line {ln}: step={val!r}" for ln, val in bad)
        )
