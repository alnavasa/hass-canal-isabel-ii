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


def bookmarklet_page_url(entry_id: str) -> str:
    """Return the relative URL to the per-entry bookmarklet install page.

    The page is served by the integration's
    :class:`CanalBookmarkletPageView` and carries the drag-to-bookmark
    link + the copy-to-clipboard button."""
    return f"{BOOKMARKLET_PAGE_URL_PREFIX}/{entry_id}"


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
    page_url = bookmarklet_page_url(entry_id)
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
