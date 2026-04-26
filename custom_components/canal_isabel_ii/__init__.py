"""The Canal de Isabel II integration.

The integration receives CSV payloads directly via an HTTP endpoint
exposed under HA itself. A bookmarklet the user installs in their
browser does the portal-side heavy lifting and POSTs the result.

Setup sequence:

1. ``async_setup`` (once per HA boot): register the
   :class:`CanalIngestView` HTTP endpoint (POST CSV from bookmarklet),
   the :class:`CanalBookmarkletPageView` HTTP endpoint (HTML page with
   drag-link + clipboard-copy button so the user can install the
   bookmarklet without copying ~1.5 KB of escaped JavaScript out of a
   Markdown code block), and the services.
2. ``async_setup_entry`` (once per integration entry): restore the
   per-entry ``ReadingStore`` from disk, build the coordinator,
   forward to the sensor platform. On first setup (no contract yet
   bound) we also publish the persistent notification that links to
   the bookmarklet install page.

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

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.recorder import get_instance as get_recorder_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .bookmarklet import (
    build_bookmarklet,
    build_bookmarklet_source,
    collect_alternate_urls,
    format_install_notification,
)
from .bookmarklet_view import CanalBookmarkletPageView
from .const import (
    CONF_COST_STATS_MIGRATED,
    CONF_CUOTA_SUPL_ALC,
    CONF_DIAMETRO_MM,
    CONF_ENABLE_COST,
    CONF_HA_URL,
    CONF_IVA_PCT,
    CONF_N_VIVIENDAS,
    CONF_NAME,
    CONF_TOKEN,
    DEFAULT_CUOTA_SUPL_ALC,
    DEFAULT_DIAMETRO_MM,
    DEFAULT_IVA_PCT,
    DEFAULT_N_VIVIENDAS,
    DOMAIN,
    SIGNAL_CLEAR_COST_STATS,
    SIGNAL_METER_RESET,
    STATISTICS_SOURCE,
)
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
#: Manual recovery service — clears the long-term cost statistics for
#: the entry (both the external ``canal_isabel_ii:cost_<contract>``
#: series and the entity-side ``sensor.<…>_coste_acumulado`` series),
#: then triggers a coordinator refresh. Same code path as the v0.5.4
#: one-shot migration; exposed as a service so users who hit residual
#: stale data (negative bars in the Energy panel because pre-v0.5.4
#: auto-stats survived past the migration window, or any other
#: corruption that slipped through) have a self-service recovery
#: button that doesn't require editing ``.storage`` files.
SERVICE_CLEAR_COST_STATS = "clear_cost_stats"
#: Meter-replacement service — invoked by the user (or an automation)
#: when the physical water counter is swapped by the installer. Drops
#: the per-contract trim baseline in the store and signals the
#: cumulative sensors to clear their in-memory monotonic guard so the
#: next (lower) reading from the new counter is accepted instead of
#: being rejected as a glitch. Long-term recorder statistics are NOT
#: touched — the next push continues forward seamlessly from the
#: existing ``last_sum``, so the user keeps their full historical
#: consumption / cost curves across the swap.
SERVICE_RESET_METER = "reset_meter"
ATTR_INSTANCE = "instance"

REFRESH_SCHEMA = vol.Schema({vol.Optional(ATTR_INSTANCE): cv.string})
SHOW_BOOKMARKLET_SCHEMA = vol.Schema({vol.Optional(ATTR_INSTANCE): cv.string})
CLEAR_COST_STATS_SCHEMA = vol.Schema({vol.Optional(ATTR_INSTANCE): cv.string})
RESET_METER_SCHEMA = vol.Schema({vol.Optional(ATTR_INSTANCE): cv.string})

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


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
    for label, alt_url in collect_alternate_urls(hass, ha_url):
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
    """Register the HTTP views + the manual refresh service exactly once."""
    hass.data.setdefault(DOMAIN, {})
    hass.http.register_view(CanalIngestView(hass))
    # Human-facing install page with drag-link + clipboard-copy button.
    # Linked from the install notification; no auth surface added (uses
    # HA's existing session cookie via ``requires_auth = True``).
    hass.http.register_view(CanalBookmarkletPageView(hass))

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

    if not hass.services.has_service(DOMAIN, SERVICE_CLEAR_COST_STATS):

        async def _clear_cost_stats(call: ServiceCall) -> None:
            wanted = (call.data.get(ATTR_INSTANCE) or "").strip().lower()
            for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
                if not isinstance(entry_data, dict):
                    continue
                store: ReadingStore | None = entry_data.get("store")
                coord: CanalCoordinator | None = entry_data.get("coordinator")
                name = (entry_data.get("name") or "").lower()
                if store is None or coord is None:
                    continue
                if wanted and wanted not in {entry_id.lower(), name}:
                    continue
                config_entry = hass.config_entries.async_get_entry(entry_id)
                if config_entry is None:
                    continue
                _LOGGER.info("[%s] Service clear_cost_stats requested", entry_id)
                await _clear_cost_stats_for_entry(hass, config_entry, store)
                # Re-publish from a clean slate. The cost sensor's
                # ``_handle_coordinator_update`` rebuilds the full
                # monotonic series via the spike-immune push.
                await coord.async_request_refresh()

        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_COST_STATS,
            _clear_cost_stats,
            schema=CLEAR_COST_STATS_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RESET_METER):

        async def _reset_meter(call: ServiceCall) -> None:
            """Tell the integration the physical water meter was replaced.

            For each matched contract under each matched entry:

            * Drop the per-contract trim baseline in the store (the
              trimmed-out liters belonged to the OLD physical counter
              and would otherwise inflate the new cumulative state).
            * Fire ``SIGNAL_METER_RESET`` so the cumulative-consumption,
              cumulative-cost and absolute-meter-reading sensors clear
              their in-memory monotonic guards. Without this, the next
              (now-lower) reading would be rejected and the entities
              would freeze on the pre-swap value.
            * Trigger a coordinator refresh so the new state hits the
              entity card immediately.

            Long-term external statistics (the Energy panel curves) are
            **not** touched: those anchor to the recorder's ``last_sum``
            and the next push continues forward seamlessly. The user
            keeps their full consumption / cost history across the
            counter swap; only the per-meter state on the entity card
            and the now-stale baseline get cleared.
            """
            wanted = (call.data.get(ATTR_INSTANCE) or "").strip().lower()
            matched_any = False
            for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
                if not isinstance(entry_data, dict):
                    continue
                store: ReadingStore | None = entry_data.get("store")
                coord: CanalCoordinator | None = entry_data.get("coordinator")
                name = (entry_data.get("name") or "").lower()
                if store is None or coord is None:
                    continue
                if wanted and wanted not in {entry_id.lower(), name}:
                    continue
                matched_any = True
                contracts = store.contracts
                if not contracts:
                    _LOGGER.info(
                        "[%s] reset_meter: no contracts cached yet — "
                        "nothing to reset; the next bookmarklet POST "
                        "will populate fresh state",
                        entry_id,
                    )
                    continue
                for contract_id in sorted(contracts):
                    await store.async_reset_baseline(contract_id)
                    async_dispatcher_send(
                        hass,
                        SIGNAL_METER_RESET.format(entry_id=entry_id, contract_id=contract_id),
                    )
                    _LOGGER.info(
                        "[%s] reset_meter: cleared baseline + signalled sensors for contract %s",
                        entry_id,
                        contract_id,
                    )
                await coord.async_request_refresh()
            if wanted and not matched_any:
                _LOGGER.warning(
                    "reset_meter: no integration entry matched %r — "
                    "use the entry id or the configured name",
                    wanted,
                )

        hass.services.async_register(
            DOMAIN, SERVICE_RESET_METER, _reset_meter, schema=RESET_METER_SCHEMA
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Per-entry boot: restore store, build coordinator, fan out platforms."""
    store = ReadingStore(hass, entry.entry_id)
    await store.async_load()

    # v0.5.4 one-shot migration. Pre-v0.5.4 entries had cost stats that
    # mixed HA's auto-generated values (from ``state_class=total_increasing``)
    # with the explicit push added in v0.5.2 — leaving non-monotonic
    # series that the Energy panel renders as 0 € for old periods or
    # huge negative bars where the two paths diverged. We clear those
    # stats once so the new spike-immune push (see
    # ``CanalCumulativeCostSensor._submit_running_stats``) can rebuild
    # them from a clean slate. Idempotent via the ``CONF_COST_STATS_MIGRATED``
    # flag — runs at most once per entry.
    if not entry.data.get(CONF_COST_STATS_MIGRATED):
        await _migrate_cost_stats_v054(hass, entry, store)
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_COST_STATS_MIGRATED: True},
        )

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
        # Cost-feature merged settings (data ⊕ options) so sensor.py
        # can read them without re-doing the merge on every refresh.
        # OptionsFlow writes to ``entry.options``; the wizard wrote to
        # ``entry.data``. Options win when both are present.
        "cost": _resolve_cost_settings(entry),
        # Per-entry asyncio.Lock serializing the read-modify-write
        # section of the ingest view. Two POSTs hitting the same
        # entry simultaneously would otherwise both pass the
        # ``expected_contract == ""`` check, both call
        # ``async_update_entry`` to claim the contract, and both run
        # ``store.async_replace`` racing each other's writes. Per-entry
        # (not global) so two entries can ingest in parallel — only
        # POSTs targeting the SAME entry serialize.
        "ingest_lock": asyncio.Lock(),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Listen for option/data updates so a token rotation, rename, or
    # cost-params edit propagates to the cached value above without an
    # HA restart. Cost-params changes also trigger a full entry reload
    # so sensors get torn down / created to match the new state (see
    # ``_async_update_listener``).
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # First-time setup: the entry has no bound contract yet. Publish the
    # install notification with the bookmarklet(s) so the user can copy
    # them without having to hunt for the ``show_bookmarklet`` service.
    # Once the first POST binds a contract this branch stops firing on
    # restart, so HA reboots don't re-spam the notification.
    if not entry.data.get("contract"):
        await _publish_bookmarklet_notification(hass, entry)

    return True


