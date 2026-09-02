"""Tests for how often SMART disk temperatures are read."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.truenas.const import CONF_DISK_TEMPERATURES, DOMAIN
from custom_components.truenas.coordinator import TrueNASCoordinator

DISK = {
    "identifier": "{uuid}d1",
    "name": "sda",
    "devname": "sda",
    "serial": "S1",
    "size": 1000,
    "model": "WD",
    "type": "HDD",
}


def _coordinator(hass: HomeAssistant, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "TrueNAS",
            CONF_HOST: "10.0.0.1",
            CONF_API_KEY: "key",
            CONF_VERIFY_SSL: False,
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.truenas.coordinator.TrueNASAPI") as api_class:
        api_class.return_value = MagicMock()
        coordinator = TrueNASCoordinator(hass, entry)

    coordinator.api.connected.return_value = True
    coordinator.api.connect = AsyncMock(return_value=True)
    coordinator.api.disconnect = AsyncMock()
    coordinator.api.query = AsyncMock(
        side_effect=lambda service, params=None: (
            [DISK] if service == "disk.query" else {"sda": 35}
        )
    )
    return coordinator


def _temperature_calls(coordinator) -> int:
    return len(
        [
            c
            for c in coordinator.api.query.call_args_list
            if c.args[0] == "disk.temperatures"
        ]
    )


@pytest.fixture(name="coordinator")
def coordinator_fixture(hass: HomeAssistant) -> TrueNASCoordinator:
    """Return a coordinator with disk temperatures enabled."""
    return _coordinator(hass)


async def test_temperature_is_read_once_per_interval(coordinator) -> None:
    """TrueNAS caches SMART data for five minutes; do not ask more often."""
    await coordinator.get_disk()
    assert _temperature_calls(coordinator) == 1
    assert coordinator.ds["disk"]["{uuid}d1"]["temperature"] == 35

    # four more one-minute updates must not touch SMART again
    for _ in range(4):
        await coordinator.get_disk()

    assert _temperature_calls(coordinator) == 1
    # the last reading is still reported
    assert coordinator.ds["disk"]["{uuid}d1"]["temperature"] == 35


async def test_temperature_is_refreshed_after_the_interval(coordinator) -> None:
    """Once the cache is stale the reading is taken again."""
    await coordinator.get_disk()
    coordinator._disk_temperatures_read = datetime.now() - timedelta(minutes=6)

    await coordinator.get_disk()

    assert _temperature_calls(coordinator) == 2


async def test_temperature_can_be_turned_off(hass: HomeAssistant) -> None:
    """Users whose disks will not spin down can stop the SMART reads."""
    coordinator = _coordinator(hass, {CONF_DISK_TEMPERATURES: False})

    await coordinator.get_disk()

    assert _temperature_calls(coordinator) == 0
    # reported as unknown rather than a misleading 0 degrees
    assert coordinator.ds["disk"]["{uuid}d1"]["temperature"] is None


async def test_disks_are_still_reported_when_turned_off(hass: HomeAssistant) -> None:
    """Turning the reads off must not remove the disks themselves."""
    coordinator = _coordinator(hass, {CONF_DISK_TEMPERATURES: False})

    await coordinator.get_disk()

    assert coordinator.ds["disk"]["{uuid}d1"]["model"] == "WD"
