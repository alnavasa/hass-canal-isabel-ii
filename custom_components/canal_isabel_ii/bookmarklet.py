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

from urllib.parse import quote

from .const import INGEST_URL_PREFIX

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
    // Prefer the current DOM if the user is already on the consumption
    // page (any filter they've applied — month, date range — is
    // encoded in the live form inputs). Fall back to fetching the page
    // only when the form isn't present in the document (user is on a
    // different page of the portal).
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
    // Capture every named input + select in the form — that's how
    // the user's date range / month / year selections get carried
    // through. SELECTs expose `.value` = currently-selected option's
    // value in both live and parsed DOMs.
    form.querySelectorAll("input, select").forEach((i) => { if (i.name) fd.set(i.name, i.value || ""); });
    fd.set(sel.name, "Horaria");
    let contract = "";
    const cs = doc1.querySelector("#contratosSelect");
    if (cs) {
      // Live DOM: cs.value is the selected option. Parsed DOM: prefer
      // the [selected] option, else fall back to the first one.
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

    Keeps string contents intact (no escape-sensitive transforms).
    """
    out_lines: list[str] = []
    for line in src.splitlines():
        s = line.strip()
        if not s:
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

    ``alternates`` is an optional list of ``(label, url, bookmarklet)``
    tuples for additional HA URLs (e.g. a LAN ``internal_url`` when the
    primary is a public ``external_url``). Each alternate is rendered
    as its own copy-ready code block so the user can pick the one that
    matches where they'll click the favorite from. All alternates
    share the same ``entry_id`` + ``token`` — any of them hits the
    same integration entry.

    Markdown-friendly so the persistent_notification card renders the
    code blocks correctly. Keeps the full minified bookmarklet
    inline — current size (~1.5 KB) is well under HA's notification
    truncation threshold.
    """
    alt_blocks = ""
    if alternates:
        alt_blocks += (
            "\n### Variantes adicionales (copia la que te convenga)\n\n"
            "El `entry_id` y el token son los mismos — lo único que cambia "
            "es a qué URL de tu HA apunta el POST. Instala **uno** (el que "
            "uses más) o **los dos** con nombres distintos (p.ej. "
            "*Canal → HA (LAN)* y *Canal → HA (externo)*).\n\n"
        )
        for label, alt_url, alt_bm in alternates:
            alt_blocks += f"**{label}** — apunta a `{alt_url}`\n\n```\n{alt_bm}\n```\n\n"

    return (
        f"## Integración creada — {install}\n\n"
        "### Paso 1 · Crea un favorito en tu navegador\n"
        "**Mac Safari**: Marcadores → *Añadir marcador…* → guarda esta página "
        "(cualquier página vale temporalmente). Luego edita ese marcador y "
        "pega lo siguiente en el campo URL:\n\n"
        f"```\n{bookmarklet}\n```\n\n"
        "**iOS Safari**: pulsa el botón **Compartir** (cuadrado con flecha) → "
        "*Añadir marcador*. Luego ve a Marcadores → *Editar*, abre ese marcador "
        "y reemplaza la URL con el bloque de arriba (mantén pulsado para pegar).\n\n"
        "**Chrome / Firefox**: arrastra cualquier favorito existente a la barra, "
        "haz click derecho → *Editar*, y pega el bloque de arriba en la URL.\n"
        f"{alt_blocks}"
        "### Paso 2 · Úsalo\n"
        "1. Abre <https://oficinavirtual.canaldeisabelsegunda.es> e inicia sesión "
        "como siempre (DNI + contraseña + captcha si aparece).\n"
        "2. Asegúrate de que el dropdown de contrato (arriba a la derecha) "
        "muestra el contrato que quieres importar a esta integración.\n"
        "3. *(Opcional, para histórico)* Si quieres importar un mes concreto del "
        "pasado, entra en **Mi consumo**, filtra el rango de fechas deseado "
        "(p.ej. del 1 al 31 de enero) con frecuencia **Horaria**, y pulsa "
        "**Ver** para que la tabla se actualice. El bookmarklet leerá tu "
        "selección actual.\n"
        "4. Pulsa el favorito que acabas de crear.\n"
        '5. Verás un alert con el resumen: "Canal -> HA - Contrato: ..., '
        'Lecturas importadas: ...".\n'
        "6. Vuelve a HA y verás los sensores creados en *Ajustes -> "
        "Dispositivos y servicios -> Canal de Isabel II*.\n\n"
        "### Datos técnicos (por si se te pierde el bookmarklet)\n"
        f"- **URL HA**: `{ha_url}`\n"
        f"- **Entry ID**: `{entry_id}`\n"
        f"- **Token**: `{token}`\n"
        f"- **Endpoint**: `{ha_url}/api/canal_isabel_ii/ingest/{entry_id}`\n\n"
        "<details><summary>Código JavaScript legible (sin minificar)</summary>\n\n"
        "```javascript\n"
        f"{source}\n"
        "```\n"
        "</details>\n\n"
        "### Cuándo pulsar el bookmarklet\n"
        "- **Setup inicial**: ahora mismo, para importar los últimos 60 días (el "
        "rango por defecto de la Oficina Virtual).\n"
        "- **Histórico de meses anteriores**: filtra el rango que quieras en la "
        "Oficina Virtual (p.ej. enero entero), pulsa **Ver** para aplicar el "
        "filtro, y luego pulsa el favorito. La integración detecta los días "
        "nuevos y los mete en las estadísticas horarias retroactivamente — "
        "aparecerán en el panel Energía → Agua.\n"
        "- **Mantenimiento**: 1-2 veces por semana es suficiente. La integración "
        "hace upsert idempotente, no duplica datos.\n"
        "- **Mismo contrato siempre**: este bookmarklet está vinculado al "
        "contrato que selecciones en la primera pulsación. Si tienes varios "
        "contratos, añade otra integración Canal de Isabel II para cada uno."
    )