async def _clear_cost_stats_for_entry(
    hass: HomeAssistant, entry: ConfigEntry, store: ReadingStore
) -> None:
    """Clear all long-term cost statistics for this entry.

    Drops two statistic ids per contract:

    1. ``canal_isabel_ii:cost_<contract>`` — external statistic
       written by the spike-immune push in
       ``CanalCumulativeCostSensor._submit_running_stats``.
    2. ``sensor.<…>_coste_acumulado`` — entity-side statistic
       auto-generated by HA's recorder (``state_class=total``).
       Looked up via the entity registry by unique_id so a user
       rename of the entity doesn't break the cleanup.

    Used in two places:

    * ``_migrate_cost_stats_v054`` — one-shot at first boot of v0.5.4,
      gated by ``CONF_COST_STATS_MIGRATED``. Cleans up the
      stuck-on-zero / huge-negative-bar mess left by pre-v0.5.4
      auto-stats colliding with the v0.5.2 explicit push.
    * The new ``clear_cost_stats`` service (v0.5.15) — manual
      recovery for users who report negative cost bars in the Energy
      panel anyway. Same code path; just exposes the eraser button.

    After the recorder applies the clear, the next coordinator tick
    runs the cost sensor's update which pushes a from-scratch
    monotonic series via the spike-immune path. The Energy panel
    backfills automatically once the new stats land.

    ``async_clear_statistics`` is a callback that queues a recorder
    task — fire-and-forget. We don't await an acknowledgement; the
    push that follows the next ``async_request_refresh`` will see the
    cleared series.
    """
    contracts = sorted({r.contract for r in store.readings if r.contract})
    if not contracts:
        # Brand-new entry (no bookmarklet POST yet) or genuinely
        # contract-less — nothing to clear. Caller still flips its
        # idempotency flag so we don't keep re-scanning on every boot.
        return

    stat_ids: list[str] = []

    # External statistic ids — deterministic from the contract id.
    for contract in contracts:
        stat_ids.append(f"{STATISTICS_SOURCE}:cost_{contract}")

    # Entity statistic ids — resolve via the entity registry by the
    # unique_id the cost sensor stamps on itself in
    # ``CanalCumulativeCostSensor.__init__``. If the user renamed the
    # entity, this still finds it; if the entity was never created
    # (cost feature never enabled) we get None and skip cleanly.
    ent_reg = er.async_get(hass)
    for contract in contracts:
        unique_id = f"canal_isabel_ii_{contract}_cumulative_cost"
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id:
            stat_ids.append(entity_id)

    if not stat_ids:
        return

    _LOGGER.info(
        "[%s] Clearing %d cost statistic_ids: %s",
        entry.entry_id,
        len(stat_ids),
        stat_ids,
    )
    recorder = get_recorder_instance(hass)
    recorder.async_clear_statistics(stat_ids)

    # v0.5.22: notify the live cost sensors so they drop their
    # in-memory ``_restored_value``. Without this, v0.5.21's symmetric
    # regression guard would freeze the entity on the pre-clear high
    # forever — the recorder is empty after the wipe but the entity
    # keeps reporting the old value, the guard sees a regression
    # against that stale anchor on every tick, the push is skipped
    # and the Energy panel reads 0 € indefinitely. Firing the signal
    # AFTER ``async_clear_statistics`` (queued on the recorder) is
    # safe: the next coordinator tick will recompute against
    # ``_restored_value=None`` and push from cold-start; the recorder
    # will have applied the clear by then or will apply the new push
    # idempotently — either way the series ends monotonic.
    for contract in contracts:
        async_dispatcher_send(
            hass,
            SIGNAL_CLEAR_COST_STATS.format(
                entry_id=entry.entry_id, contract_id=contract
            ),
        )


