"""Config flow for Canal de Isabel II.

Two-step wizard:

1. **User step** — installation name (``name``), HTTPS URL of HA
   (``ha_url``), and a checkbox ``enable_cost`` that toggles the
   opt-in cost-tracking feature. The first two fields drive the
   ingest path (where the bookmarklet POSTs); the third decides
   whether step 2 runs.

2. **Cost step** — only rendered when ``enable_cost`` was ticked.
   Asks for the four parameters needed to reproduce the bill:
   meter diameter (mm), number of dwellings, cuota suplementaria de
   alcantarillado (€/m³), and the IVA percentage. Defaults are the
   most common Doméstico-1-vivienda values so a user who isn't sure
   can leave everything as-is and still get a plausible cost
   estimate.

The cost feature is **strictly opt-in**: leaving the checkbox
unticked skips the second step entirely and produces an entry that
behaves exactly like v0.4.x — no cost entities, no extra runtime
cost, no surprise bills shown next to consumption.

After install, the same parameters are re-editable via an
``OptionsFlow`` so the user can correct the diameter / fix a typo in
the suplementaria / toggle the cost feature on or off without
deleting the integration.

On submit of the user step we generate a 192-bit token
(``secrets.token_hex(24)``) for this entry. The flow manager
allocates the real ``entry_id`` after ``async_create_entry`` returns
— **so the bookmarklet and its install notification cannot be built
here**; they are published from ``async_setup_entry`` (see
``__init__.py``), which runs with the final ``entry.entry_id`` bound.

The entry starts with no bound contract id. The first successful
POST via the bookmarklet binds it (see ``ingest.py``) and triggers an
entity reload so the sensors materialise without an HA restart.

Token rotation lives in the OptionsFlow as of v0.5.18. The user
opens *Configurar* on the integration card, picks "Rotar token" from
the menu, and confirms; we generate a new ``secrets.token_hex(24)``,
write it to ``entry.data[CONF_TOKEN]`` and re-publish the install
notification with the bookmarklet rebuilt around the new token. The
old bookmarklet (and any URL with the old ``?t=…`` query) stops
working immediately, which is the whole point.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
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
    DEFAULT_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


# Realistic ranges for residential / small-community Canal de Isabel II
# contracts. Bounds chosen to keep the input typeable (NumberSelector
# in BOX mode renders as input box rather than slider) and to reject
# obviously-wrong values (a 200 mm contador would be industrial, a
# 100 % IVA would be a typo).
#
# Cuota suplementaria: real municipal values are well under 1 €/m³,
# but we leave plenty of headroom (5 €/m³) so users can't trip on a
# single-digit typo (e.g. ``1,2345`` when they meant ``0,1234``)
# without HA throwing a useless "Value too large" error. The wrong
# input still produces a wrong cost, but at least the form accepts it
# and the user catches it the moment they compare to a real bill.
_DIAMETRO_MIN_MM, _DIAMETRO_MAX_MM = 10, 50
_VIVIENDAS_MIN, _VIVIENDAS_MAX = 1, 200
_CUOTA_MIN, _CUOTA_MAX = 0.0, 5.0
_IVA_MIN, _IVA_MAX = 0.0, 25.0


def _cost_fields(
    *,
    diametro: int = DEFAULT_DIAMETRO_MM,
    viviendas: int = DEFAULT_N_VIVIENDAS,
    suplementaria: float = DEFAULT_CUOTA_SUPL_ALC,
    iva: float = DEFAULT_IVA_PCT,
) -> dict:
    """Build the cost-params field dict with given defaults.

    Returns a dict (not a vol.Schema) so callers can compose it with
    additional fields — e.g. the OptionsFlow prepends ``enable_cost``
    so the user can toggle the feature off without leaving the form.

    All four fields use ``NumberSelector`` with ``mode=BOX`` so they
    render as typeable input boxes (with up/down spinners) instead of
    sliders. Without this, HA's auto-detection picks a slider widget
    for the diameter field because of its int+wide-range schema, which
    is awkward for a value the user just wants to type from their bill.
    """
    return {
        vol.Required(CONF_DIAMETRO_MM, default=diametro): NumberSelector(
            NumberSelectorConfig(
                min=_DIAMETRO_MIN_MM,
                max=_DIAMETRO_MAX_MM,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_N_VIVIENDAS, default=viviendas): NumberSelector(
            NumberSelectorConfig(
                min=_VIVIENDAS_MIN,
                max=_VIVIENDAS_MAX,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_CUOTA_SUPL_ALC, default=suplementaria): NumberSelector(
            # ``step="any"`` removes step coercion in the UI: HA's
            # ``NumberSelectorConfig`` schema rejects any numeric
            # ``step`` below ``1e-3`` (see core ``selector.py``), and
            # cuota suplementaria values from real bills have up to 4
            # decimals (e.g. ``0.1234 €/m³``). Using ``"any"`` lets
            # the user type the exact value from the bill without
            # forcing a 0.001 grid.
            NumberSelectorConfig(
                min=_CUOTA_MIN,
                max=_CUOTA_MAX,
                step="any",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_IVA_PCT, default=iva): NumberSelector(
            NumberSelectorConfig(
                min=_IVA_MIN,
                max=_IVA_MAX,
                step=0.5,
                mode=NumberSelectorMode.BOX,
            )
        ),
    }


def _cost_schema(
    *,
    diametro: int = DEFAULT_DIAMETRO_MM,
    viviendas: int = DEFAULT_N_VIVIENDAS,
    suplementaria: float = DEFAULT_CUOTA_SUPL_ALC,
    iva: float = DEFAULT_IVA_PCT,
) -> vol.Schema:
    """Wrap ``_cost_fields`` in a ``vol.Schema`` for the ConfigFlow step."""
    return vol.Schema(
        _cost_fields(
            diametro=diametro,
            viviendas=viviendas,
            suplementaria=suplementaria,
            iva=iva,
        )
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._name: str = DEFAULT_NAME
        self._ha_url: str = ""
        self._token: str = ""
        self._enable_cost: bool = False
        self._cost_params: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1 — installation name, HA URL, opt-in cost-feature toggle."""
        errors: dict[str, str] = {}
        default_url = (self.hass.config.external_url or "").rstrip("/") or (
            self.hass.config.internal_url or ""
        ).rstrip("/")

        if user_input is not None:
            self._name = (user_input.get(CONF_NAME) or DEFAULT_NAME).strip() or DEFAULT_NAME
            self._ha_url = (user_input.get(CONF_HA_URL) or default_url or "").strip().rstrip("/")
            self._enable_cost = bool(user_input.get(CONF_ENABLE_COST, False))
            if not self._ha_url:
                errors["base"] = "missing_ha_url"
            elif not (self._ha_url.startswith("http://") or self._ha_url.startswith("https://")):
                errors[CONF_HA_URL] = "invalid_ha_url"
            else:
                self._token = secrets.token_hex(24)  # 48 chars, 192 bits
                if self._enable_cost:
                    return await self.async_step_cost()
                return await self._create_entry()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self._name): str,
                vol.Required(CONF_HA_URL, default=default_url): str,
                vol.Required(CONF_ENABLE_COST, default=self._enable_cost): bool,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "default_url": default_url or "(no detectada — pega la URL HTTPS de tu HA)",
            },
        )

    async def async_step_cost(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 2 — cost parameters (only when enable_cost was ticked)."""
        if user_input is not None:
            self._cost_params = {
                CONF_DIAMETRO_MM: int(user_input[CONF_DIAMETRO_MM]),
                CONF_N_VIVIENDAS: int(user_input[CONF_N_VIVIENDAS]),
                CONF_CUOTA_SUPL_ALC: float(user_input[CONF_CUOTA_SUPL_ALC]),
                CONF_IVA_PCT: float(user_input[CONF_IVA_PCT]),
            }
            return await self._create_entry()

        return self.async_show_form(
            step_id="cost",
            data_schema=_cost_schema(),
        )

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> FlowResult:
        """No live session to re-auth — point the user at delete/recreate."""
        return self.async_abort(reason="reauth_not_supported")

    async def _create_entry(self) -> FlowResult:
        data: dict[str, Any] = {
            CONF_NAME: self._name,
            CONF_TOKEN: self._token,
            CONF_HA_URL: self._ha_url,
            CONF_ENABLE_COST: self._enable_cost,
            # Empty until the first successful POST sets it (see ingest.py).
            "contract": "",
        }
        if self._enable_cost:
            data.update(self._cost_params)
        # The bookmarklet + its persistent notification are published
        # from ``async_setup_entry`` once we have a real ``entry_id``.
        # Building them here would bake ``<pending>`` into the
        # ``javascript:…`` URL (the flow manager only populates the
        # ``result`` field after this method returns).
        return self.async_create_entry(title=self._name, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> CanalOptionsFlow:
        return CanalOptionsFlow(config_entry)


class CanalOptionsFlow(config_entries.OptionsFlow):
    """Post-install settings: cost params + token rotation.

    Top-level menu (``init`` step) presents two branches:

    * ``cost_params`` — original v0.5.x form. Stored as
      ``entry.options`` (HA convention for editable settings). On
      save the update listener in ``__init__.py`` reloads the entry
      so the sensor platform re-evaluates which entities should
      exist based on the new ``enable_cost`` value.
    * ``rotate_token`` (v0.5.18+) — confirm-and-go step that
      regenerates the 192-bit token in ``entry.data`` and
      re-publishes the install notification with the bookmarklet
      rebuilt around the new token. The previous bookmarklet stops
      working the moment the token in ``entry.data`` flips, so the
      user MUST install the new bookmarklet before clicking again.

    The cost form reads from ``entry.options`` first (last-saved
    values) and falls back to ``entry.data`` (initial wizard values)
    so the inputs pre-populate with whatever the user last saw.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Top-level menu — branch to cost editing or token rotation."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["cost_params", "rotate_token"],
        )

    async def async_step_cost_params(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Edit cost-feature toggle + the four tariff parameters.

        Same fields as the wizard's cost step, plus the
        ``enable_cost`` checkbox so a user can switch cost entities
        off without deleting the integration.
        """
        merged = {**self._entry.data, **self._entry.options}

        if user_input is not None:
            new_options: dict[str, Any] = {
                CONF_ENABLE_COST: bool(user_input.get(CONF_ENABLE_COST, False)),
            }
            if new_options[CONF_ENABLE_COST]:
                new_options.update(
                    {
                        CONF_DIAMETRO_MM: int(user_input[CONF_DIAMETRO_MM]),
                        CONF_N_VIVIENDAS: int(user_input[CONF_N_VIVIENDAS]),
                        CONF_CUOTA_SUPL_ALC: float(user_input[CONF_CUOTA_SUPL_ALC]),
                        CONF_IVA_PCT: float(user_input[CONF_IVA_PCT]),
                    }
                )
            return self.async_create_entry(title="", data=new_options)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ENABLE_COST,
                    default=bool(merged.get(CONF_ENABLE_COST, False)),
                ): bool,
                **_cost_fields(
                    diametro=int(merged.get(CONF_DIAMETRO_MM, DEFAULT_DIAMETRO_MM)),
                    viviendas=int(merged.get(CONF_N_VIVIENDAS, DEFAULT_N_VIVIENDAS)),
                    suplementaria=float(merged.get(CONF_CUOTA_SUPL_ALC, DEFAULT_CUOTA_SUPL_ALC)),
                    iva=float(merged.get(CONF_IVA_PCT, DEFAULT_IVA_PCT)),
                ),
            }
        )
        return self.async_show_form(step_id="cost_params", data_schema=schema)

    async def async_step_rotate_token(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Rotate the per-entry 192-bit token.

        Two-pass form:

        1. First call (``user_input is None``): show an empty form
           with a long descriptive blurb explaining what's about to
           happen and what the user must do afterwards (re-install
           the bookmarklet). The form has no inputs — clicking
           Submit is the consent.
        2. Second call (``user_input is not None``): generate
           ``secrets.token_hex(24)``, persist into ``entry.data``,
           and re-publish the bookmarklet install notification so
           the user sees the new bookmarklet code immediately.

        Note: the old bookmarklet stops working the moment we update
        ``entry.data[CONF_TOKEN]``. The ingest view checks tokens by
        ``hmac.compare_digest`` against the live entry data on every
        request, so there's no cache-invalidation gap.

        We persist via ``async_update_entry`` (writes to
        ``entry.data``) rather than ``async_create_entry`` (which
        only writes to ``entry.options``). ``entry.options`` is for
        editable settings; the token is operational state — it
        belongs in ``entry.data`` next to the other ingest config.

        After rotating we return ``async_create_entry(title="",
        data=...)`` with the existing options unchanged so the
        OptionsFlow closes cleanly. The update listener in
        ``__init__.py`` picks up the token change via
        ``cache["token"] = entry.data.get("token", "")`` without
        needing an entry reload (sensors don't depend on the token).
        """
        if user_input is not None:
            new_token = secrets.token_hex(24)  # 48 chars, 192 bits
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={**self._entry.data, CONF_TOKEN: new_token},
            )
            # Re-publish the bookmarklet so the user can copy the
            # one with the new token. Lazy import to dodge the
            # circular dep: ``__init__.py`` imports from
            # ``config_flow.py`` at startup, so we cannot put this
            # import at the top of this file.
            from . import _publish_bookmarklet_notification

            await _publish_bookmarklet_notification(self.hass, self._entry)
            _LOGGER.info(
                "[%s] Token rotated; new bookmarklet notification published",
                self._entry.entry_id,
            )
            # Return without touching options (token is in data, not
            # options — but the OptionsFlow still needs an
            # ``async_create_entry`` to close).
            return self.async_create_entry(title="", data=dict(self._entry.options))

        # First pass — empty schema, just a Submit button. The long
        # explanation lives in strings.json under
        # options.step.rotate_token.description.
        return self.async_show_form(
            step_id="rotate_token",
            data_schema=vol.Schema({}),
        )
