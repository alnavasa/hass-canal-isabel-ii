"""Regression guard for the OptionsFlow menu introduced in v0.5.18.

Until v0.5.17 the OptionsFlow was a single-step form (``init`` rendered
the cost-params editor directly). v0.5.18 turns ``init`` into a
top-level menu with two branches:

* ``cost_params`` — the original cost-params form, renamed.
* ``rotate_token`` — new flow that regenerates the bookmarklet token
  and re-publishes the install notification.

This is the kind of change that's easy to break by accident:

* A refactor removes one of the two menu options → the user can edit
  cost params but loses the only UI path to rotate the token (or vice
  versa). HA shows no warning — the menu just renders with one
  option, which looks intentional.
* A refactor renames ``async_step_rotate_token`` but forgets to
  update the ``menu_options=[..., "rotate_token"]`` literal → HA's
  flow manager raises ``UnknownStep`` *only* when the user clicks
  that menu entry, never at startup. Easy to miss in dev.
* The translation files (``strings.json``, ``translations/{en,es}.json``)
  drift from the menu options → user sees the raw key
  ("cost_params") instead of a localised label.

This test is **AST + JSON**, no HA dependency. It walks
``config_flow.py`` to find the ``CanalOptionsFlow`` class, asserts:

1. ``async_step_init`` returns ``self.async_show_menu(step_id="init",
   menu_options=[...])`` with the expected literal list.
2. Each menu option has a matching ``async_step_<option>`` method.
3. Each menu option appears as a key under
   ``options.step.init.menu_options`` in ``strings.json`` and in
   each ``translations/*.json`` file.
4. Each menu option has its own ``options.step.<option>`` block in
   the same JSON files (so the form has a title and description
   when the user clicks through).

If any of those breaks, the test fails with a focused message
naming the offending file and key — turning an invisible-in-CI bug
into a one-read fix.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "custom_components" / "canal_isabel_ii"
_CONFIG_FLOW = _PKG / "config_flow.py"
_STRINGS = _PKG / "strings.json"
_TRANSLATIONS_DIR = _PKG / "translations"

# The expected menu, declared once here so the test fails loudly if
# someone adds/removes a branch without updating this guard.
EXPECTED_MENU_OPTIONS: tuple[str, ...] = ("cost_params", "rotate_token")


def _options_flow_class(tree: ast.AST) -> ast.ClassDef:
    """Return the ``CanalOptionsFlow`` class node, or fail the test."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CanalOptionsFlow":
            return node
    pytest.fail("CanalOptionsFlow class not found in config_flow.py")


def _step_methods(cls: ast.ClassDef) -> set[str]:
    """Names of ``async_step_*`` methods declared on the class."""
    return {
        node.name
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("async_step_")
    }


def _menu_options_from_init(cls: ast.ClassDef) -> list[str]:
    """Extract the literal ``menu_options=[...]`` from the init step.

    We look for ``return self.async_show_menu(...)`` inside
    ``async_step_init`` and read the ``menu_options=`` keyword as a
    list of string literals. Anything else (a variable, a function
    call, a non-string element) raises ``ValueError`` — the test then
    fails with that message, which is the right outcome: we want the
    menu options pinned to literals so the strings.json keys can be
    statically checked.
    """
    init = next(
        (
            node
            for node in cls.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_init"
        ),
        None,
    )
    if init is None:
        pytest.fail("async_step_init not found on CanalOptionsFlow")
    for node in ast.walk(init):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_show_menu"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            continue
        for kw in node.keywords:
            if kw.arg != "menu_options":
                continue
            if not isinstance(kw.value, ast.List):
                raise ValueError(
                    f"menu_options must be a list literal so we can statically "
                    f"verify it; got {ast.dump(kw.value)!r}"
                )
            out: list[str] = []
            for elt in kw.value.elts:
                if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                    raise ValueError(
                        f"menu_options entries must be string literals; got {ast.dump(elt)!r}"
                    )
                out.append(elt.value)
            return out
        raise ValueError("self.async_show_menu(...) call missing menu_options=")
    raise ValueError("async_step_init does not call self.async_show_menu(...)")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _translation_files() -> list[Path]:
    """All translation files we ship — one assertion per locale."""
    return sorted(_TRANSLATIONS_DIR.glob("*.json"))


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


