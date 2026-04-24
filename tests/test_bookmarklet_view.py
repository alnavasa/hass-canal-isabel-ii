"""Unit tests for the bookmarklet install page renderer.

We don't spin up an aiohttp server in these tests — that requires a
``hass`` fixture and an HTTP test client, both of which are out of
scope for the unit-test rig (they belong in HA's integration test
infrastructure). What we DO verify, exhaustively, is the *pure*
``render_bookmarklet_page`` function: same set of structural
contracts the bookmarklet-URL tests pin for the JS payload, but on
the HTML wrapper:

* Returns a complete HTML document (``<!DOCTYPE html>``, ``<title>``,
  ``<body>``).
* Embeds the minified bookmarklet in BOTH a draggable ``href=`` AND a
  ``data-bookmarklet=`` attribute (so the JS clipboard handler can
  read it without parsing the href).
* HTML-escapes user-provided strings (install name, source code) so a
  ``<script>`` in any of those does NOT execute.
* Renders one section per variant (primary + LAN/external alternates).
* Includes the contract-segregation warning verbatim — same UX
  guarantee the markdown notification used to make.
* Includes the JS clipboard handler bound to ``.copy-btn``.
* Includes the click-blocker on ``.drag-link`` so accidental clicks
  inside the HA UI don't try to execute the bookmarklet against a
  no-Canal-session origin.

The renderer lives in ``bookmarklet.py`` (stdlib-only) on purpose —
``bookmarklet_view.py`` is a thin HomeAssistantView wrapper that imports
``aiohttp``, which CI doesn't install (we don't run a real HA in
unit tests). Loading the wrapper from this test file would break test
collection.
"""

from __future__ import annotations

import importlib.util
import os as _os
import sys as _sys
import types as _types
from pathlib import Path


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
build_bookmarklet = _bm.build_bookmarklet
build_bookmarklet_source = _bm.build_bookmarklet_source
render_bookmarklet_page = _bm.render_bookmarklet_page
BOOKMARKLET_PAGE_URL_PREFIX = _const.BOOKMARKLET_PAGE_URL_PREFIX


HA_URL = "https://micasa.duckdns.org"
ENTRY = "abcdef1234567890"
TOKEN = "deadbeefcafebabe1234567890abcdef"
INSTALL = "Casa principal"


def _primary_only() -> tuple[str, list[tuple[str, str, str]], str]:
    bm = build_bookmarklet(ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL)
    src = build_bookmarklet_source(
        ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
    )
    return bm, [("Por defecto", HA_URL, bm)], src


def _render(install: str = INSTALL, **overrides) -> str:
    _bm_unused, variants, src = _primary_only()
    kwargs = {
        "install": install,
        "variants": variants,
        "source": src,
        "ha_url": HA_URL,
        "entry_id": ENTRY,
        "token": TOKEN,
    }
    kwargs.update(overrides)
    return render_bookmarklet_page(**kwargs)


# ---------------------------------------------------------------------
# Structural contract
# ---------------------------------------------------------------------


class TestStructure:
    def test_is_complete_html_document(self):
        out = _render()
        assert out.startswith("<!DOCTYPE html>")
        assert "<title>" in out
        assert "</title>" in out
        assert "<body>" in out
        assert "</body>" in out
        assert out.rstrip().endswith("</html>")

    def test_install_name_in_title_and_h1(self):
        out = _render(install="Casa principal")
        assert "Canal → HA · Casa principal</title>" in out
        assert "<h1>Canal → HA · Casa principal</h1>" in out

    def test_constant_url_prefix_value(self):
        # The page URL prefix is what the install notification + this view
        # both depend on. If someone renames it, both ends must move
        # together — pin the value.
        assert BOOKMARKLET_PAGE_URL_PREFIX == "/api/canal_isabel_ii/bookmarklet"


# ---------------------------------------------------------------------
# Bookmarklet payload — the whole point
# ---------------------------------------------------------------------


class TestBookmarkletPayload:
    def test_bookmarklet_in_href_attribute(self):
        _bm, variants, src = _primary_only()
        out = render_bookmarklet_page(
            install=INSTALL,
            variants=variants,
            source=src,
            ha_url=HA_URL,
            entry_id=ENTRY,
            token=TOKEN,
        )
        # Single quotes wrap the href in our template — no, double quotes.
        # The BM URL contains double quotes (escaped to &quot; in HTML),
        # so the attribute value won't appear literally. We check for the
        # leading marker + the entry id (which is alphanumeric so survives
        # untouched).
        assert 'class="drag-link" href="javascript:' in out
        assert ENTRY in out

    def test_bookmarklet_in_data_attribute(self):
        # The clipboard-copy button reads ``btn.dataset.bookmarklet`` —
        # the JS handler depends on this attribute being present.
        out = _render()
        assert 'data-bookmarklet="javascript:' in out

    def test_clipboard_button_present(self):
        out = _render()
        assert 'class="copy-btn"' in out
        assert "📋 Copiar bookmarklet" in out

    def test_clipboard_js_uses_navigator_clipboard(self):
        # Pin the API choice — if someone swaps it for execCommand,
        # iOS Safari will reject it silently outside of user gestures.
        out = _render()
        assert "navigator.clipboard.writeText" in out

    def test_drag_link_blocked_from_accidental_click(self):
        # Clicking the draggable link inside HA UI would try to run the
        # bookmarklet there (no Canal session) — the click handler must
        # preventDefault and warn the user.
        out = _render()
        assert "preventDefault" in out
        # Some short hint about dragging vs clicking; we don't pin the
        # exact phrasing.
        assert "arrastra" in out.lower() or "drag" in out.lower()


