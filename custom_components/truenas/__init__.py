"""The TrueNAS integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryError

from .const import PLATFORMS
from .coordinator import TrueNASCoordinator

type TrueNASConfigEntry = ConfigEntry[TrueNASCoordinator]


# ---------------------------
#   async_setup_entry
# ---------------------------
async def async_setup_entry(
    hass: HomeAssistant, config_entry: TrueNASConfigEntry
) -> bool:
    """Set up TrueNAS config entry."""
    try:
        coordinator = TrueNASCoordinator(hass, config_entry)
    except ValueError as err:
        raise ConfigEntryError(f"Invalid TrueNAS host: {err}") from err

    config_entry.async_on_unload(coordinator.async_close)
    await coordinator.async_config_entry_first_refresh()
    config_entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    config_entry.async_on_unload(config_entry.add_update_listener(async_reload_entry))
    return True


# ---------------------------
#   async_reload_entry
# ---------------------------
async def async_reload_entry(
    hass: HomeAssistant, config_entry: TrueNASConfigEntry
) -> None:
    """Reload the config entry when it changed."""
    await hass.config_entries.async_reload(config_entry.entry_id)


# ---------------------------
#   async_unload_entry
# ---------------------------
async def async_unload_entry(
    hass: HomeAssistant, config_entry: TrueNASConfigEntry
) -> bool:
    """Unload TrueNAS config entry."""

    return await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
