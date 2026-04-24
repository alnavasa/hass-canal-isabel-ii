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


# ---------------------------------------------------------------------
# Pure renderer — easy to unit-test without a hass fixture.
# ---------------------------------------------------------------------


def render_bookmarklet_page(
    *,
    install: str,
    variants: list[tuple[str, str, str]],
    source: str,
    ha_url: str,
    entry_id: str,
    token: str,
) -> str:
    """Render the install-page HTML body.

    ``variants`` is a list of ``(label, ha_url, bookmarklet)`` tuples.
    Every tuple gets its own draggable link + copy button. With more
    than one variant each gets its own labelled section; with exactly
    one we drop the section heading to avoid awkward "Por defecto"
    visual noise.

    The ``href`` and ``data-bookmarklet`` attributes carry the
    URL-encoded bookmarklet payload, additionally HTML-escaped so
    characters like ``"`` and ``&`` survive the attribute. The JS
    reads ``btn.dataset.bookmarklet`` (decoded automatically by the
    browser's HTML parser) and feeds it straight to the clipboard API.
    """
    install_e = html.escape(install)

    variant_blocks: list[str] = []
    show_section_label = len(variants) > 1
    for label, var_url, bm in variants:
        label_e = html.escape(label)
        var_url_e = html.escape(var_url) if var_url else "(URL relativa)"
        bm_attr = html.escape(bm, quote=True)
        link_label_suffix = f" · {label_e}" if show_section_label else ""
        link_text = f"★ Canal → HA · {install_e}{link_label_suffix}"

        section_heading = (
            f'<h2>{label_e}</h2>\n  <p class="muted">apunta a <code>{var_url_e}</code></p>\n'
            if show_section_label
            else ""
        )

        variant_blocks.append(
            '<section class="variant">\n'
            f"  {section_heading}"
            "  <p><strong>A) Arrastra a la barra de favoritos:</strong></p>\n"
            f'  <a class="drag-link" href="{bm_attr}" draggable="true"'
            f' data-bookmarklet="{bm_attr}">{link_text}</a>\n'
            '  <p class="muted small">'
            "Pulsa con el botón izquierdo y arrastra hasta la barra de marcadores. "
            "<strong>NO hagas click suelto</strong> — si pulsas, el bookmarklet "
            "intentará ejecutarse aquí mismo (donde no hay sesión del Canal) y "
            "no hará nada útil."
            "</p>\n"
            "  <p><strong>B) Copia y pega manualmente:</strong></p>\n"
            '  <button class="copy-btn" type="button"'
            f' data-bookmarklet="{bm_attr}">📋 Copiar bookmarklet</button>\n'
            '  <p class="muted small">Crea un favorito cualquiera en tu navegador, '
            "edita su URL y pega lo copiado.</p>\n"
            "</section>"
        )

    variants_html = "\n".join(variant_blocks)

    source_e = html.escape(source)
    ha_url_e = html.escape(ha_url) if ha_url else "(no configurada)"
    entry_id_e = html.escape(entry_id)
    token_e = html.escape(token)
    endpoint = f"{ha_url}/api/canal_isabel_ii/ingest/{entry_id}" if ha_url else "(no configurada)"
    endpoint_e = html.escape(endpoint)

    # NOTE: this template is built with str.replace placeholders rather
    # than f-strings to keep CSS/JS literal `{` and `}` un-doubled. Way
    # easier to maintain than escaping every brace in the stylesheet.
    template = _PAGE_TEMPLATE
    return (
        template.replace("__INSTALL__", install_e)
        .replace("__VARIANTS__", variants_html)
        .replace("__SOURCE__", source_e)
        .replace("__HA_URL__", ha_url_e)
        .replace("__ENTRY_ID__", entry_id_e)
        .replace("__TOKEN__", token_e)
        .replace("__ENDPOINT__", endpoint_e)
    )


