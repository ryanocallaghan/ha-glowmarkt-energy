"""Glowmarkt (Bright) smart meter data for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import GlowmarktClient
from .const import DOMAIN
from .coordinator import GlowmarktCoordinator

PLATFORMS = [Platform.SENSOR]

type GlowmarktConfigEntry = ConfigEntry[GlowmarktCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: GlowmarktConfigEntry) -> bool:
    """Set up a Bright account."""
    client = GlowmarktClient(
        async_get_clientsession(hass), entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    coordinator = GlowmarktCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GlowmarktConfigEntry) -> bool:
    """Unload a Bright account."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: GlowmarktConfigEntry) -> None:
    """Forget import progress when the account is removed."""
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
