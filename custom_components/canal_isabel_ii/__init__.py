"""The Canal de Isabel II integration.

The integration receives CSV payloads directly via an HTTP endpoint
exposed under HA itself. A bookmarklet the user installs in their
browser does the portal-side heavy lifting and POSTs the result.

Setup sequence:

1. ``async_setup`` (once per HA boot): register the
   :class:`CanalIngestView` HTTP endpoint and the services.
2. ``async_setup_entry`` (once per integration entry): restore the
   per-entry ``ReadingStore`` from disk, build the coordinator,
   forward to the sensor platform. On first setup (no contract yet
   bound) we also publish the persistent notification that carries
   the generated bookmarklet(s).

Wizard, in plain words:

* User picks a label ("Casa principal") in the config flow.
* HA generates a one-time token and stores the entry.
* ``async_setup_entry`` runs with the final ``entry_id`` and
  publishes a persistent notification with the bookmarklet
  (minified + readable source) ready to copy into the browser.
* User pastes the bookmarklet into Safari/Chrome bookmarks.
* User opens the Oficina Virtual, logs in, clicks the bookmarklet.
* The bookmarklet downloads the CSV using their session cookies
  and POSTs to ``/api/canal_isabel_ii/ingest/<entry_id>``.
* The integration validates, parses, persists, and reloads itself
  the very first time (so sensors materialise).
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .bookmarklet import (
    build_bookmarklet,
    build_bookmarklet_source,
    format_install_notification,
)
from .const import CONF_HA_URL, CONF_NAME, CONF_TOKEN, DOMAIN
from .coordinator import CanalCoordinator
from .ingest import CanalIngestView
from .store import ReadingStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

#: Manual refresh service — kept as a no-op convenience that just
#: kicks the coordinator to re-publish whatever the store has. Useful
#: when the user wants to force an attribute recompute without
#: waiting for the hourly tick (e.g. after manually purging the
#: storage file). It does NOT call out to anything — without a
#: bookmarklet click we have no way to fetch fresh data.
SERVICE_REFRESH = "refresh"
SERVICE_SHOW_BOOKMARKLET = "show_bookmarklet"
ATTR_INSTANCE = "instance"

REFRESH_SCHEMA = vol.Schema({vol.Optional(ATTR_INSTANCE): cv.string})
SHOW_BOOKMARKLET_SCHEMA = vol.Schema({vol.Optional(ATTR_INSTANCE): cv.string})

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _collect_alternate_urls(hass: HomeAssistant, primary_url: str) -> list[tuple[str, str]]:
    """Return `[(label, url), ...]` for HA-configured URLs that differ from
    the primary one baked into this entry's bookmarklet.

    Looks at ``hass.config.internal_url`` and ``hass.config.external_url``;
    labels them as LAN / externo based on which slot they live in. If a
    value equals the primary (after trailing-slash normalisation) or is
    empty, it's skipped.
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


