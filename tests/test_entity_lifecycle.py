"""Tests for adding and removing entities while the integration runs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.truenas.const import DOMAIN

APP = {
    "id": "plex",
    "name": "plex",
    "human_version": "1.0",
    "version": "1.0",
    "latest_version": "1.0",
    "upgrade_available": False,
    "state": "RUNNING",
    "portals": {"Web UI": "http://x"},
}
SECOND_APP = {**APP, "id": "sonarr", "name": "sonarr"}
SYSTEM_INFO = {
    "version": "TrueNAS-25.10.5",
    "hostname": "truenas",
    "uptime_seconds": 3600,
    "system_serial": "1234",
    "system_product": "Custom",
    "system_manufacturer": "ASUS",
    "physmem": 1024,
}


@pytest.fixture(name="responses")
def responses_fixture() -> dict:
    """Mutable canned API responses."""
    return {"system.info": SYSTEM_INFO, "app.query": [APP]}


@pytest.fixture(name="entry")
async def entry_fixture(hass: HomeAssistant, responses) -> MockConfigEntry:
    """Set the integration up against the canned responses."""
    api = MagicMock()
    api.connected.return_value = True
    api.error = ""
    api.connect = AsyncMock(return_value=True)
    api.disconnect = AsyncMock()
    api.query = AsyncMock(
        side_effect=lambda service, params=None: responses.get(service)
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "TrueNAS",
            CONF_HOST: "10.0.0.1",
            CONF_API_KEY: "key",
            CONF_VERIFY_SSL: False,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.truenas.coordinator.TrueNASAPI", return_value=api):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry


async def test_new_object_gets_an_entity(hass, entry, responses) -> None:
    """An app added on the NAS appears without reloading the integration."""
    assert hass.states.get("binary_sensor.truenas_apps_sonarr") is None

    responses["app.query"] = [APP, SECOND_APP]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.truenas_apps_sonarr") is not None


async def test_removed_object_goes_unavailable(hass, entry, responses) -> None:
    """An app removed on the NAS stops reporting a stale state."""
    assert hass.states.get("binary_sensor.truenas_apps_plex").state == "on"

    entry.runtime_data.ds["app"] = {}
    responses["app.query"] = []
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.truenas_apps_plex").state == "unavailable"


async def test_entity_is_not_added_twice(hass, entry, responses) -> None:
    """Repeated updates do not create duplicate entities."""
    before = len(hass.states.async_entity_ids())

    await entry.runtime_data.async_refresh()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert len(hass.states.async_entity_ids()) == before
