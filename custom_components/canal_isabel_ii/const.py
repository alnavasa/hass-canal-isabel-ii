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
# Defaults
# ---------------------------------------------------------------------

DEFAULT_NAME = "Canal de Isabel II"

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
#: with HA's normal session cookie; the user reaches it from a link in the
#: install notification.
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