_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Canal → HA · __INSTALL__</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff;
    --fg: #1f2328;
    --muted: #57606a;
    --accent: #0969da;
    --accent-fg: #ffffff;
    --ok: #1f883d;
    --warn-bg: rgba(212, 167, 44, 0.12);
    --warn-border: #d4a72c;
    --code-bg: #f6f8fa;
    --border: #d0d7de;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117;
      --fg: #e6edf3;
      --muted: #8b949e;
      --accent: #2f81f7;
      --accent-fg: #ffffff;
      --ok: #3fb950;
      --warn-bg: rgba(212, 167, 44, 0.18);
      --warn-border: #d4a72c;
      --code-bg: #161b22;
      --border: #30363d;
    }
  }
  html, body { background: var(--bg); color: var(--fg); }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    max-width: 760px;
    margin: 2rem auto;
    padding: 0 1rem 4rem;
    line-height: 1.55;
  }
  h1 { margin-top: 0; }
  h2 { margin-top: 0; }
  .muted { color: var(--muted); }
  .small { font-size: 0.85rem; margin-top: 0.4rem; }
  code {
    background: var(--code-bg);
    padding: 0.1rem 0.4rem;
    border-radius: 4px;
    font-size: 0.9em;
  }
  pre {
    background: var(--code-bg);
    padding: 1rem;
    border-radius: 6px;
    overflow-x: auto;
    border: 1px solid var(--border);
    font-size: 0.85rem;
    white-space: pre;
  }
  .variant {
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1.25rem;
    margin: 1rem 0;
    background: var(--bg);
  }
  .drag-link {
    display: inline-block;
    padding: 0.6rem 1rem;
    background: var(--accent);
    color: var(--accent-fg);
    text-decoration: none;
    border-radius: 6px;
    font-weight: 600;
    margin: 0.25rem 0;
    cursor: grab;
    user-select: none;
  }
  .drag-link:active { cursor: grabbing; }
  .copy-btn {
    appearance: none;
    background: var(--accent);
    color: var(--accent-fg);
    border: none;
    padding: 0.6rem 1rem;
    border-radius: 6px;
    font-weight: 600;
    cursor: pointer;
    font-size: 1rem;
    margin: 0.25rem 0;
  }
  .copy-btn.ok { background: var(--ok); }
  details { margin: 1rem 0; }
  summary { cursor: pointer; font-weight: 600; }
  .warn {
    border-left: 4px solid var(--warn-border);
    background: var(--warn-bg);
    padding: 0.75rem 1rem;
    margin: 1rem 0;
    border-radius: 4px;
  }
  table.kv {
    border-collapse: collapse;
    width: 100%;
    font-size: 0.9rem;
    margin: 0.5rem 0;
  }
  table.kv td {
    padding: 0.4rem 0.5rem;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
    word-break: break-all;
  }
  table.kv td:first-child {
    font-weight: 600;
    width: 9rem;
    color: var(--muted);
    word-break: normal;
  }
</style>
</head>
<body>
<h1>Canal → HA · __INSTALL__</h1>
<p>El bookmarklet que conecta la <strong>Oficina Virtual del Canal de Isabel II</strong>
con tu Home Assistant está listo. Instálalo de cualquiera de estas dos formas:</p>

__VARIANTS__

<div class="warn">
  <strong>Un bookmarklet ↔ un contrato.</strong> Si tienes <em>varios contratos</em>
  en el portal, añade <em>otra integración</em> Canal de Isabel II por cada uno
  (no mezcles bookmarklets — el endpoint rechazará el segundo contrato con
  HTTP 409 + notificación).
</div>

<details>
  <summary>Cómo usarlo (paso a paso)</summary>
  <ol>
    <li>Abre <a href="https://oficinavirtual.canaldeisabelsegunda.es" target="_blank" rel="noopener">oficinavirtual.canaldeisabelsegunda.es</a> e inicia sesión.</li>
    <li>Asegúrate de que el dropdown de contrato muestra el correcto.</li>
    <li><em>Opcional (histórico)</em>: en <strong>Mi consumo</strong>, filtra el rango de fechas que quieras (p.ej. enero entero) con frecuencia <strong>Horaria</strong> y pulsa <strong>Ver</strong>.</li>
    <li>Pulsa el favorito que acabas de crear.</li>
    <li>Verás un alert con el resumen («Lecturas importadas: …, Nuevas: …»).</li>
    <li>Vuelve a HA: los sensores y el panel <em>Energía → Agua</em> se rellenan solos.</li>
  </ol>
</details>

<details>
  <summary>Datos técnicos</summary>
  <table class="kv">
    <tr><td>URL HA</td><td><code>__HA_URL__</code></td></tr>
    <tr><td>Entry ID</td><td><code>__ENTRY_ID__</code></td></tr>
    <tr><td>Token</td><td><code>__TOKEN__</code></td></tr>
    <tr><td>Endpoint ingest</td><td><code>__ENDPOINT__</code></td></tr>
  </table>
</details>

<details>
  <summary>Código JavaScript legible (sin minificar)</summary>
  <pre><code>__SOURCE__</code></pre>
</details>

<script>
(function () {
  const original = "📋 Copiar bookmarklet";

  document.querySelectorAll(".copy-btn").forEach(function (btn) {
    btn.addEventListener("click", async function () {
      const text = btn.dataset.bookmarklet || "";
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "✅ Copiado al portapapeles";
        btn.classList.add("ok");
        setTimeout(function () {
          btn.textContent = original;
          btn.classList.remove("ok");
        }, 2500);
      } catch (e) {
        // Fallback for browsers that block the Clipboard API outside
        // of user gestures or in non-https contexts. window.prompt
        // shows a pre-selected text the user can Cmd-C / long-press
        // copy from.
        window.prompt("Copia este bookmarklet:", text);
      }
    });
  });

  // Block accidental clicks on the draggable link — clicking would
  // try to execute the javascript: URL in the context of the HA UI,
  // which has no Canal session and would just fail silently.
  document.querySelectorAll(".drag-link").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      alert("Arrastra este enlace a tu barra de marcadores en lugar de pulsarlo.\\n\\n" +
            "Si lo pulsas aquí, el bookmarklet se ejecuta en HA (que no tiene " +
            "sesión del Canal) y no hace nada útil.");
    });
  });
})();
</script>
</body>
</html>
"""