async def _migrate_cost_stats_v054(
    hass: HomeAssistant, entry: ConfigEntry, store: ReadingStore
) -> None:
    """One-shot v0.5.4 migration: clear stale cost statistics for this entry.

    ## Why

    ``sensor.<…>_coste_acumulado`` was declared
    ``state_class = TOTAL_INCREASING`` so HA's recorder auto-generated
    long-term statistics for it from the entity's state stream. That
    auto-generation began **the moment the cost feature was first
    enabled**, with whatever ``native_value`` the sensor had at that
    instant — which only reflects what's in ``coordinator.data`` (a
    rolling window capped by ``MAX_READINGS_PER_ENTRY``).

    v0.5.2 added a parallel ``async_import_statistics`` push to the
    same ``statistic_id``. Both writers are upserts by
    ``(statistic_id, start)``, but they only overlap on the hours our
    push covers — every hour outside that window kept the
    auto-generated ``sum``. Result: a series with two regimes glued
    together, frequently non-monotonic at the seam.

    The Energy panel renders each bar as ``sum[n] - sum[n-1]``, so a
    drop at the seam becomes a large negative bar (sometimes hundreds
    of euros), and a series that auto-gen never reached becomes 0 €
    for the entire historical period before the cost-feature toggle.

    ## What we do

    Delegate to :func:`_clear_cost_stats_for_entry`, which drops both
    the external and entity statistic ids for every contract in this
    entry. The next coordinator tick rebuilds the series from a clean
    slate via the spike-immune push.

    ## Idempotency

    Caller sets ``entry.data[CONF_COST_STATS_MIGRATED] = True`` after
    we return, so a restart never re-clears. A user who deliberately
    wants to re-trigger the migration can either flip that flag in
    ``.storage/core.config_entries`` and reboot, OR — preferred since
    v0.5.15 — call the ``canal_isabel_ii.clear_cost_stats`` service,
    which uses the same code path without touching storage files.
    """
    await _clear_cost_stats_for_entry(hass, entry, store)


