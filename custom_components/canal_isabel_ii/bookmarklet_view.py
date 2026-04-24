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

    GET /api/canal_isabel_ii/bookmarklet/{entry_id}

Auth
----

``requires_auth = True`` — the user reaches this page from a link inside
the HA UI, so they're already authenticated by HA's session cookie. The
page itself never executes anything privileged; it's a static HTML
render of values already stored in ``entry.data``. (The bookmarklet that
the page exposes still authenticates against the ingest endpoint via its
own per-entry Bearer token, exactly as before.)

Why this isn't a Lovelace card
------------------------------

The notification fires on first config + can be re-fired by the
``show_bookmarklet`` service. It links to ``/api/canal_isabel_ii/bookmarklet/<entry_id>`` directly — no Lovelace card to install,
no add-on, no third-party panel. Just a single HTML page rendered by
the integration on demand.
"""

from __future__ import annotations

import html
import logging

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
    requires_auth = True  # User clicks from inside HA, cookie auth is fine.

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
