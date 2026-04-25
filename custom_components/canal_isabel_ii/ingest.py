"""HTTP endpoint that receives CSV payloads from the bookmarklet.

This is the heart of the integration: the user's browser already has
a live portal session (cookies, anti-bot challenges solved, etc.), so
the bookmarklet does the CSV download client-side and POSTs the
result here. The integration validates, parses, persists and pushes
statistics — no headless browser kept alive in the background, no WAF
that learns to fingerprint our scraper, no captcha to defeat.

URL:
    POST /api/canal_isabel_ii/ingest/{entry_id}

Auth:
    Bearer token in the Authorization header. The token is generated
    at config-flow time and stored in ``entry.data[CONF_TOKEN]``. We
    compare with :func:`secrets.compare_digest` to avoid leaking the
    valid prefix length via timing.

Body (JSON):
    {
        "csv": "<raw Canal CSV bytes as text>",
        "meter_summary": {                 # optional — bookmarklet may
            "reading_liters": 56735.0,     # post the four pre-parsed
            "reading_at": "2026-04-22T03:00:00",
            "meter": "Y20HK123456",
            "address": "C/ Ejemplo 1",
            "raw_reading": "56,735m³"
        },
        "consumption_page_html": "<full HTML of /group/ovir/consumo>",
                                            # optional — fallback when
                                            # meter_summary is missing
        "client_ts": "2026-04-23T10:42:00Z" # optional — diagnostic
    }

Response (JSON):
    HTTP 200 → {"ok": true, "imported": 168, "new": 24, "contract": "..."}
    HTTP 4xx → {"ok": false, "code": "...", "detail": "human msg"}

CONTRACT-MIXING SAFEGUARDS
==========================

The user's portal account may expose more than one contract under
the same login. Each contract must end up in its own integration
entry (one device + one set of sensors per contract). To prevent
data corruption from a misclick:

1. **First successful POST** auto-binds the entry to the contract
   present in the CSV. Stored in ``entry.data[CONF_CONTRACT]``.
2. **Subsequent POSTs** must carry the same contract id — anything
   else returns HTTP 409 ``contract_mismatch`` and fires a persistent
   notification telling the user to either select the right
   contract in the portal dropdown OR add a separate integration
   entry for the other contract.
3. **Multiple contracts in one CSV** is rejected outright (HTTP 400
   ``multiple_contracts``) — the bookmarklet always selects one
   contract before fetching the CSV, so this means the user clicked
   it before the dropdown was applied. Easy fix on the user side
   ("hazlo otra vez con el dropdown ya seleccionado").
4. The CSV's ``Contrato`` column is the source of truth — we don't
   trust an explicit ``contract`` field in the JSON body even if
   present. The CSV is what the portal actually returned to the
   bookmarklet; metadata can lie, the data can't.

This is the user's explicit ask: "ojo presentar algo para evitar
mezclar contratos o que ACUTO detecte que es otro contrato".
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONTRACT,
    CONF_NAME,
    DOMAIN,
    INGEST_URL_PREFIX,
    MAX_INGEST_BYTES,
)
from .csv_parser import parse_csv
from .meter_summary_parser import (
    parse_meter_summary_from_dict,
    parse_meter_summary_from_html,
)

_LOGGER = logging.getLogger(__name__)


class CanalIngestView(HomeAssistantView):
    """POST endpoint for bookmarklet uploads.

    Registered once per HA boot in ``async_setup``. Multi-tenant:
    every entry shares this view, identified by ``entry_id`` in the
    URL path. Auth is per-entry via the Bearer token.

    CORS: ``cors_allowed = True`` opts this view into HA's global
    ``KEY_ALLOW_ALL_CORS`` bucket, which wires an ``aiohttp_cors``
    preflight handler onto the route (``Access-Control-Allow-Origin:
    *``, all methods, standard headers). That's exactly what the
    bookmarklet needs — no cookies, just a Bearer token, so ``*`` is
    safe. Do NOT add a custom ``options()`` method here: HA already
    attaches one via ``aiohttp_cors`` on registration, and aiohttp
    refuses two OPTIONS handlers on the same route
    (``ValueError: <...> already has OPTIONS handler``).
    """

    url = f"{INGEST_URL_PREFIX}/{{entry_id}}"
    name = "api:canal_isabel_ii:ingest"
    requires_auth = False  # We use our own Bearer token; HA session auth not relevant.
    cors_allowed = True  # Let HA's aiohttp_cors handle the preflight + response headers.

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        # Reject obvious garbage early to avoid touching the JSON parser.
        if request.content_length is not None and request.content_length > MAX_INGEST_BYTES:
            return _error(
                request,
                413,
                "payload_too_large",
                f"Payload exceeds {MAX_INGEST_BYTES} bytes.",
            )

        entry_data = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if not entry_data:
            return _error(
                request,
                404,
                "unknown_entry",
                "No integration entry matches this URL. Check the bookmarklet target.",
            )

        # Constant-time token check.
        provided = _extract_bearer(request)
        expected = entry_data.get("token", "")
        if not provided or not expected or not secrets.compare_digest(provided, expected):
            return _error(
                request,
                401,
                "invalid_token",
                "Bearer token missing or invalid.",
            )

        # Enforce the size cap once the body is read (Content-Length may
        # be absent for chunked transfers).
        try:
            raw_body = await request.read()
        except Exception as exc:
            return _error(request, 400, "read_failed", f"Failed to read body: {exc}")
        if len(raw_body) > MAX_INGEST_BYTES:
            return _error(
                request,
                413,
                "payload_too_large",
                f"Payload exceeds {MAX_INGEST_BYTES} bytes after read.",
            )

        try:
            payload = await request.json()
        except Exception as exc:
            return _error(request, 400, "invalid_json", f"JSON parse failed: {exc}")

        if not isinstance(payload, dict):
            return _error(request, 400, "invalid_payload", "Top-level body must be an object.")

        csv_text = payload.get("csv") or ""
        if not isinstance(csv_text, str) or not csv_text.strip():
            return _error(request, 400, "missing_csv", "Field 'csv' is required and non-empty.")

        # ------------------------------------------------------------------
        # Parse the CSV — this also tells us the contract id.
        # ------------------------------------------------------------------
        readings = parse_csv(csv_text)
        if not readings:
            return _error(
                request,
                400,
                "empty_csv",
                "CSV parsed to zero readings — wrong page, or empty range.",
            )

        contracts_in_csv = {r.contract for r in readings if r.contract}
        if len(contracts_in_csv) > 1:
            return _error(
                request,
                400,
                "multiple_contracts",
                (
                    f"CSV contains {len(contracts_in_csv)} contracts "
                    f"({', '.join(sorted(contracts_in_csv))}). The bookmarklet "
                    "must be clicked AFTER selecting one contract in the portal "
                    "dropdown — one POST per contract."
                ),
            )
        if not contracts_in_csv:
            return _error(
                request,
                400,
                "missing_contract",
                "CSV does not include a 'Contrato' column value.",
            )
        posted_contract = next(iter(contracts_in_csv))

        # ------------------------------------------------------------------
        # Critical section: contract-mixing safeguard + first-ingest claim
        # + store write + reload/refresh trigger.
        #
        # Every step inside is read-modify-write against shared state
        # (``config_entry.data``, the on-disk store, the coordinator
        # cache). Two POSTs hitting the same entry within milliseconds
        # — easy to do by double-clicking the bookmarklet, or by the
        # Chrome retry path on a flaky network — would otherwise:
        #
        #   * Both observe ``expected_contract == ""`` and both call
        #     ``async_update_entry`` to claim the contract. The second
        #     call is a no-op (same contract id) but in a future where
        #     the user is on a multi-contract account and the bookmarklet
        #     somehow posts two different contracts back-to-back, the
        #     second would silently overwrite the first.
        #   * Both run ``store.async_replace`` and race the JSON write,
        #     leaving a partially-merged file on disk.
        #   * Both schedule a config-entry reload, leaving the second
        #     reload to happen in the middle of the first one's setup.
        #
        # The per-entry ``ingest_lock`` (created in __init__.py) makes
        # the whole block strictly serial PER ENTRY. Different entries
        # can still ingest in parallel (each has its own lock).
        # ------------------------------------------------------------------
        async with entry_data["ingest_lock"]:
            config_entry = self.hass.config_entries.async_get_entry(entry_id)
            if config_entry is None:
                # Belt-and-braces in case the entry was unloaded
                # between acquiring the lock and now.
                return _error(request, 404, "unknown_entry", "Entry vanished mid-request.")

            expected_contract = (config_entry.data.get(CONF_CONTRACT) or "").strip()
            install_name = config_entry.data.get(CONF_NAME) or "Canal de Isabel II"

            if expected_contract and expected_contract != posted_contract:
                await _notify_contract_mismatch(
                    self.hass,
                    install_name,
                    expected_contract,
                    posted_contract,
                    entry_id,
                )
                return _error(
                    request,
                    409,
                    "contract_mismatch",
                    (
                        f"This integration entry ('{install_name}') is bound to contract "
                        f"{expected_contract}, but the CSV is for contract {posted_contract}. "
                        "If you have more than one contract, add a separate integration "
                        "entry for the other one."
                    ),
                )

            first_ingest = not expected_contract
            if first_ingest:
                # First successful POST — claim the contract on the entry.
                new_data = {**config_entry.data, CONF_CONTRACT: posted_contract}
                self.hass.config_entries.async_update_entry(config_entry, data=new_data)
                _LOGGER.info(
                    "[%s] First ingest — entry now bound to contract %s",
                    entry_id,
                    posted_contract,
                )

            # ------------------------------------------------------------------
            # Meter summary — prefer the pre-parsed dict, fall back to HTML scrape.
            # ------------------------------------------------------------------
            meter_summary = parse_meter_summary_from_dict(payload.get("meter_summary"))
            if meter_summary is None:
                meter_summary = parse_meter_summary_from_html(
                    payload.get("consumption_page_html") or ""
                )

            # ------------------------------------------------------------------
            # Store + push.
            # ------------------------------------------------------------------
            store = entry_data["store"]
            coordinator = entry_data["coordinator"]
            now = datetime.now(UTC)
            new_count = await store.async_replace(readings, meter_summary, ingest_at=now)

            # The first ever POST creates entities (the wizard finished
            # without any) — schedule a reload so ``async_setup_entry``
            # re-runs with data present and materialises the sensors.
            # Subsequent POSTs only need a coordinator refresh.
            if first_ingest:
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(entry_id),
                    name="canal_isabel_ii first_ingest reload",
                )
            else:
                await coordinator.async_request_refresh()

        _LOGGER.info(
            "[%s] Ingest OK — contract=%s total=%d new=%d meter=%s first=%s",
            entry_id,
            posted_contract,
            len(readings),
            new_count,
            "yes" if meter_summary else "no",
            "yes" if first_ingest else "no",
        )
        return _json(
            request,
            200,
            {
                "ok": True,
                "imported": len(readings),
                "new": new_count,
                "contract": posted_contract,
                "installation": install_name,
                "meter_reading_l": (meter_summary.reading_liters if meter_summary else None),
                "ingest_at": now.isoformat(),
            },
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _extract_bearer(request: web.Request) -> str:
    raw = request.headers.get("Authorization", "")
    if not raw.lower().startswith("bearer "):
        return ""
    return raw[7:].strip()


def _json(
    request: web.Request,
    status: int,
    body: dict[str, Any],
) -> web.Response:
    """Return a JSON response. CORS headers are attached by HA's aiohttp_cors
    middleware automatically (we opted in via ``cors_allowed = True``), so we
    don't need to set them here.
    """
    return web.json_response(body, status=status)


def _error(
    request: web.Request,
    status: int,
    code: str,
    detail: str,
) -> web.Response:
    _LOGGER.warning("Ingest %d %s: %s", status, code, detail)
    return _json(
        request,
        status,
        {"ok": False, "code": code, "detail": detail},
    )


async def _notify_contract_mismatch(
    hass: HomeAssistant,
    install_name: str,
    expected: str,
    posted: str,
    entry_id: str,
) -> None:
    """Fire a persistent notification on contract mismatch.

    The endpoint already returned 409 to the bookmarklet (which
    surfaces it as an alert in the user's browser), but a persistent
    notification gives the user a record they can act on later from
    the HA UI itself — important because the alert is dismissed
    instantly and the user may not remember the exact text.
    """
    try:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"Canal de Isabel II — contrato no coincide ({install_name})",
                "message": (
                    f"El bookmarklet envió el contrato **{posted}**, pero esta "
                    f'integración ("{install_name}") está vinculada al contrato '
                    f"**{expected}**.\n\n"
                    "¿Qué hacer?\n"
                    "1. Si querías subir el contrato que ya tenías configurado: "
                    "abre la Oficina Virtual, asegúrate de que el dropdown de "
                    "contrato muestra el correcto y vuelve a pulsar el "
                    "bookmarklet.\n"
                    "2. Si tienes varios contratos en el portal: añade otra "
                    "integración Canal de Isabel II en *Ajustes → Dispositivos y "
                    "servicios → Añadir integración*, configúrala y usa **el "
                    "bookmarklet de esa nueva integración** para subir las "
                    "lecturas del otro contrato."
                ),
                "notification_id": f"canal_contract_mismatch_{entry_id}",
            },
            blocking=False,
        )
    except Exception:
        _LOGGER.exception("Could not raise contract-mismatch notification")
