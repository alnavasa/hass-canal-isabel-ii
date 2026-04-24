"""Config flow for Canal de Isabel II.

The wizard asks for two things:

1. An installation name (``name``) — free text, used as device label
   and sensor prefix. The contract number is still the internal
   ``unique_id``; the label is cosmetic.
2. The HTTPS URL of this Home Assistant (``ha_url``) where the
   bookmarklet will POST the CSV payload. Defaults to
   ``hass.config.external_url`` or ``internal_url`` so the common
   case (DuckDNS / Nabu Casa / a LAN-only HTTPS setup) needs no
   input.

On submit we generate a 192-bit token (``secrets.token_hex(24)``)
for this entry and return ``async_create_entry``. The flow manager
allocates the real ``entry_id`` after the step returns — **so the
bookmarklet and its install notification cannot be built here**;
they are published from ``async_setup_entry`` (see ``__init__.py``),
which runs with the final ``entry.entry_id`` bound.

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
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_HA_URL,
    CONF_NAME,
    CONF_TOKEN,
    DEFAULT_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._name: str = DEFAULT_NAME
        self._ha_url: str = ""
        self._token: str = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1 — ask for the installation name + optional URL override."""
        errors: dict[str, str] = {}
        default_url = (self.hass.config.external_url or "").rstrip("/") or (
            self.hass.config.internal_url or ""
        ).rstrip("/")

        if user_input is not None:
            self._name = (user_input.get(CONF_NAME) or DEFAULT_NAME).strip() or DEFAULT_NAME
            self._ha_url = (user_input.get(CONF_HA_URL) or default_url or "").strip().rstrip("/")
            if not self._ha_url:
                errors["base"] = "missing_ha_url"
            elif not (self._ha_url.startswith("http://") or self._ha_url.startswith("https://")):
                errors[CONF_HA_URL] = "invalid_ha_url"
            else:
                self._token = secrets.token_hex(24)  # 48 chars, 192 bits
                return await self._create_entry()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self._name): str,
                vol.Required(CONF_HA_URL, default=default_url): str,
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

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> FlowResult:
        """No live session to re-auth — point the user at delete/recreate."""
        return self.async_abort(reason="reauth_not_supported")

    async def _create_entry(self) -> FlowResult:
        data: dict[str, Any] = {
            CONF_NAME: self._name,
            CONF_TOKEN: self._token,
            CONF_HA_URL: self._ha_url,
            # Empty until the first successful POST sets it (see ingest.py).
            "contract": "",
        }
        # The bookmarklet + its persistent notification are published
        # from ``async_setup_entry`` once we have a real ``entry_id``.
        # Building them here would bake ``<pending>`` into the
        # ``javascript:…`` URL (the flow manager only populates the
        # ``result`` field after this method returns).
        return self.async_create_entry(title=self._name, data=data)