async def _publish_bookmarklet_notification(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Build the primary bookmarklet + any alternates and publish the
    install notification for this entry.

    Idempotent via ``notification_id = canal_bookmarklet_<entry_id>`` —
    re-posting replaces the previous notification for the same entry.
    """
    ha_url = entry.data.get(CONF_HA_URL) or ""
    token = entry.data.get(CONF_TOKEN) or ""
    install = entry.data.get(CONF_NAME) or "Canal de Isabel II"

    primary_bm = build_bookmarklet(
        ha_url=ha_url,
        entry_id=entry.entry_id,
        token=token,
        installation_name=install,
    )
    primary_src = build_bookmarklet_source(
        ha_url=ha_url,
        entry_id=entry.entry_id,
        token=token,
        installation_name=install,
    )

    alternates: list[tuple[str, str, str]] = []
    for label, alt_url in _collect_alternate_urls(hass, ha_url):
        alt_bm = build_bookmarklet(
            ha_url=alt_url,
            entry_id=entry.entry_id,
            token=token,
            installation_name=install,
        )
        alternates.append((label, alt_url, alt_bm))

    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": f"Canal de Isabel II — bookmarklet ({install})",
            "message": format_install_notification(
                install=install,
                bookmarklet=primary_bm,
                ha_url=ha_url,
                entry_id=entry.entry_id,
                token=token,
                source=primary_src,
                alternates=alternates or None,
            ),
            "notification_id": f"canal_bookmarklet_{entry.entry_id}",
        },
        blocking=False,
    )


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register the HTTP view + the manual refresh service exactly once."""
    hass.data.setdefault(DOMAIN, {})
    hass.http.register_view(CanalIngestView(hass))

    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH):

        async def _refresh(call: ServiceCall) -> None:
            wanted = (call.data.get(ATTR_INSTANCE) or "").strip().lower()
            for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
                if not isinstance(entry_data, dict):
                    continue
                coord: CanalCoordinator | None = entry_data.get("coordinator")
                name = (entry_data.get("name") or "").lower()
                if coord is None:
                    continue
                if wanted and wanted not in {entry_id.lower(), name}:
                    continue
                _LOGGER.info("[%s] Service refresh requested", entry_id)
                await coord.async_request_refresh()

        hass.services.async_register(DOMAIN, SERVICE_REFRESH, _refresh, schema=REFRESH_SCHEMA)

    if not hass.services.has_service(DOMAIN, SERVICE_SHOW_BOOKMARKLET):

        async def _show_bookmarklet(call: ServiceCall) -> None:
            wanted = (call.data.get(ATTR_INSTANCE) or "").strip().lower()
            for config_entry in hass.config_entries.async_entries(DOMAIN):
                name = (config_entry.data.get(CONF_NAME) or "").lower()
                if wanted and wanted not in {config_entry.entry_id.lower(), name}:
                    continue
                await _publish_bookmarklet_notification(hass, config_entry)

        hass.services.async_register(
            DOMAIN, SERVICE_SHOW_BOOKMARKLET, _show_bookmarklet, schema=SHOW_BOOKMARKLET_SCHEMA
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Per-entry boot: restore store, build coordinator, fan out platforms."""
    store = ReadingStore(hass, entry.entry_id)
    await store.async_load()

    coordinator = CanalCoordinator(hass, entry, store)
    # First refresh just publishes what's already in the store; never
    # raises. Skipping ``async_config_entry_first_refresh`` because we
    # don't want a hard failure on a brand-new (empty) entry — it's a
    # legitimate state until the user clicks the bookmarklet.
    await coordinator.async_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "store": store,
        "coordinator": coordinator,
        "name": entry.data.get(CONF_NAME) or entry.title or "",
        # Token consulted by ``CanalIngestView`` on every POST. Cached
        # here so the view doesn't have to scan ``hass.config_entries``
        # on every request.
        "token": entry.data.get("token", ""),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Listen for option/data updates so a token rotation or rename
    # propagates to the cached value above without an HA restart.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # First-time setup: the entry has no bound contract yet. Publish the
    # install notification with the bookmarklet(s) so the user can copy
    # them without having to hunt for the ``show_bookmarklet`` service.
    # Once the first POST binds a contract this branch stops firing on
    # restart, so HA reboots don't re-spam the notification.
    if not entry.data.get("contract"):
        await _publish_bookmarklet_notification(hass, entry)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Refresh the cached token + name when the entry data changes."""
    cache = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not cache:
        return
    cache["token"] = entry.data.get("token", "")
    cache["name"] = entry.data.get(CONF_NAME) or entry.title or ""


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down the per-entry state. View + service stay registered (they're hass-wide)."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Wipe the persisted readings file when the entry is deleted."""
    store = ReadingStore(hass, entry.entry_id)
    try:
        await store.async_clear()
    except Exception:
        _LOGGER.exception("[%s] Failed to clear store on entry removal", entry.entry_id)
