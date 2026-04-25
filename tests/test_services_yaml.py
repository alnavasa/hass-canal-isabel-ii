"""Regression guard for ``services.yaml`` ↔ ``__init__.py`` coherence.

When you register a service in ``async_setup`` but forget to declare
it in ``services.yaml`` (or vice-versa) HA still works — but the user
is punished:

* Service in code, missing from YAML → no entry in
  *Herramientas para desarrolladores → Acciones*; the user can't
  invoke it from the UI, only from a script with the literal
  ``service: canal_isabel_ii.<name>``. They have no way to discover
  it exists.
* Service in YAML, missing from code → HA logs
  ``"service <name> for domain canal_isabel_ii not found"`` every
  startup. The UI dropdown *does* show it, so the user clicks it
  and gets ``Failed to call service``.
* Field in YAML's ``fields:`` not in the voluptuous schema → user
  sees a textbox for a parameter that the handler will silently
  ignore. Looks like a bug.
* Field in voluptuous schema not in YAML's ``fields:`` → handler
  reads it but the UI never offers it. Same shape: invisible
  parameter.

This bug class is invisible to runtime tests (HA tolerates the
mismatch), invisible to ``ruff`` (each file is internally valid),
and slips through code review whenever the diff touches only one
of the two files.

The test below is **AST + YAML**, no Home Assistant dependency:

1. Parse ``__init__.py``, collect every ``SERVICE_*`` constant and
   every ``*_SCHEMA = vol.Schema({...})`` block, and every
   ``hass.services.async_register(DOMAIN, SERVICE_*, …, schema=…)``
   call to pair them.
2. Parse ``services.yaml`` with ``yaml.safe_load``.
3. Assert:
   - The set of service names from code == the set of top-level
     keys in the YAML.
   - For every matched service, the set of voluptuous keys ==
     the set of YAML field names.

Each assertion gets a focused failure message naming the offending
file and identifier so a future contributor sees the fix in one read.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_INIT = _REPO / "custom_components" / "canal_isabel_ii" / "__init__.py"
_SERVICES_YAML = _REPO / "custom_components" / "canal_isabel_ii" / "services.yaml"


# ---------------------------------------------------------------------
# Module-level constant resolver — handles ``SERVICE_X = "x"`` and
# ``ATTR_INSTANCE = "instance"`` lookups so the AST inspection below
# can dereference identifiers used in service registrations.
# ---------------------------------------------------------------------


def _resolve_string_constants(tree: ast.AST) -> dict[str, str]:
    """Collect module-level ``NAME = "literal"`` mappings.

    Skips anything that isn't a plain string literal so we don't
    accidentally pretend a complex expression resolves to something.
    """
    out: dict[str, str] = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out[node.targets[0].id] = node.value.value
    return out


# ---------------------------------------------------------------------
# Schema → field-name extractor
#
# We parse expressions of the form
#   ``vol.Schema({vol.Optional(ATTR_INSTANCE): cv.string})``
# and extract the dict's keys (resolving ``vol.Optional(NAME)`` and
# ``vol.Required(NAME)`` to ``NAME``, and ``NAME`` to its string value
# via ``constants``). Anything we can't resolve is reported up so the
# test fails loud rather than silently passing on an unrecognised
# pattern (which would let a future schema rewrite slip through).
# ---------------------------------------------------------------------


def _schema_keys(call: ast.Call, constants: dict[str, str]) -> set[str]:
    """Extract the set of voluptuous keys declared by ``vol.Schema({...})``.

    Handles the patterns we actually use:

    * ``vol.Optional("literal")``
    * ``vol.Required("literal")``
    * ``vol.Optional(NAME)`` / ``vol.Required(NAME)`` where ``NAME``
      is a module-level string constant.

    Bare-string keys (``{"x": cv.string}``) are also accepted.

    Raises ``ValueError`` if a key uses a pattern we don't recognise —
    we want the test to fail loudly rather than silently miss a key.
    """
    if not call.args or not isinstance(call.args[0], ast.Dict):
        return set()
    out: set[str] = set()
    for key in call.args[0].keys:
        if key is None:
            # ``**spread`` in the dict literal — not used in our code.
            raise ValueError("Unsupported schema key: dict spread (**)")
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out.add(key.value)
            continue
        if isinstance(key, ast.Call) and isinstance(key.func, ast.Attribute):
            attr = key.func.attr
            if attr not in {"Optional", "Required", "Exclusive", "Inclusive"}:
                raise ValueError(f"Unsupported voluptuous wrapper: {attr}")
            if not key.args:
                raise ValueError(f"vol.{attr}() with no args")
            inner = key.args[0]
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                out.add(inner.value)
            elif isinstance(inner, ast.Name) and inner.id in constants:
                out.add(constants[inner.id])
            else:
                raise ValueError(
                    f"Cannot resolve schema key inside vol.{attr}(): {ast.unparse(inner)}"
                )
            continue
        raise ValueError(f"Unsupported schema key node: {ast.unparse(key)}")
    return out


def _is_vol_schema_call(node: ast.AST) -> bool:
    """True for ``vol.Schema(...)`` calls."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Schema"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "vol"
    )