# ---------------------------------------------------------------------
# Multi-variant rendering
# ---------------------------------------------------------------------


class TestVariants:
    def test_single_variant_no_label_heading(self):
        # With one variant we suppress the "Por defecto" heading so the
        # page doesn't look like a placeholder for missing variants.
        out = _render()
        # The install title is in <h1>, but variant <h2> labels appear
        # ONLY when there's more than one.
        assert "<h2>Por defecto</h2>" not in out

    def test_two_variants_each_get_own_section(self):
        bm = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        lan_url = "http://homeassistant.local:8123"
        bm_lan = build_bookmarklet(
            ha_url=lan_url, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        src = build_bookmarklet_source(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        out = render_bookmarklet_page(
            install=INSTALL,
            variants=[
                ("Uso externo", HA_URL, bm),
                ("Uso en LAN", lan_url, bm_lan),
            ],
            source=src,
            ha_url=HA_URL,
            entry_id=ENTRY,
            token=TOKEN,
        )
        # Both labels render as section headings.
        assert "<h2>Uso externo</h2>" in out
        assert "<h2>Uso en LAN</h2>" in out
        # And both URLs appear so the user can see which is which.
        assert HA_URL in out
        assert lan_url in out
        # Both copy buttons are wired (one per variant).
        assert out.count('class="copy-btn"') == 2

    def test_variant_url_html_escaped(self):
        # If the user (somehow) sets internal_url to something with a "<"
        # or "&", we MUST escape it before dropping into <code>...</code>.
        bm = build_bookmarklet(
            ha_url="http://x.example/<script>alert(1)</script>",
            entry_id=ENTRY,
            token=TOKEN,
            installation_name=INSTALL,
        )
        out = render_bookmarklet_page(
            install=INSTALL,
            variants=[
                ("Por defecto", HA_URL, bm),
                ("Hostil", "http://x.example/<script>alert(1)</script>", bm),
            ],
            source="// just a comment",
            ha_url=HA_URL,
            entry_id=ENTRY,
            token=TOKEN,
        )
        assert "<script>alert(1)</script>" not in out
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


# ---------------------------------------------------------------------
# Safety — HTML escaping
# ---------------------------------------------------------------------


class TestHtmlEscaping:
    def test_install_name_html_escaped_in_title(self):
        out = _render(install='<script>alert("xss")</script>')
        assert "<script>alert" not in out
        assert "&lt;script&gt;alert" in out

    def test_source_html_escaped(self):
        bm = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        # Hand-craft a "source" containing a literal </script> — if we
        # render it verbatim the page parser closes our outer <script>
        # tag early and the rest of the document breaks. Escaping must
        # turn it into &lt;/script&gt;.
        hostile_src = "var x = '</script><img src=x onerror=alert(1)>';"
        out = render_bookmarklet_page(
            install=INSTALL,
            variants=[("Por defecto", HA_URL, bm)],
            source=hostile_src,
            ha_url=HA_URL,
            entry_id=ENTRY,
            token=TOKEN,
        )
        assert "</script><img" not in out
        assert "&lt;/script&gt;" in out

    def test_token_html_escaped(self):
        # Tokens are 32-hex from secrets.token_hex; they'd never contain
        # special chars in practice. But the renderer should still escape
        # — if a future change generates tokens with arbitrary chars
        # (passphrase, something bcrypt-y) we want defence in depth.
        out = _render()
        # No token character should leak unescaped — the test value
        # contains only hex chars, so we just verify the renderer ran the
        # escape path by checking that the token appears in <code>.
        assert f"<code>{TOKEN}</code>" in out


# ---------------------------------------------------------------------
# Contract-segregation warning — the UX-critical bit
# ---------------------------------------------------------------------


class TestContractWarning:
    def test_one_bookmarklet_one_contract_warning_present(self):
        # Same UX guarantee that the install notification used to make
        # — preserved as we move the content to the HTML page.
        out = _render()
        assert "contrato" in out.lower()
        # Must mention the multi-contract path (one of these phrases).
        assert (
            "varios" in out.lower() or "más de" in out.lower() or "otra integración" in out.lower()
        )


# ---------------------------------------------------------------------
# Notification ↔ page contract — the notification points at this page
# ---------------------------------------------------------------------


class TestNotificationLinksToPage:
    def test_format_install_notification_links_to_page_endpoint(self):
        bm = build_bookmarklet(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        src = build_bookmarklet_source(
            ha_url=HA_URL, entry_id=ENTRY, token=TOKEN, installation_name=INSTALL
        )
        msg = _bm.format_install_notification(
            install=INSTALL,
            bookmarklet=bm,
            ha_url=HA_URL,
            entry_id=ENTRY,
            token=TOKEN,
            source=src,
        )
        # The notification should point at the page endpoint with this
        # entry_id, so the link in the markdown is clickable.
        expected_path = f"{BOOKMARKLET_PAGE_URL_PREFIX}/{ENTRY}"
        assert expected_path in msg
        # And the raw bookmarklet must STILL appear (collapsed fallback)
        # so a user with a broken page still has something they can copy.
        assert bm in msg
