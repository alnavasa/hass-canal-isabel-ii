"""Pin: no cost entities live in ``sensor.py`` (v0.6.0+).

## Why this test exists

v0.6.0 deleted the cost entity layer. Cost is now a pure long-term
statistic published from ``cost_publisher.py``; the three sensors
(``CanalCumulativeCostSensor``, ``CanalCurrentPriceSensor``,
``CanalCurrentBlockSensor``) plus the ``_CostSensorMixin`` they shared
are gone. The redesign is the v0.6.0 fix for the recurring negative-bar
bug train (v0.5.20 → 21 → 22 → 23) — every patch in that train was a
symptom of the entity owning mutable state that drifted out of lockstep
with the recorder push. Cutting the entity removes the second writer,
which removes the divergence by construction.

This guard fires the moment any of those classes (or the mixin) is
re-introduced. Pure source-introspection (AST), no HA runtime — same
shape as ``test_dispatcher_handler_decorators.py`` and
``test_services_yaml.py``.

## Why a "negative" guard

Without this test, a future contributor could re-add a
``CanalCumulativeCostSensor`` to "expose cost as an entity for
templates" without realising the structural problem it brings back.
The new entity would either:

* Compete with ``cost_publisher.py`` as a second writer of the same
  statistic id (the old bug, restored).
* Compute its own state independently of the published statistic,
  giving template authors a value that disagrees with the Energy
  panel (a different but equally bad bug).

If a future need for a cost entity *does* arise, the right fix is to
read it from the published statistic (one writer, one reader) — and to
delete this test with a CHANGELOG note explaining the new design. A
silent re-introduction must fail loudly.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "custom_components" / "canal_isabel_ii"
_SENSOR = _PKG / "sensor.py"

# Names that must NOT appear as class definitions in sensor.py.
# Each one was a v0.5.x cost entity removed in v0.6.0.
_FORBIDDEN_CLASSES = frozenset(
    {
        "CanalCumulativeCostSensor",
        "CanalCurrentPriceSensor",
        "CanalCurrentBlockSensor",
        "_CostSensorMixin",
    }
)


def _class_names(path: Path) -> set[str]:
    """Return the set of class definitions at the module level."""
    tree = ast.parse(path.read_text())
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def test_no_cost_entity_classes_in_sensor_py():
    """No v0.5.x cost entity may be re-introduced in ``sensor.py``.

    See module docstring for the structural rationale. If you have a
    legitimate reason to add an entity that exposes cost data, route
    it through the published long-term statistic instead — and update
    this test only when the new entity's design is reviewed for the
    second-writer problem.
    """
    found = _class_names(_SENSOR)
    revived = found & _FORBIDDEN_CLASSES
    assert not revived, (
        f"v0.6.0 removed cost entities; one or more were re-added: {sorted(revived)}.\n"
        f"Cost is published as a long-term statistic from cost_publisher.py.\n"
        f"Re-adding an entity for cost makes the entity a second writer of the "
        f"same statistic id, which restores the v0.5.20 negative-bar bug "
        f"(entity state and recorder series drift out of sync). If you really "
        f"need a cost entity, derive it by reading the published statistic — "
        f"do NOT have it compute its own state."
    )
