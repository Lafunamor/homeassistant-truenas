"""Tests for API method capability discovery."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.truenas.const import DOMAIN
from custom_components.truenas.coordinator import TrueNASCoordinator


@pytest.fixture(name="coordinator")
def coordinator_fixture(hass: HomeAssistant) -> TrueNASCoordinator:
    """Return a coordinator with a mocked API."""
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
    with patch("custom_components.truenas.coordinator.TrueNASAPI") as api_class:
        api_class.return_value = MagicMock()
        coordinator = TrueNASCoordinator(hass, entry)

    coordinator.api.connected.return_value = True
    coordinator.api.query = AsyncMock()
    coordinator.api.connect = AsyncMock(return_value=True)
    coordinator.api.disconnect = AsyncMock()
    return coordinator


async def test_supports_everything_before_discovery(coordinator) -> None:
    """Without a method list every call is attempted."""
    assert coordinator.supports("anything.at.all") is True


async def test_get_methods_from_mapping(coordinator) -> None:
    """core.get_methods returns a mapping of name to metadata."""
    coordinator.api.query.return_value = {"app.query": {}, "system.info": {}}

    await coordinator.get_methods()

    assert coordinator.supports("app.query") is True
    assert coordinator.supports("update.check_available") is False


async def test_get_methods_fails_open(coordinator) -> None:
    """A NAS that cannot list its methods keeps working as before."""
    coordinator.api.query.return_value = None

    await coordinator.get_methods()

    assert coordinator.supports("app.query") is True


async def test_unsupported_calls_are_skipped(coordinator) -> None:
    """Getters for methods the NAS does not expose make no API call."""
    coordinator._methods = {"system.info"}
    coordinator.api.query.reset_mock()

    await coordinator.get_app()
    await coordinator.get_cloudsync()
    await coordinator.get_replication()
    await coordinator.get_snapshottask()
    await coordinator.get_systemstats()

    coordinator.api.query.assert_not_called()


async def test_supported_calls_still_run(coordinator) -> None:
    """A method the NAS does expose is still queried."""
    coordinator._methods = {"app.query"}
    coordinator.api.query.return_value = []

    await coordinator.get_app()

    coordinator.api.query.assert_called_once_with("app.query")


# ---------------------------
#   system update API
# ---------------------------
UPDATE_STATUS_AVAILABLE = {
    "code": "NORMAL",
    "status": {
        "current_version": {"train": "25.10", "profile": "GENERAL"},
        "new_version": {"version": "TrueNAS-25.10.6", "manifest": {}},
    },
    "error": None,
    "update_download_progress": None,
}
UPDATE_STATUS_UP_TO_DATE = {
    "code": "NORMAL",
    "status": {
        "current_version": {"train": "25.10", "profile": "GENERAL"},
        "new_version": None,
    },
    "error": None,
    "update_download_progress": None,
}


async def test_update_status_reports_available_update(coordinator) -> None:
    """update.status signals an update through status.new_version."""
    coordinator._methods = {"update.status"}
    coordinator.ds["system_info"]["version"] = "TrueNAS-25.10.5"
    coordinator.api.query.return_value = UPDATE_STATUS_AVAILABLE

    await coordinator.get_updatecheck()

    coordinator.api.query.assert_called_once_with("update.status")
    assert coordinator.ds["system_info"]["update_available"] is True
    assert coordinator.ds["system_info"]["update_version"] == "TrueNAS-25.10.6"


async def test_update_status_reports_up_to_date(coordinator) -> None:
    """No new_version means the running version is the latest."""
    coordinator._methods = {"update.status"}
    coordinator.ds["system_info"]["version"] = "TrueNAS-25.10.5"
    coordinator.api.query.return_value = UPDATE_STATUS_UP_TO_DATE

    await coordinator.get_updatecheck()

    assert coordinator.ds["system_info"]["update_available"] is False
    assert coordinator.ds["system_info"]["update_version"] == "TrueNAS-25.10.5"


async def test_update_falls_back_to_check_available(coordinator) -> None:
    """An older TrueNAS keeps using update.check_available."""
    coordinator._methods = {"update.check_available"}
    coordinator.ds["system_info"]["version"] = "TrueNAS-24.10.0"
    coordinator.api.query.return_value = {
        "status": "AVAILABLE",
        "version": "TrueNAS-24.10.1",
    }

    await coordinator.get_updatecheck()

    coordinator.api.query.assert_called_once_with("update.check_available")
    assert coordinator.ds["system_info"]["update_available"] is True
    assert coordinator.ds["system_info"]["update_version"] == "TrueNAS-24.10.1"


async def test_update_check_skipped_when_unsupported(coordinator) -> None:
    """A NAS with neither method is not polled for updates."""
    coordinator._methods = {"system.info"}

    await coordinator.get_updatecheck()

    coordinator.api.query.assert_not_called()


async def test_system_update_method(coordinator) -> None:
    """Installing an update uses update.run where it exists."""
    coordinator._methods = {"update.run"}
    assert coordinator.system_update_method() == "update.run"

    coordinator._methods = {"update.update"}
    assert coordinator.system_update_method() == "update.update"
