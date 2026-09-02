"""Tests for setting up and tearing down the TrueNAS config entry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.truenas.const import DOMAIN

ENTRY_DATA = {
    CONF_NAME: "TrueNAS",
    CONF_HOST: "http://10.0.0.1",
    CONF_API_KEY: "key",
    CONF_VERIFY_SSL: False,
}


@pytest.fixture(name="api")
def api_fixture():
    """Patch the API used by the coordinator with a connected stub."""
    api = MagicMock()
    api.connected.return_value = True
    api.error = ""
    api.query = AsyncMock(return_value=None)
    api.connect = AsyncMock(return_value=True)
    api.disconnect = AsyncMock()
    with patch("custom_components.truenas.coordinator.TrueNASAPI", return_value=api):
        yield api


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_and_unload(hass: HomeAssistant, api) -> None:
    """The entry sets up, exposes the coordinator and closes the socket."""
    entry = await _setup(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    api.disconnect.assert_called()


async def test_reload_does_not_leak_connection(hass: HomeAssistant, api) -> None:
    """Reloading closes the previous connection instead of leaking it."""
    entry = await _setup(hass)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    api.disconnect.assert_called()


async def test_setup_rejects_invalid_host(hass: HomeAssistant) -> None:
    """An entry with an unusable host fails setup instead of raising."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={**ENTRY_DATA, CONF_HOST: "ftp://10.0.0.1"}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
