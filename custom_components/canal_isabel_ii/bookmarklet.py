"""Bookmarklet generator for the Canal de Isabel II ingest flow.

The user installs this once as a Safari/Chrome favorite (works on
iOS Safari too — that's the whole point of going bookmarklet over
"native iOS app extension"). When clicked while logged into the
Canal portal, it:

1. Verifies the user is on ``oficinavirtual.canaldeisabelsegunda.es``
   (bails with an alert otherwise).
2. **If the user is already on ``/group/ovir/consumo`` with a rendered
   form** (including any date-range / periodicity filters they've
   applied), reuses that DOM as-is. Otherwise it fetches the page
   fresh. This is what lets the user cherry-pick an arbitrary month
   (e.g. "January 2026") in the portal UI and have the bookmarklet
   honour that filter — the default fetched page only exposes the
   rolling 60-day window.
3. POSTs the form switching periodicity to "Horaria" (hourly) while
   preserving every other field (date inputs, contract, Liferay
   nonces).
4. Finds the ``export-csv`` link in the response, downloads the CSV.
5. Optionally extracts the four-card meter summary from the same HTML.
6. POSTs ``{csv, meter_summary, consumption_page_html}`` as JSON to
   the integration's ``CanalIngestView`` with a Bearer token.
7. Shows the user an alert with the result ("✅ 168 lecturas
   subidas" / "❌ contrato no coincide").

The whole thing runs against the user's own browser cookies — no
captcha, no bot detection, nothing kept alive 24/7.

The generator lives in Python (not as a static .js file) so we can
bake the per-entry HA URL + token + entry id + name into the
``javascript:`` payload at setup time. The user just copies the
resulting string and drags it into their bookmarks bar.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING
from urllib.parse import quote

from .const import BOOKMARKLET_PAGE_URL_PREFIX, INGEST_URL_PREFIX

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

#: Readable bookmarklet body (kept indented for diffability). Placeholders
#: ``__HA_URL__`` ``__ENTRY_ID__`` ``__TOKEN__`` ``__INSTALL__`` are
#: replaced at generate time. Avoid template strings in the JS itself
#: so we don't have to escape backticks / curly braces.
_BOOKMARKLET_TEMPLATE = r"""
(async () => {
  const HA_URL = "__HA_URL__";
  const ENTRY = "__ENTRY_ID__";
  const TOKEN = "__TOKEN__";
  const INSTALL = "__INSTALL__";
  const INGEST = HA_URL.replace(/\/+$/, "") + "__INGEST_PREFIX__/" + ENTRY;
  const log = (m) => { try { console.log("[Canal→HA]", m); } catch (_) {} };
  const fail = (m) => { alert("❌ Canal → HA (" + INSTALL + ")\n\n" + m); };
  try {
    if (!location.hostname.endsWith("canaldeisabelsegunda.es")) {
      fail("Estás en " + location.hostname + ".\n\nAbre primero la Oficina Virtual y vuelve a pulsar el favorito.");
      return;
    }
    let doc1, html1, reusedDom;
    if (document.querySelector("#selectPeriodicidad")) {
      doc1 = document;
      html1 = document.documentElement.outerHTML;
      reusedDom = true;
      log("Reusing current DOM (honours your on-screen filters).");
    } else {
      log("Loading consumption page (no form visible)…");
      const r1 = await fetch("/group/ovir/consumo", { credentials: "include" });
      if (!r1.ok) { fail("Portal no autenticado (HTTP " + r1.status + ").\n\nEntra a la Oficina Virtual y vuelve a pulsar."); return; }
      html1 = await r1.text();
      doc1 = new DOMParser().parseFromString(html1, "text/html");
      reusedDom = false;
    }
    const sel = doc1.querySelector("#selectPeriodicidad");
    if (!sel) { fail("No se encuentra el selector de periodicidad — sesión expirada o portal cambiado."); return; }
    const form = sel.closest("form");
    if (!form || !form.action) { fail("Formulario de consumo sin action — portal cambiado."); return; }
    const fd = new FormData();
    form.querySelectorAll("input, select").forEach((i) => { if (i.name) fd.set(i.name, i.value || ""); });
    fd.set(sel.name, "Horaria");
    let contract = "";
    const cs = doc1.querySelector("#contratosSelect");
    if (cs) {
      const liveVal = reusedDom ? cs.value : "";
      if (liveVal) {
        fd.set(cs.name, liveVal); contract = liveVal;
      } else {
        const opt = cs.querySelector("option[selected]") || cs.querySelector("option");
        if (opt && opt.value) { fd.set(cs.name, opt.value); contract = opt.value; }
      }
    }
    log("Switching to Horaria…");
    const r2 = await fetch(form.action, { method: "POST", body: fd, credentials: "include" });
    if (!r2.ok) { fail("Switch a Horaria falló (HTTP " + r2.status + ")."); return; }
    const html2 = await r2.text();
    const doc2 = new DOMParser().parseFromString(html2, "text/html");
    const csvA = Array.from(doc2.querySelectorAll("a")).find((a) => (a.getAttribute("href") || "").includes("export-csv"));
    if (!csvA) { fail("No se encuentra el enlace export-csv después del switch."); return; }
    let csvUrl = csvA.getAttribute("href");
    if (csvUrl.indexOf("http") !== 0) csvUrl = location.origin + csvUrl;
    log("Downloading CSV…");
    const r3 = await fetch(csvUrl, { credentials: "include" });
    if (!r3.ok) { fail("Descarga del CSV falló (HTTP " + r3.status + ")."); return; }
    const csv = await r3.text();
    if (!csv || csv.length < 40) { fail("CSV vacío o demasiado corto — sin datos en el rango."); return; }
    log("Posting to HA " + INGEST);
    const r4 = await fetch(INGEST, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + TOKEN },
      body: JSON.stringify({
        csv: csv,
        consumption_page_html: html1,
        client_ts: new Date().toISOString(),
        contract_hint: contract
      }),
    });
    let body = null;
    try { body = await r4.json(); } catch (_) { body = { detail: await r4.text() }; }
    if (r4.ok) {
      const meter = body.meter_reading_l != null ? (body.meter_reading_l / 1000).toFixed(3) + " m³" : "—";
      alert("✅ Canal → HA (" + INSTALL + ")\n\n" +
            "Contrato: " + (body.contract || contract) + "\n" +
            "Lecturas importadas: " + body.imported + "\n" +
            "Nuevas: " + body.new + "\n" +
            "Lectura del contador: " + meter);
    } else {
      fail("HTTP " + r4.status + " — " + (body.code || "error") + "\n\n" + (body.detail || "(sin detalle)"));
    }
  } catch (e) {
    fail("Excepción: " + (e && e.message ? e.message : e));
  }
})();
""".strip()


def _minify(src: str) -> str:
    """Crude single-line minify — collapse runs of whitespace, drop
    comments. The bookmarklet is tiny so we don't need a real minifier;
    we mostly want a single line so it pastes cleanly into a Safari
    bookmark URL field (which doesn't tolerate raw newlines).

    JS line-comments (``//…``) are dropped ENTIRELY because the joiner
    is a space, not a newline — a surviving ``//`` would swallow every
    subsequent line of the joined one-liner, silently breaking the
    bookmarklet. We treat a line as a comment when the first
    non-whitespace characters are ``//``. (Inline trailing ``// …``
    comments are NOT stripped — avoid using them in the template; put
    docs in the Python docstring instead.) The template deliberately
    contains no `://` URL literals so this rule never misfires.

    Keeps string contents intact (no escape-sensitive transforms).
    """
    out_lines: list[str] = []
    for line in src.splitlines():
        s = line.strip()
        if not s:
            continue
        # Pure-comment line — drop entirely so the joiner doesn't
        # produce a runaway comment that eats the rest of the code.
        if s.startswith("//"):
            continue
        out_lines.append(s)
    joined = " ".join(out_lines)
    # Collapse multi-space runs that the join may have introduced.
    while "  " in joined:
        joined = joined.replace("  ", " ")
    return joined


def build_bookmarklet(
    *,
    ha_url: str,
    entry_id: str,
    token: str,
    installation_name: str,
) -> str:
    """Return a ``javascript:…`` URL ready to paste into a browser bookmark.

    The URL is fully self-contained: all four parameters are baked in.
    Re-generate when any of them changes (token rotation, HA URL move,
    entry rename).

    Note we ``urllib.parse.quote`` the JS body so characters like ``"``
    survive a bookmark-bar paste in browsers that aggressively escape
    URL chars. Safari treats ``javascript:`` URLs liberally but Chrome
    on some platforms is stricter. Quoting is safe in both.
    """
    ha_url = (ha_url or "").rstrip("/")
    body = (
        _minify(_BOOKMARKLET_TEMPLATE)
        .replace("__HA_URL__", _js_string_safe(ha_url))
        .replace("__ENTRY_ID__", _js_string_safe(entry_id))
        .replace("__TOKEN__", _js_string_safe(token))
        .replace("__INSTALL__", _js_string_safe(installation_name))
        .replace("__INGEST_PREFIX__", _js_string_safe(INGEST_URL_PREFIX))
    )
    return "javascript:" + quote(body, safe="(){}[]=;,:!?+-*/&|<>'.\"")


def build_bookmarklet_source(
    *,
    ha_url: str,
    entry_id: str,
    token: str,
    installation_name: str,
) -> str:
    """Return the readable JS body (no ``javascript:`` prefix, multi-line).

    Useful for the user who wants to inspect what they're pasting
    BEFORE installing — surfaced in the README + the config-flow
    success page.
    """
    ha_url = (ha_url or "").rstrip("/")
    return (
        _BOOKMARKLET_TEMPLATE.replace("__HA_URL__", _js_string_safe(ha_url))
        .replace("__ENTRY_ID__", _js_string_safe(entry_id))
        .replace("__TOKEN__", _js_string_safe(token))
        .replace("__INSTALL__", _js_string_safe(installation_name))
        .replace("__INGEST_PREFIX__", _js_string_safe(INGEST_URL_PREFIX))
    )


def _js_string_safe(s: str) -> str:
    """Escape backslashes + double-quotes so the value can be dropped
    inside a JS double-quoted string literal without breaking the
    bookmarklet."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def collect_alternate_urls(hass: HomeAssistant, primary_url: str) -> list[tuple[str, str]]:
    """Return ``[(label, url), ...]`` for HA-configured URLs that differ from
    the primary one baked into this entry's bookmarklet.

    Looks at ``hass.config.internal_url`` and ``hass.config.external_url``;
    labels them as LAN / externo based on which slot they live in. If a
    value equals the primary (after trailing-slash normalisation) or is
    empty, it's skipped.

    Exposed at module level (not inside ``__init__``) so both the
    notification publisher and the HTML-page view can render the same set
    of variants without a circular import.
    """
    normalised_primary = (primary_url or "").rstrip("/")
    seen = {normalised_primary}
    out: list[tuple[str, str]] = []

    internal = (hass.config.internal_url or "").rstrip("/")
    external = (hass.config.external_url or "").rstrip("/")

    if internal and internal not in seen:
        out.append(("Uso en LAN (desde tu WiFi de casa)", internal))
        seen.add(internal)
    if external and external not in seen:
        out.append(("Uso externo (desde fuera de casa)", external))
        seen.add(external)

    return out


def bookmarklet_page_url(entry_id: str, token: str) -> str:
    """Return the relative URL to the per-entry bookmarklet install page.

    The page is served by the integration's
    :class:`CanalBookmarkletPageView` and carries the drag-to-bookmark
    link + the copy-to-clipboard button.

    The ``token`` is appended as a ``?t=`` query param. The view runs
    with ``requires_auth=False`` and validates the token in
    constant-time (`secrets.compare_digest`) against the entry's stored
    Bearer token. We need this auth path because a plain browser
    navigation from a notification link (which is what the user
    actually does) does NOT carry the ``Authorization: Bearer`` header
    that HA's normal `requires_auth=True` machinery expects — the
    Bearer token lives in HA's frontend localStorage and only travels
    on requests issued by frontend JS, not on user-driven URL
    navigations. Without the ``?t=…`` shortcut the page returns 401.

    Putting the token in the query string doesn't widen the attack
    surface: the same token is already baked verbatim into the
    bookmarklet body the page exposes, and into every install copy the
    user makes. Anyone who has the page URL also has the bookmarklet
    URL — symmetric exposure.
    """
    return f"{BOOKMARKLET_PAGE_URL_PREFIX}/{entry_id}?t={quote(token, safe='')}"


def format_install_notification(
    *,
    install: str,
    bookmarklet: str,
    ha_url: str,
    entry_id: str,
    token: str,
    source: str,
    alternates: list[tuple[str, str, str]] | None = None,
) -> str:
    """Render the persistent-notification body shown right after
    install (and re-shown by the ``show_bookmarklet`` service).

    The notification is short on purpose: the awful UX of copying a
    ``javascript:…`` URL out of a Markdown code block (especially on iOS
    Safari, where dragging selection markers across hundreds of escaped
    characters is a misery) is now handled by a dedicated HTML page
    served by :class:`CanalBookmarkletPageView`. The page has a
    drag-to-bookmarks-bar link AND a one-tap copy-to-clipboard button.

    The notification just points at that page. We keep the raw
    bookmarklet inside a collapsed ``<details>`` block as a fallback
    in case the page can't be opened (extreme HA misconfiguration).

    ``alternates`` (LAN/external variants) is rendered into the page
    too — the notification doesn't need to enumerate them itself.

    Markdown-friendly so the persistent_notification card renders the
    bullets, the bold install button, and the collapsible block
    correctly.
    """
    page_url = bookmarklet_page_url(entry_id, token)
    alternates_hint = ""
    if alternates:
        labels = ", ".join(label for label, _url, _bm in alternates)
        alternates_hint = f"\n_La página incluye variantes adicionales para tus URLs: {labels}._\n"

    return (
        f"## Bookmarklet listo — {install}\n\n"
        "Tu bookmarklet ya está generado. La forma más cómoda de instalarlo es "
        "desde la página HTML que la integración acaba de exponer:\n\n"
        f"### → [📥 Abrir página de instalación]({page_url})\n\n"
        "En esa página tienes:\n"
        "- un botón **📋 Copiar bookmarklet** (un solo toque, "
        "funciona en iOS Safari);\n"
        "- un enlace **★ Canal → HA** que arrastras a la barra de favoritos "
        "en escritorio;\n"
        "- el código fuente legible y los datos técnicos."
        f"{alternates_hint}\n"
        "### Una vez instalado el favorito\n"
        "1. Abre <https://oficinavirtual.canaldeisabelsegunda.es> e inicia "
        "sesión (DNI + contraseña + captcha si aparece).\n"
        "2. Asegúrate de que el dropdown de contrato muestra el correcto.\n"
        "3. *(Opcional)* Para importar un mes histórico, en **Mi consumo** "
        "filtra ese rango con frecuencia **Horaria** y pulsa **Ver** antes "
        "de pulsar el favorito.\n"
        "4. Pulsa el favorito → verás un alert con el resumen.\n\n"
        "⚠️ **Un bookmarklet ↔ un contrato.** Si tienes varios contratos en "
        "el portal, añade *otra integración* Canal de Isabel II por cada uno "
        "(no mezcles bookmarklets — el endpoint rechazará el segundo "
        "contrato con HTTP 409).\n\n"
        "<details><summary>Si la página no abre — bookmarklet en bruto</summary>\n\n"
        "Datos técnicos:\n\n"
        f"- **URL HA**: `{ha_url}`\n"
        f"- **Entry ID**: `{entry_id}`\n"
        f"- **Token**: `{token}`\n"
        f"- **Endpoint ingest**: `{ha_url}/api/canal_isabel_ii/ingest/{entry_id}`\n\n"
        "Bookmarklet minificado (cópialo y pégalo en la URL de un favorito):\n\n"
        f"```\n{bookmarklet}\n```\n\n"
        "Código fuente legible:\n\n"
        "```javascript\n"
        f"{source}\n"
        "```\n"
        "</details>"
    )


# ---------------------------------------------------------------------
# HTML install page renderer
# ---------------------------------------------------------------------
#
# The renderer lives here (next to the bookmarklet builders) instead of
# in ``bookmarklet_view.py`` so we can unit-test it without dragging in
# aiohttp. ``bookmarklet_view.py`` is the thin HomeAssistantView wrapper
# that actually serves the page; it does ``from .bookmarklet import
# render_bookmarklet_page`` and just calls this. Keeping the pure
# rendering logic stdlib-only means the test rig (which never installs
# aiohttp) can import the renderer directly.


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