# ---------------------------------------------------------------------
# Inspect __init__.py: which services exist + which schema each uses?
# ---------------------------------------------------------------------


def _collect_code_services() -> dict[str, set[str]]:
    """Map service-name → set of declared field names, parsed from code.

    Walks ``__init__.py`` and looks for the pattern:

        hass.services.async_register(
            DOMAIN, SERVICE_X, handler, schema=X_SCHEMA
        )

    pairing the ``SERVICE_X`` literal with the ``X_SCHEMA`` literal
    (both module-level constants). Then resolves ``X_SCHEMA`` back to
    the dict it was assigned and extracts the voluptuous keys.

    Failure modes that intentionally raise ``ValueError`` here so the
    test surfaces them loudly:

    * A registration that omits the keyword ``schema=`` — pre-HA-2024.4
      shape, never used in this codebase but worth catching.
    * A schema referenced by an ``async_register`` call that we can't
      find as a module-level assignment.
    * A schema using a voluptuous wrapper we don't recognise (see
      ``_schema_keys``).
    """
    src = _INIT.read_text()
    tree = ast.parse(src)
    constants = _resolve_string_constants(tree)

    # Map of NAME → ast.Call for every ``NAME = vol.Schema({...})``
    # at module level.
    schemas: dict[str, ast.Call] = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if _is_vol_schema_call(node.value):
            schemas[node.targets[0].id] = node.value  # type: ignore[assignment]

    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match ``hass.services.async_register(...)``.
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_register"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "services"
        ):
            continue
        # First positional is DOMAIN, second is the SERVICE_* constant.
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Name):
            continue
        service_const = node.args[1].id
        service_name = constants.get(service_const)
        if service_name is None:
            raise ValueError(
                f"async_register references unknown service constant {service_const!r}"
            )
        # ``schema=`` keyword.
        schema_name: str | None = None
        for kw in node.keywords:
            if kw.arg == "schema" and isinstance(kw.value, ast.Name):
                schema_name = kw.value.id
                break
        if schema_name is None:
            raise ValueError(f"async_register for {service_name!r} omits schema= keyword")
        if schema_name not in schemas:
            raise ValueError(
                f"async_register for {service_name!r} uses unknown schema constant {schema_name!r}"
            )
        out[service_name] = _schema_keys(schemas[schema_name], constants)
    return out


# ---------------------------------------------------------------------
# Inspect services.yaml: which services + fields are declared?
# ---------------------------------------------------------------------


def _collect_yaml_services() -> dict[str, set[str]]:
    """Map service-name → set of declared field names, parsed from YAML.

    Empty ``fields:`` (or absent) is treated as an empty set, matching
    HA's behaviour where the field list is purely descriptive.
    """
    raw: Any = yaml.safe_load(_SERVICES_YAML.read_text())
    if not isinstance(raw, dict):
        return {}
    out: dict[str, set[str]] = {}
    for name, body in raw.items():
        fields = (body or {}).get("fields") if isinstance(body, dict) else None
        if not isinstance(fields, dict):
            out[str(name)] = set()
            continue
        out[str(name)] = {str(k) for k in fields}
    return out


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


def test_yaml_and_code_declare_the_same_services():
    code = _collect_code_services()
    yml = _collect_yaml_services()
    in_code_not_yaml = sorted(set(code) - set(yml))
    in_yaml_not_code = sorted(set(yml) - set(code))
    assert not in_code_not_yaml, (
        f"Services registered in __init__.py but missing from "
        f"services.yaml: {in_code_not_yaml}. Add them to services.yaml "
        f"(see existing entries for shape) or the user won't see them "
        f"in Developer Tools → Actions."
    )
    assert not in_yaml_not_code, (
        f"Services declared in services.yaml but not registered in "
        f"__init__.py: {in_yaml_not_code}. Either implement them or "
        f"remove the YAML entry — otherwise HA logs a 'service not "
        f"found' warning every startup."
    )


@pytest.mark.parametrize(
    "service",
    sorted(_collect_yaml_services().keys()),
    ids=lambda s: f"service:{s}",
)
def test_each_service_yaml_fields_match_voluptuous_schema(service: str):
    """Per-service check so a failure points at the offending name."""
    code = _collect_code_services()
    yml = _collect_yaml_services()
    code_fields = code.get(service, set())
    yaml_fields = yml.get(service, set())
    in_code_not_yaml = sorted(code_fields - yaml_fields)
    in_yaml_not_code = sorted(yaml_fields - code_fields)
    assert not in_code_not_yaml, (
        f"Service {service!r}: schema declares fields {in_code_not_yaml} "
        f"that are missing from services.yaml. The user won't see a UI "
        f"input for them."
    )
    assert not in_yaml_not_code, (
        f"Service {service!r}: services.yaml declares fields "
        f"{in_yaml_not_code} that aren't in the voluptuous schema. The "
        f"handler will silently ignore whatever the user types."
    )


def test_no_unparseable_schema_pattern():
    """The collector raises ValueError if it sees a schema pattern it
    can't decode. We want THAT to be the test failure (loud, focused),
    not a silent miss that lets a real mismatch through. So we just
    invoke the collector and let any raise propagate as a test failure
    with the original message."""
    _collect_code_services()
    _collect_yaml_services()
