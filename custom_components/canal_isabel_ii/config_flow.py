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

Re-auth (token rotation) is not modelled — if the user needs a new
token, they delete and recreate the entry.
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


def _cost_schema(
    *,
    diametro: int = DEFAULT_DIAMETRO_MM,
    viviendas: int = DEFAULT_N_VIVIENDAS,
    suplementaria: float = DEFAULT_CUOTA_SUPL_ALC,
    iva: float = DEFAULT_IVA_PCT,
) -> vol.Schema:
    """Build the cost-params voluptuous schema with given defaults.

    Factored out so the ConfigFlow's "cost" step and the OptionsFlow
    share one definition — both render the same fields, only the
    pre-filled defaults differ (defaults vs already-configured
    values).
    """
    return vol.Schema(
        {
            vol.Required(CONF_DIAMETRO_MM, default=diametro): vol.All(
                vol.Coerce(int), vol.Range(min=10, max=200)
            ),
            vol.Required(CONF_N_VIVIENDAS, default=viviendas): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=999)
            ),
            vol.Required(CONF_CUOTA_SUPL_ALC, default=suplementaria): vol.All(
                vol.Coerce(float), vol.Range(min=0.0, max=10.0)
            ),
            vol.Required(CONF_IVA_PCT, default=iva): vol.All(
                vol.Coerce(float), vol.Range(min=0.0, max=100.0)
            ),
        }
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
    """Edit cost parameters (and toggle the cost feature) post-install.

    Stored as ``entry.options`` (HA convention for editable settings).
    On save, ``async_setup_entry`` re-runs via the update listener
    wired in ``__init__.py``; the new values flow through to the
    sensors on that reload.

    Reads from ``entry.options`` first (last-saved values) and falls
    back to ``entry.data`` (initial wizard values) so the form
    pre-populates with whatever the user last saw.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Single step — show the same fields as the wizard's cost step
        plus the enable_cost toggle so the user can disable cost entirely."""
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
                vol.Required(
                    CONF_DIAMETRO_MM,
                    default=int(merged.get(CONF_DIAMETRO_MM, DEFAULT_DIAMETRO_MM)),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=200)),
                vol.Required(
                    CONF_N_VIVIENDAS,
                    default=int(merged.get(CONF_N_VIVIENDAS, DEFAULT_N_VIVIENDAS)),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=999)),
                vol.Required(
                    CONF_CUOTA_SUPL_ALC,
                    default=float(merged.get(CONF_CUOTA_SUPL_ALC, DEFAULT_CUOTA_SUPL_ALC)),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=10.0)),
                vol.Required(
                    CONF_IVA_PCT,
                    default=float(merged.get(CONF_IVA_PCT, DEFAULT_IVA_PCT)),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
