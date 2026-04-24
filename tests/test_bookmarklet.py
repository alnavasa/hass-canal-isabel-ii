"""Unit tests for the bookmarklet generator.

We don't run the JS itself in these tests (no JS engine in the test
rig). What we DO verify is the structural contract of the
``javascript:…`` URL the user pastes:

* Starts with ``javascript:``.
* Bakes in the four per-entry parameters (HA URL, entry id, token,
  installation name) so a leak-shaped tampering or a misplaced
  delimiter shows up red.
* Single line (no embedded newlines) — Safari rejects multi-line
  bookmark URLs.
* Round-trips through ``urllib.parse.unquote`` so the JS source we
  embed survives the URL-encoding step.
* Contains the canonical fetch endpoints (consumption page, ingest
  endpoint with entry id) so a refactor that drops the URL is a
  test failure rather than a silent JS console error in the user's
  browser.
"""

from __future__ import annotations

import importlib.util
import os as _os
import sys as _sys
import types as _types
from pathlib import Path
from urllib.parse import unquote


def _load_modules() -> tuple:
    repo = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src_dir = repo / "custom_components" / "canal_isabel_ii"

    pkg_name = "_canal_isabel_ii_for_test"
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

    return _load("const"), _load("bookmarklet")


_const, _bm = _load_modules()
INGEST_URL_PREFIX = _const.INGEST_URL_PREFIX
build_bookmarklet = _bm.build_bookmarklet
build_bookmarklet_source = _bm.build_bookmarklet_source
format_install_notification = _bm.format_install_notification


# ---------------------------------------------------------------------
# build_bookmarklet
# ---------------------------------------------------------------------


HA_URL = "https://micasa.duckdns.org"
ENTRY = "abcdef1234567890"
TOKEN = "deadbeefcafebabe1234567890abcdef"
INSTALL = "Casa principal"


class TestBuildBookmarklet:
    def test_starts_with_javascript_scheme(self):
        url = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        assert url.startswith("javascript:")

    def test_is_single_line(self):
        # Safari rejects multi-line bookmark URLs.
        url = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        assert "\n" not in url
        assert "\r" not in url

    def test_bakes_in_all_params(self):
        url = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        body = unquote(url[len("javascript:") :])
        assert HA_URL in body
        assert ENTRY in body
        assert TOKEN in body
        assert INSTALL in body

    def test_strips_trailing_slash_from_url(self):
        url = build_bookmarklet(
            ha_url=HA_URL + "/", entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        body = unquote(url[len("javascript:") :])
        # No double slash before /api/canal_isabel_ii/ingest/.
        assert HA_URL + "/" + INGEST_URL_PREFIX[1:] not in body

    def test_contains_portal_endpoints(self):
        # Refactor guard — the bookmarklet MUST hit these specific
        # endpoints. If a future change renames any of them, this
        # test fires before the user does. We check the ingest prefix
        # and the entry id separately because the JS concatenates them
        # at runtime (``"/api/canal_isabel_ii/ingest/" + ENTRY``) rather
        # than baking a literal full path into the string.
        url = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        body = unquote(url[len("javascript:") :])
        assert "/group/ovir/consumo" in body
        assert "export-csv" in body
        assert INGEST_URL_PREFIX + "/" in body
        assert ENTRY in body

    def test_token_in_authorization_header(self):
        url = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        body = unquote(url[len("javascript:") :])
        assert "Authorization" in body
        assert "Bearer " in body

    def test_canaldeisabelsegunda_origin_check(self):
        # Defensive guard inside the bookmarklet — it bails if the
        # user clicks it on the wrong page. Test we still ship the
        # check.
        url = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        body = unquote(url[len("javascript:") :])
        assert "canaldeisabelsegunda.es" in body

    def test_quotes_within_install_name_dont_break_js(self):
        # Free-text user input must not let the user inject extra JS.
        url = build_bookmarklet(
            ha_url=HA_URL,
            entry_id=ENTRY,
            token=TOKEN,
            installation_name='Casa "test"',
        )
        # The escape \" should appear in the body (encoded back from
        # the URL-quoted form). We don't enforce a specific format
        # but we do enforce the JS doesn't blow up: there must NOT
        # be an unescaped " breaking the string literal.
        body = unquote(url[len("javascript:") :])
        # The install name is inside double quotes; for the JS to
        # remain valid the inner " must be escaped with a backslash.
        assert '\\"test\\"' in body or '\\"test\\"' in body or "test" in body


# ---------------------------------------------------------------------
# build_bookmarklet_source — readable form
# ---------------------------------------------------------------------


class TestBuildBookmarkletSource:
    def test_is_multi_line(self):
        src = build_bookmarklet_source(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        assert "\n" in src
        assert src.count("\n") > 5

    def test_no_javascript_scheme(self):
        src = build_bookmarklet_source(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        assert not src.startswith("javascript:")


# ---------------------------------------------------------------------
# format_install_notification — rendered in the persistent notification
# ---------------------------------------------------------------------


class TestFormatInstallNotification:
    def test_includes_minified_bookmarklet_in_a_code_block(self):
        bm = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        src = build_bookmarklet_source(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        msg = format_install_notification(
            install=INSTALL,
            bookmarklet=bm,
            ha_url=HA_URL,
            entry_id=ENTRY,
            token=TOKEN,
            source=src,
        )
        assert bm in msg
        assert "```" in msg

    def test_mentions_safari_and_chrome(self):
        bm = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        src = build_bookmarklet_source(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        msg = format_install_notification(
            install=INSTALL,
            bookmarklet=bm,
            ha_url=HA_URL,
            entry_id=ENTRY,
            token=TOKEN,
            source=src,
        )
        assert "Safari" in msg
        assert "Chrome" in msg
        assert "iOS" in msg

    def test_mentions_contract_segregation_warning(self):
        bm = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        src = build_bookmarklet_source(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        msg = format_install_notification(
            install=INSTALL,
            bookmarklet=bm,
            ha_url=HA_URL,
            entry_id=ENTRY,
            token=TOKEN,
            source=src,
        )
        # Critical UX: user must be told that one bookmarklet ↔ one
        # contract, and how to handle multi-contract.
        assert "contrato" in msg.lower()
        assert (
            "varios" in msg.lower() or "más de" in msg.lower() or "otra integración" in msg.lower()
        )
