"""Tests for the TrueNAS coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.truenas.const import DOMAIN
from custom_components.truenas.coordinator import TrueNASCoordinator

REPORTING = "reporting.netdata_get_data"


def _aggregated(name: str, legend: list[str], mean: dict) -> dict:
    """Build a netdata graph reply."""
    return {"name": name, "legend": legend, "aggregations": {"mean": mean}}


@pytest.fixture(name="coordinator")
def coordinator_fixture(hass: HomeAssistant) -> TrueNASCoordinator:
    """Return a coordinator with a mocked API."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "TrueNAS",
            CONF_HOST: "http://10.0.0.1",
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


def _graph_names(call) -> list[str]:
    """Return the graph names of a reporting call."""
    return [graph["name"] for graph in call.kwargs["params"][0]]


async def test_systemstats_retries_each_graph(coordinator) -> None:
    """A rejected batch is retried per graph so one bad graph is not fatal."""

    def query(service, params=None):
        assert service == REPORTING
        names = [graph["name"] for graph in params[0]]
        if len(names) > 1:
            return None
        if names == ["cputemp"]:
            return None
        return [_aggregated("cpu", ["cpu"], {"cpu": 12.5})]

    coordinator.api.query.side_effect = query
    await coordinator.get_systemstats()

    assert coordinator._systemstats_errored == ["cputemp"]
    # Every other graph still made it through the per graph retry.
    assert coordinator.ds["system_info"]["cpu_usage"] == 12.5


async def test_systemstats_skips_errored_graphs(coordinator) -> None:
    """Graphs known to fail are not requested again."""
    coordinator._systemstats_errored = ["cputemp", "arcsize"]
    coordinator.api.query.side_effect = lambda service, params=None: []

    await coordinator.get_systemstats()

    requested = _graph_names(coordinator.api.query.call_args)
    assert "cputemp" not in requested
    assert "arcsize" not in requested
    assert "cpu" in requested


async def test_systemstats_stops_when_disconnected(coordinator) -> None:
    """Losing the connection must not trigger a retry per graph."""
    coordinator.api.connected.return_value = False
    coordinator.api.query.side_effect = lambda service, params=None: None

    await coordinator.get_systemstats()

    assert coordinator.api.query.call_count == 1
    assert coordinator._systemstats_errored == []


async def test_systemstats_without_aggregations(coordinator) -> None:
    """A graph without aggregations zeroes its values instead of crashing."""
    coordinator.ds["interface"] = {"eth0": {"rx": 5.0, "tx": 5.0}}
    coordinator.api.query.side_effect = lambda service, params=None: [
        {"name": "memory", "legend": []},
        {"name": "cpu", "legend": []},
        {"name": "arcsize", "legend": []},
        {"name": "interface", "identifier": "eth0", "legend": []},
    ]

    await coordinator.get_systemstats()

    system_info = coordinator.ds["system_info"]
    assert system_info["memory-free_value"] == 0.0
    assert system_info["cpu_cpu"] == 0.0
    assert system_info["cache_size-arc_value"] == 0.0
    assert coordinator.ds["interface"]["eth0"] == {"rx": 0.0, "tx": 0.0}
    # The legend names must never be iterated character by character.
    assert not [key for key in system_info if len(key) == 1]


async def test_systemstats_interface_rates(coordinator) -> None:
    """Interface traffic is mapped from the netdata legend onto rx/tx."""
    coordinator.ds["interface"] = {"eth0": {"rx": 0.0, "tx": 0.0}}
    coordinator.api.query.side_effect = lambda service, params=None: [
        _aggregated(
            "interface",
            ["received", "sent"],
            {"received": 100.0, "sent": 200.0},
        )
        | {"identifier": "eth0"}
    ]

    await coordinator.get_systemstats()

    assert coordinator.ds["interface"]["eth0"]["rx"] == round(100.0 * 0.12207, 2)
    assert coordinator.ds["interface"]["eth0"]["tx"] == round(200.0 * 0.12207, 2)


async def test_systemstats_cputemp_skipped_on_virtual(coordinator) -> None:
    """A virtual machine has no CPU temperature graph."""
    coordinator._is_virtual = True
    coordinator.api.query.side_effect = lambda service, params=None: []

    await coordinator.get_systemstats()

    assert "cputemp" not in _graph_names(coordinator.api.query.call_args)


async def test_interface_graph_with_incomplete_aggregations(coordinator) -> None:
    """An interface graph without a usable mean must not abort the update."""
    coordinator.ds["interface"] = {"eth0": {"rx": 5.0, "tx": 5.0}}
    coordinator.api.query.side_effect = lambda service, params=None: [
        {"name": "interface", "identifier": "eth0", "aggregations": {}},
    ]

    await coordinator.get_systemstats()

    assert coordinator.ds["interface"]["eth0"] == {"rx": 0.0, "tx": 0.0}
