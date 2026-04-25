"""Constants for the Canal de Isabel II integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "canal_isabel_ii"

# ---------------------------------------------------------------------
# Config-entry keys
# ---------------------------------------------------------------------

#: Free-text label the user picks during config flow ("Casa principal").
#: Used as the device name and entity prefix so the dashboard shows
#: something readable instead of the long contract id. The unique_id
#: stays tied to the contract id so renaming never breaks history.
CONF_NAME = "name"

#: 32-char hex token generated at config-flow time and required on
#: every POST to the ingest endpoint. Stored in entry.data; the
#: bookmarklet bakes it into the ``Authorization: Bearer …`` header.
CONF_TOKEN = "token"

#: The contract id this entry is bound to. Empty until the first
#: successful POST through the bookmarklet, then auto-set from the
#: CSV's ``Contrato`` column. Subsequent POSTs whose contract id
#: doesn't match are rejected with HTTP 409 + a persistent
#: notification — see ``ingest.py`` for the contract-mixing safeguard
#: rationale.
CONF_CONTRACT = "contract"

#: Optional override for the HA external URL the bookmarklet POSTs to.
#: Defaults to ``hass.config.external_url`` (typical setup with
#: DuckDNS / Nabu Casa) but the user can paste a different value if
#: they front HA with a reverse proxy on a custom domain.
CONF_HA_URL = "ha_url"

# ---------------------------------------------------------------------
# Cost feature (opt-in via config flow)
# ---------------------------------------------------------------------

#: Whether this entry should compute and publish cost-derived entities
#: (cumulative cost, current price, current block) on top of the
#: consumption ones. Defaults to False — users who only care about m³
#: pay zero overhead. Toggleable later via the options flow.
CONF_ENABLE_COST = "enable_cost"

#: Caliber of the meter in millimetres. Drives the cuota-fija formula
#: (``D²`` term in aducción / distribución). Doméstico contracts in
#: Madrid usually have a 13 or 15 mm meter; bigger calibres exist for
#: communal installations or large unifamiliar houses.
CONF_DIAMETRO_MM = "diametro_mm"

#: Number of dwellings served by this contract. ``N`` in the cuota-fija
#: formulas. Almost always 1 for a single-family installation; communal
#: contracts (a vertical of flats sharing one contador) plug in the
#: actual number.
CONF_N_VIVIENDAS = "n_viviendas"

#: Cuota suplementaria de alcantarillado in €/m³. Each municipio in
#: the Comunidad de Madrid sets its own rate, so it differs per
#: contract and sometimes between vigencias. Defaults to 0.0 so a
#: user who hasn't looked at their bill yet still gets a reasonable
#: lower-bound cost estimate; the real value is on the bill labelled
#: "Cuota suplementaria de alcantarillado".
CONF_CUOTA_SUPL_ALC = "cuota_supl_alc_eur_m3"

#: IVA percentage applied to the entire bill. 10 % nationally for
#: water in Spain — exposed as a config so a user who has a different
#: legal regime (or who wants to switch to the future 21 % rate if
#: water ever loses its reduced-VAT status) can update without an
#: integration release.
CONF_IVA_PCT = "iva_pct"

#: One-shot migration flag set on first boot under v0.5.4. When False
#: (or missing — pre-v0.5.4 entries don't have it), :func:`async_setup_entry`
#: clears any previously-stored cost statistics so the new spike-immune
#: push path can rebuild them from a clean, monotonic series. See the
#: long-form rationale in ``__init__.py:_migrate_cost_stats_v054``.
CONF_COST_STATS_MIGRATED = "cost_stats_v054_migrated"


# ---------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------

DEFAULT_NAME = "Canal de Isabel II"

#: Sensible Doméstico 1-vivienda defaults — cover the "I checked the
#: enable_cost box but haven't read my bill yet" path with a
#: configuration that produces a plausible ballpark cost.
#:
#: ``DEFAULT_CUOTA_SUPL_ALC`` corresponds to a value in the typical
#: range of the supplementary sewer fees billed across municipalities
#: served by Canal de Isabel II (most of them between ``0.05`` and
#: ``0.15 €/m³`` for the 2026 vigencia). Picking a non-zero default
#: yields a more realistic ballpark in the Energy panel for the user
#: who hasn't fetched their bill yet — they can refine via the Options
#: flow once they do.
DEFAULT_DIAMETRO_MM = 15
DEFAULT_N_VIVIENDAS = 1
DEFAULT_CUOTA_SUPL_ALC = 0.1002
DEFAULT_IVA_PCT = 10.0

#: Coordinator tick interval. The ingest endpoint pokes the
#: coordinator on every POST so live data arrives instantly; this slow
#: tick exists ONLY to refresh the time-derived attribute aggregates
#: (``consumption_today_l`` flips at midnight, ``data_age_minutes``
#: keeps growing while no one POSTs, etc.).
UPDATE_INTERVAL = timedelta(hours=1)

#: Source identifier used for the long-term external statistics
#: (``canal_isabel_ii:consumption_<contract>``). Matches the domain so
#: HA's recorder associates the series with this integration.
STATISTICS_SOURCE = DOMAIN

# ---------------------------------------------------------------------
# Ingest endpoint
# ---------------------------------------------------------------------

#: URL prefix served by the integration's ``HomeAssistantView``. The
#: full URL is ``<ha-external-url>/api/canal_isabel_ii/ingest/<entry_id>``.
#: Entry id in the path keeps the routing trivial; the actual
#: authentication is the Bearer token in the header.
INGEST_URL_PREFIX = "/api/canal_isabel_ii/ingest"

#: URL prefix for the human-facing bookmarklet install page (HTML with a
#: drag-to-bookmarks-bar link + a "copy to clipboard" button). Authenticated
#: by the per-entry token in a ``?t=<token>`` query param (``requires_auth=False``
#: on the view), because a plain browser navigation from a markdown link in a
#: persistent notification does NOT carry the ``Authorization: Bearer`` header
#: that HA's session-cookie auth requires. Putting the same token in the URL
#: doesn't widen the attack surface — the bookmarklet body that this page
#: exposes already embeds the same token in plain JS.
BOOKMARKLET_PAGE_URL_PREFIX = "/api/canal_isabel_ii/bookmarklet"

#: Maximum POST body size accepted by the ingest endpoint. The
#: portal CSV for one full year of one contract is < 200 KB; we set a
#: 4 MB ceiling so a malformed bookmarklet that ships an entire
#: portal HTML page (~1 MB) still goes through, while obvious
#: garbage (a flood of bytes) is rejected without parsing.
MAX_INGEST_BYTES = 4 * 1024 * 1024

# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------

#: Storage key prefix for the per-entry ``ReadingStore`` JSON file
#: under ``<config>/.storage/``. Suffixed with the entry id so each
#: integration entry gets its own file: ``canal_isabel_ii.<entry_id>``.
STORAGE_KEY_PREFIX = DOMAIN
STORAGE_VERSION = 1

#: Hard cap on how many hourly readings we keep per entry. Roughly
#: matches the portal's ~7-month retention (24 * 31 * 7 = 5208) with
#: enough headroom for the occasional gap-fill backfill.
MAX_READINGS_PER_ENTRY = 8760