def test_init_step_declares_expected_menu_options():
    """The init step's menu_options must match EXPECTED_MENU_OPTIONS exactly."""
    tree = ast.parse(_CONFIG_FLOW.read_text(encoding="utf-8"))
    cls = _options_flow_class(tree)
    declared = _menu_options_from_init(cls)
    assert declared == list(EXPECTED_MENU_OPTIONS), (
        f"OptionsFlow init menu_options drifted. Expected "
        f"{list(EXPECTED_MENU_OPTIONS)} but config_flow.py declares "
        f"{declared}. Update EXPECTED_MENU_OPTIONS in this test if the "
        f"change is intentional, then mirror the change in strings.json "
        f"and translations/*.json."
    )


def test_each_menu_option_has_a_step_method():
    """Every menu option must map to an ``async_step_<name>`` method."""
    tree = ast.parse(_CONFIG_FLOW.read_text(encoding="utf-8"))
    cls = _options_flow_class(tree)
    methods = _step_methods(cls)
    missing = [opt for opt in EXPECTED_MENU_OPTIONS if f"async_step_{opt}" not in methods]
    assert not missing, (
        f"Menu options {missing} have no matching async_step_<name> "
        f"method on CanalOptionsFlow. HA's flow manager will raise "
        f"UnknownStep when the user clicks them. Found methods: "
        f"{sorted(methods)}."
    )


@pytest.mark.parametrize("option", EXPECTED_MENU_OPTIONS, ids=lambda s: f"option:{s}")
def test_menu_option_listed_in_strings_json(option: str):
    """Each menu option must appear under options.step.init.menu_options in strings.json."""
    strings = _load_json(_STRINGS)
    menu = strings.get("options", {}).get("step", {}).get("init", {}).get("menu_options", {})
    assert option in menu, (
        f"strings.json options.step.init.menu_options is missing "
        f"key {option!r}. The user will see the raw key as the menu "
        f"label instead of a localised string."
    )


@pytest.mark.parametrize("option", EXPECTED_MENU_OPTIONS, ids=lambda s: f"option:{s}")
def test_menu_option_has_step_block_in_strings_json(option: str):
    """Each menu option must have its own options.step.<option> block."""
    strings = _load_json(_STRINGS)
    block = strings.get("options", {}).get("step", {}).get(option)
    assert isinstance(block, dict) and "title" in block, (
        f"strings.json is missing options.step.{option} (or it has no "
        f"title). The form will render with no title/description when "
        f"the user clicks the {option!r} menu entry."
    )


@pytest.mark.parametrize(
    ("locale_path", "option"),
    [(p, opt) for p in _translation_files() for opt in EXPECTED_MENU_OPTIONS],
    ids=lambda x: x.name if isinstance(x, Path) else f"option:{x}",
)
def test_menu_option_listed_in_translations(locale_path: Path, option: str):
    """Each translation file must localise both menu options."""
    data = _load_json(locale_path)
    menu = data.get("options", {}).get("step", {}).get("init", {}).get("menu_options", {})
    assert option in menu, (
        f"{locale_path.name} options.step.init.menu_options is missing "
        f"{option!r}. Users on this locale will see the raw key as the "
        f"menu label."
    )


@pytest.mark.parametrize(
    ("locale_path", "option"),
    [(p, opt) for p in _translation_files() for opt in EXPECTED_MENU_OPTIONS],
    ids=lambda x: x.name if isinstance(x, Path) else f"option:{x}",
)
def test_menu_option_has_step_block_in_translations(locale_path: Path, option: str):
    """Each translation file must define options.step.<option> with a title."""
    data = _load_json(locale_path)
    block = data.get("options", {}).get("step", {}).get(option)
    assert isinstance(block, dict) and "title" in block, (
        f"{locale_path.name} is missing options.step.{option} (or it "
        f"has no title). Users on this locale will see the form with "
        f"no title/description."
    )
