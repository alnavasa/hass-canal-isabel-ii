"""Pure HTML parser for the consumption-page meter summary panel.

The portal renders four ``<li>`` cards above the chart:

    <li>… Dirección suministro … <h5 class="dato dato1">…</h5></li>
    <li>… Contador             … <h5 class="dato dato1">…</h5></li>
    <li>… Última lectura       … <h5 class="dato minusculas">56,735m³</h5></li>
    <li>… Fecha y hora lectura … <h5 class="dato dato1">22/04/2026 03:00</h5></li>

This module extracts those four values and packages them as a
:class:`models.MeterSummary`. Pure — no HA, no aiohttp, just an HTML
string in and a dataclass out — so the bookmarklet ingest pipeline
can be driven entirely by pytest without a live HA.

Why not BeautifulSoup?
We keep the integration's runtime dependencies tiny —
``manifest.json`` should not pull bs4 just for one parser. The portal
HTML is regular enough that a regex pass over the four ``<li>`` blocks
is reliable. If the structure ever changes drastically we'd need to
reconsider — but the same is true for the bs4 selectors anyway.
"""

from __future__ import annotations

import re
from datetime import datetime

from .models import MeterSummary

#: Matches the meter reading text "56,735m³" / "56,735 m3" / etc.
#: Spanish locale uses "," as decimal separator; the "." (if present)
#: is the thousands separator. The unit is always m³ on this portal so
#: we strip it before normalising.
_METER_VALUE_RE = re.compile(r"([\d.,]+)\s*m", re.I)
_DMY_HM_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})$")

#: Greedy block matcher for the four <li> cards. We pull every
#: ``<li>…</li>`` and look for a (titulo, dato) pair inside.
_LI_BLOCK_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_TITULO_RE = re.compile(
    r'<h5[^>]*class="[^"]*titulo[^"]*"[^>]*>(.*?)</h5>',
    re.IGNORECASE | re.DOTALL,
)
_DATO_RE = re.compile(
    r'<h5[^>]*class="[^"]*\bdato\b[^"]*"[^>]*>(.*?)</h5>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def parse_meter_value_to_liters(raw: str) -> float | None:
    """Spanish-localised "56,735m³" → 56735.0 L.

    Public so the ingest endpoint can call it on a plain raw string
    coming from the bookmarklet payload (which sometimes ships the
    pre-extracted text instead of the whole HTML — depends on the
    bookmarklet version).
    """
    if not raw:
        return None
    match = _METER_VALUE_RE.search(raw)
    if not match:
        return None
    token = match.group(1).strip()
    if "," in token:
        token = token.replace(".", "").replace(",", ".")
    try:
        m3 = float(token)
    except ValueError:
        return None
    return m3 * 1000.0  # m³ → L


def parse_dmy_hm(raw: str) -> datetime | None:
    """Parse "22/04/2026 03:00" as a naive local-Madrid datetime."""
    if not raw:
        return None
    m = _DMY_HM_RE.match(raw.strip())
    if not m:
        return None
    day, month, year, hour, minute = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _strip_tags(html_fragment: str) -> str:
    """Naïve "remove HTML tags + collapse whitespace" — good enough
    for the small chunks inside <h5>, where we expect plain text + at
    most a <span>."""
    no_tags = _TAG_STRIP_RE.sub("", html_fragment)
    return _WS_RE.sub(" ", no_tags).strip()


def parse_meter_summary_from_html(html: str) -> MeterSummary | None:
    """Extract the meter summary panel from a consumption-page HTML.

    Returns ``None`` if the page doesn't contain the expected pattern
    (e.g. user landed on the login page because the session expired,
    or Canal changed the markup). Callers should treat ``None`` as
    "no summary this round" rather than as an error — the absolute
    meter sensor falls back to its last good value.
    """
    if not html:
        return None
    fields: dict[str, str] = {}
    for li_html in _LI_BLOCK_RE.findall(html):
        titulo_match = _TITULO_RE.search(li_html)
        dato_match = _DATO_RE.search(li_html)
        if not titulo_match or not dato_match:
            continue
        titulo = _strip_tags(titulo_match.group(1)).lower()
        dato = _strip_tags(dato_match.group(1))
        if not titulo or not dato:
            continue
        if "dirección" in titulo or "direccion" in titulo:
            fields["address"] = dato
        elif "contador" in titulo:
            fields["meter"] = dato
        elif "última lectura" in titulo or "ultima lectura" in titulo:
            fields["raw_reading"] = dato
        elif "fecha" in titulo and "lectura" in titulo:
            fields["raw_reading_at"] = dato
    return _build_summary(fields)


def parse_meter_summary_from_dict(raw: object) -> MeterSummary | None:
    """Build a MeterSummary from a pre-parsed dict.

    Kept for the path where the bookmarklet does the four-field
    extraction client-side and posts a JSON object directly. Accepts
    keys ``reading_liters``, ``reading_at`` (ISO), ``meter``,
    ``address``, ``raw_reading`` — anything else is ignored. Returns
    ``None`` if ``reading_liters`` is missing or unparseable.
    """
    if not isinstance(raw, dict):
        return None
    try:
        liters = float(raw["reading_liters"])
    except (KeyError, TypeError, ValueError):
        return None
    reading_at: datetime | None
    raw_at = raw.get("reading_at")
    if raw_at:
        try:
            reading_at = datetime.fromisoformat(str(raw_at))
        except (TypeError, ValueError):
            reading_at = None
    else:
        reading_at = None
    return MeterSummary(
        reading_liters=liters,
        reading_at=reading_at,
        meter=str(raw.get("meter") or ""),
        address=str(raw.get("address") or ""),
        raw_reading=str(raw.get("raw_reading") or ""),
    )


def _build_summary(fields: dict[str, str]) -> MeterSummary | None:
    raw_reading = fields.get("raw_reading", "")
    liters = parse_meter_value_to_liters(raw_reading)
    if liters is None:
        return None
    reading_at = parse_dmy_hm(fields.get("raw_reading_at", ""))
    return MeterSummary(
        reading_liters=liters,
        reading_at=reading_at,
        meter=fields.get("meter", ""),
        address=fields.get("address", ""),
        raw_reading=raw_reading,
    )
