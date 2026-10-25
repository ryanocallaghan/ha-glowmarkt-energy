"""Config and options flows for the Glowmarkt (Bright) integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import GlowmarktAuthError, GlowmarktClient, GlowmarktError
from .const import (
    CONF_FIRST_RATE,
    CONF_FIRST_RATE_KWH,
    CONF_HISTORY_MONTHS,
    CONF_STANDING_CHARGE,
    CONF_UNIT_RATE,
    DEFAULT_HISTORY_MONTHS,
    DOMAIN,
    FUELS,
)

PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)


def _price(unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0, step=0.001, mode=NumberSelectorMode.BOX, unit_of_measurement=unit
        )
    )


CHARGES_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_HISTORY_MONTHS, default=DEFAULT_HISTORY_MONTHS
        ): NumberSelector(
            NumberSelectorConfig(
                min=0, max=120, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="months"
            )
        ),
    }
    | {
        vol.Required(f"{fuel}_{key}", default=0): _price(unit)
        for fuel in FUELS
        for key, unit in (
            (CONF_UNIT_RATE, "p/kWh"),
            (CONF_STANDING_CHARGE, "p/day"),
            (CONF_FIRST_RATE, "p/kWh"),
            (CONF_FIRST_RATE_KWH, "kWh/day"),
        )
    }
)


async def _validate_login(hass: HomeAssistant, data: Mapping[str, Any]) -> str | None:
    """Return an error key, or None if the login works and has a meter."""
    client = GlowmarktClient(
        async_get_clientsession(hass), data[CONF_USERNAME], data[CONF_PASSWORD]
    )
    try:
        resources = await client.resources()
    except GlowmarktAuthError:
        return "invalid_auth"
    except GlowmarktError:
        return "cannot_connect"
    classifiers = {r.get("classifier") for r in resources}
    if not classifiers & set(FUELS.values()):
        return "no_meters"
    return None


class GlowmarktConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Bright account."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._login: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the Bright login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME].strip().lower())
            self._abort_if_unique_id_configured()
            if error := await _validate_login(self.hass, user_input):
                errors["base"] = error
            else:
                self._login = user_input
                return await self.async_step_charges()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(LOGIN_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_charges(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for unit rates and standing charges."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._login[CONF_USERNAME], data=self._login, options=user_input
            )
        return self.async_show_form(step_id="charges", data_schema=CHARGES_SCHEMA)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a rejected password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            if error := await _validate_login(self.hass, data):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GlowmarktOptionsFlow:
        """Return the options flow."""
        return GlowmarktOptionsFlow()


class GlowmarktOptionsFlow(OptionsFlowWithReload):
    """Change unit rates and standing charges."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the charges form."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                CHARGES_SCHEMA, self.config_entry.options
            ),
        )
