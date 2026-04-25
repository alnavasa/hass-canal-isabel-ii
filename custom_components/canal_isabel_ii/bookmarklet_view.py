"""Human-facing HTML page that exposes this entry's bookmarklet with a
drag-to-bookmarks-bar link AND a one-tap copy-to-clipboard button.

Why this exists
---------------

The persistent notification ships the bookmarklet inside a Markdown code
block. On iOS Safari, copying ~1.5 KB of URL-encoded JavaScript out of a
code block is genuinely painful: long-press, drag the selection markers
across hundreds of escaped characters without missing a single byte, paste
into the bookmark URL field. People give up halfway and the integration
sits there waiting for a click that never comes.

This view solves it with two affordances:

* A draggable ``<a href="javascript:…">★ Canal → HA</a>`` button — on
  desktop the user drags it straight onto the bookmarks bar, no copying
  involved.
* A ``<button>📋 Copiar bookmarklet</button>`` that calls
  ``navigator.clipboard.writeText()`` — one tap on iOS, one click on
  desktop, and the URL is on the clipboard ready to paste into a
  manually-created bookmark.

Both affordances are rendered for the primary HA URL AND for every
LAN/external alternate the integration has (so a household that uses
``internal_url`` from inside the LAN and ``external_url`` from outside
gets both, properly labelled, on the same page).

URL
---

::

    GET /api/canal_isabel_ii/bookmarklet/{entry_id}?t=<token>

Auth
----

``requires_auth = False`` + per-entry token validated in the handler with
``secrets.compare_digest``. We can't use HA's normal ``requires_auth=True``
because the user reaches this page by **clicking a markdown link in a
persistent notification**, which causes a plain browser navigation. That
navigation does NOT carry the ``Authorization: Bearer`` header HA's
session-cookie auth machinery expects — HA's access token lives in the
frontend's localStorage and only travels on requests issued by frontend
JS, not on user-driven URL navigations. With ``requires_auth=True`` the
view returns 401 Unauthorized to every user who clicks the link.

The ``?t=<token>`` pattern fixes this. The same per-entry token that
authenticates the ingest endpoint is required as a query param to view
the install page. Putting it in the URL doesn't widen the attack
surface: the bookmarklet HTML this page exposes embeds the same token
verbatim (it's how the bookmarklet authenticates against the ingest
endpoint when the user clicks it). Anyone who has the page URL also
has the bookmarklet URL with the token inside — symmetric exposure.

Why this isn't a Lovelace card
------------------------------

The notification fires on first config + can be re-fired by the
``show_bookmarklet`` service. It links to ``/api/canal_isabel_ii/bookmarklet/<entry_id>?t=<token>`` directly — no Lovelace card to install,
no add-on, no third-party panel. Just a single HTML page rendered by
the integration on demand.
"""

from __future__ import annotations

import html
import logging
import secrets

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .bookmarklet import (
    build_bookmarklet,
    build_bookmarklet_source,
    collect_alternate_urls,
    render_bookmarklet_page,
)
from .const import BOOKMARKLET_PAGE_URL_PREFIX, CONF_HA_URL, CONF_NAME, CONF_TOKEN

_LOGGER = logging.getLogger(__name__)


class CanalBookmarkletPageView(HomeAssistantView):
    """HTML page exposing this entry's bookmarklet(s) with copy + drag UI.

    Multi-tenant via ``{entry_id}`` in the URL. Registered once per HA
    boot in ``async_setup``.
    """

    url = f"{BOOKMARKLET_PAGE_URL_PREFIX}/{{entry_id}}"
    name = "api:canal_isabel_ii:bookmarklet_page"
    # See module docstring "Auth" section: HA's normal requires_auth=True
    # 401s on plain browser navigations from notification links, so we
    # validate the per-entry token in the ?t= query param ourselves.
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        config_entry = self.hass.config_entries.async_get_entry(entry_id)
        if config_entry is None:
            return web.Response(
                status=404,
                text=(
                    "<!DOCTYPE html><meta charset=utf-8><title>404 — Canal de Isabel II</title>"
                    '<body style="font-family:-apple-system,sans-serif;max-width:40rem;'
                    'margin:3rem auto;padding:0 1rem">'
                    "<h1>404 · Entry no encontrada</h1>"
                    f"<p>No hay ninguna integración Canal de Isabel II con el id "
                    f"<code>{html.escape(entry_id)}</code>. Quizá la borraste o "
                    f"el enlace está obsoleto.</p>"
                    "</body>"
                ),
                content_type="text/html",
                charset="utf-8",
            )

        ha_url = config_entry.data.get(CONF_HA_URL) or ""
        token = config_entry.data.get(CONF_TOKEN) or ""
        install = config_entry.data.get(CONF_NAME) or "Canal de Isabel II"

        # Validate the ?t=<token> query param against the entry's stored
        # Bearer token. We compare in constant time and refuse the
        # request without leaking which side mismatched.
        provided_token = request.query.get("t", "")
        if not token or not provided_token or not secrets.compare_digest(provided_token, token):
            return web.Response(
                status=401,
                text=(
                    "<!DOCTYPE html><meta charset=utf-8>"
                    "<title>401 — Canal de Isabel II</title>"
                    '<body style="font-family:-apple-system,sans-serif;max-width:40rem;'
                    'margin:3rem auto;padding:0 1rem">'
                    "<h1>401 · No autorizado</h1>"
                    "<p>Esta página requiere el token de la integración en el query "
                    "string (<code>?t=…</code>). Vuelve a la notificación "
                    '<strong>"Bookmarklet listo"</strong> y pulsa el enlace de '
                    "instalación desde ahí — el enlace ya incluye el token. Si la "
                    "perdiste, regenérala desde "
                    "<em>Herramientas para desarrolladores → Acciones → "
                    "<code>canal_isabel_ii.show_bookmarklet</code></em>.</p>"
                    "</body>"
                ),
                content_type="text/html",
                charset="utf-8",
            )

        primary_bm = build_bookmarklet(
            ha_url=ha_url,
            entry_id=entry_id,
            token=token,
            installation_name=install,
        )
        primary_src = build_bookmarklet_source(
            ha_url=ha_url,
            entry_id=entry_id,
            token=token,
            installation_name=install,
        )

        # Default + alternates, same logic the install notification used to
        # apply inline. The page is the new home for this enumeration.
        variants: list[tuple[str, str, str]] = [
            ("Por defecto", ha_url, primary_bm),
        ]
        for label, alt_url in collect_alternate_urls(self.hass, ha_url):
            alt_bm = build_bookmarklet(
                ha_url=alt_url,
                entry_id=entry_id,
                token=token,
                installation_name=install,
            )
            variants.append((label, alt_url, alt_bm))

        body = render_bookmarklet_page(
            install=install,
            variants=variants,
            source=primary_src,
            ha_url=ha_url,
            entry_id=entry_id,
            token=token,
        )
        return web.Response(text=body, content_type="text/html", charset="utf-8")


# The pure renderer (`render_bookmarklet_page`) and HTML template live in
# `bookmarklet.py` so the unit tests can import them without dragging in
# aiohttp. This module is the thin HomeAssistantView wrapper that
# materialises the response.
