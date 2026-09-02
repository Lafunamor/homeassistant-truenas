"""Tests for values the TrueNAS API reports as null or omits entirely."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.truenas.apiparser import from_entry, parse_api
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


# ---------------------------
#   from_entry
# ---------------------------
@pytest.mark.parametrize(
    ("value", "default"),
    [(None, 0), (None, "unknown"), (None, 0.0), (None, "")],
)
def test_null_falls_back_to_default(value, default) -> None:
    """A null from the API must not reach the entities as None."""
    assert from_entry({"field": value}, "field", default=default) == default


def test_null_in_a_nested_path() -> None:
    """A null at the end of a source path also falls back."""
    assert (
        from_entry({"scan": {"total_secs_left": None}}, "scan/total_secs_left", 0) == 0
    )


def test_real_values_are_untouched() -> None:
    """The fallback must not swallow legitimate zero or empty values."""
    assert from_entry({"field": 0}, "field", default=99) == 0
    assert from_entry({"field": ""}, "field", default="unknown") == ""
    assert from_entry({"field": "value"}, "field", default="unknown") == "value"


async def test_vm_without_a_memory_limit(coordinator) -> None:
    """An instance with no memory limit must not crash the whole update."""
    coordinator._methods = {"virt.instance.query"}
    coordinator.api.query.return_value = [
        {
            "id": "ubuntu",
            "name": "ubuntu",
            "type": "CONTAINER",
            "cpu": None,
            "memory": None,
            "autostart": True,
            "status": "RUNNING",
        }
    ]

    await coordinator.get_vm()

    assert coordinator.ds["vm"]["ubuntu"]["memory"] == 0
    assert coordinator.ds["vm"]["ubuntu"]["cpu"] == 0


async def test_pool_without_a_running_scrub(coordinator) -> None:
    """scan.total_secs_left is null when no scrub is running."""
    data = parse_api(
        data={},
        source=[{"guid": "1", "name": "tank", "scan": {"total_secs_left": None}}],
        key="guid",
        vals=[
            {"name": "name", "default": "unknown"},
            {"name": "scrub_secs_left", "source": "scan/total_secs_left", "default": 0},
        ],
    )

    assert data["1"]["scrub_secs_left"] == 0


# ---------------------------
#   system statistics
# ---------------------------
async def test_cputemp_without_readings(coordinator) -> None:
    """A NAS that reports no CPU temperature must not crash the update."""
    coordinator.api.query.return_value = [
        {"name": "cputemp", "legend": [], "aggregations": {"mean": {}}}
    ]

    await coordinator.get_systemstats()

    assert coordinator.ds["system_info"]["cpu_temperature"] == 0.0


async def test_cputemp_with_null_readings(coordinator) -> None:
    """A null reading for one core must not poison the maximum."""
    coordinator.api.query.return_value = [
        {
            "name": "cputemp",
            "legend": ["cpu0", "cpu1"],
            "aggregations": {"mean": {"cpu0": None, "cpu1": 41.0}},
        }
    ]

    await coordinator.get_systemstats()

    assert coordinator.ds["system_info"]["cpu_temperature"] == 41.0


# ---------------------------
#   boot pool
# ---------------------------
async def test_boot_pool_survives_a_failed_query(coordinator) -> None:
    """boot.get_state failing after a good cycle must not raise KeyError."""
    boot_state = {
        "name": "boot-pool",
        "path": "/",
        "status": "ONLINE",
        "healthy": True,
        "allocated": 10,
        "free": 90,
        "root_dataset": {
            "properties": {"available": {"parsed": 90}, "used": {"parsed": 10}}
        },
        "scan": {"function": "SCRUB", "state": "FINISHED"},
    }

    def query(service, params=None):
        return {"pool.query": [], "boot.get_state": boot_state}.get(service)

    coordinator.api.query.side_effect = query
    await coordinator.get_pool()
    assert "root_dataset" not in coordinator.ds["pool"]["boot-pool"]

    # A JSON-RPC level error returns None without dropping the connection.
    coordinator.api.query.side_effect = lambda service, params=None: None
    await coordinator.get_pool()

    assert coordinator.ds["pool"]["boot-pool"]["name"] == "boot-pool"


# ---------------------------
#   uptime
# ---------------------------
async def test_uptime_default_is_not_an_int(coordinator) -> None:
    """The uptime sensor is a timestamp, so its default cannot be 0."""
    coordinator.api.query.return_value = None

    await coordinator.get_systeminfo()

    assert coordinator.ds["system_info"]["uptimeEpoch"] is None