def _resolve_cost_settings(entry: ConfigEntry) -> dict[str, Any]:
    """Merge cost params from ``entry.data`` (wizard) with ``entry.options``
    (OptionsFlow). Options win when both are present.

    Always returns the full dict (with defaults) regardless of whether
    the cost feature is enabled, so sensors can branch on
    ``settings["enable_cost"]`` and never deal with missing keys.
    """
    merged: dict[str, Any] = {**entry.data, **entry.options}
    return {
        CONF_ENABLE_COST: bool(merged.get(CONF_ENABLE_COST, False)),
        CONF_DIAMETRO_MM: int(merged.get(CONF_DIAMETRO_MM, DEFAULT_DIAMETRO_MM)),
        CONF_N_VIVIENDAS: int(merged.get(CONF_N_VIVIENDAS, DEFAULT_N_VIVIENDAS)),
        CONF_CUOTA_SUPL_ALC: float(merged.get(CONF_CUOTA_SUPL_ALC, DEFAULT_CUOTA_SUPL_ALC)),
        CONF_IVA_PCT: float(merged.get(CONF_IVA_PCT, DEFAULT_IVA_PCT)),
    }


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Refresh cached settings when the entry data/options change.

    Token + name updates are absorbed in place (no reload needed).
    Cost-feature changes trigger a full reload so the sensor platform
    re-runs ``async_setup_entry`` and creates/destroys cost entities
    to match the new state.
    """
    cache = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not cache:
        return
    cache["token"] = entry.data.get("token", "")
    cache["name"] = entry.data.get(CONF_NAME) or entry.title or ""
    new_cost = _resolve_cost_settings(entry)
    if new_cost != cache.get("cost"):
        cache["cost"] = new_cost
        # Cost-feature toggled or params changed — full reload so
        # sensor.py re-evaluates which entities should exist.
        await hass.config_entries.async_reload(entry.entry_id)


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
