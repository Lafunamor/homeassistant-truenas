"""Tests for the TrueNAS coordinator."""

from __future__ import annotations

import re

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

    assert set(coordinator._systemstats_errored) == {("cputemp", None)}
    # Every other graph still made it through the per graph retry.
    assert coordinator.ds["system_info"]["cpu_usage"] == 12.5


async def test_systemstats_skips_errored_graphs(coordinator) -> None:
    """Graphs known to fail are not requested again."""
    coordinator._systemstats_errored = {
        ("cputemp", None): 5,
        ("arcsize", None): 5,
    }
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
    assert coordinator._systemstats_errored == {}


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


async def test_muted_graph_is_retried_eventually(coordinator) -> None:
    """A graph that failed once comes back instead of staying dead."""
    coordinator._systemstats_errored = {("cputemp", None): 2}
    coordinator.api.query.side_effect = lambda service, params=None: []

    await coordinator.get_systemstats()
    assert "cputemp" not in _graph_names(coordinator.api.query.call_args)

    await coordinator.get_systemstats()

    assert "cputemp" in _graph_names(coordinator.api.query.call_args)
    assert coordinator._systemstats_errored == {}


async def test_one_bad_interface_does_not_mute_the_others(coordinator) -> None:
    """Muting is per identifier, not per graph name."""
    coordinator.ds["interface"] = {"eth0": {}, "eth1": {}}

    def query(service, params=None):
        graphs = params[0]
        if len(graphs) > 1:
            return None

        # eth0 cannot be reported, everything else can
        if graphs[0].get("identifier") == "eth0":
            return None

        return []

    coordinator.api.query.side_effect = query
    await coordinator.get_systemstats()

    assert ("interface", "eth0") in coordinator._systemstats_errored
    assert ("interface", "eth1") not in coordinator._systemstats_errored

    coordinator.api.query.side_effect = lambda service, params=None: []
    await coordinator.get_systemstats()

    requested = [
        graph.get("identifier")
        for graph in coordinator.api.query.call_args.kwargs["params"][0]
        if graph["name"] == "interface"
    ]
    assert requested == ["eth1"]


async def test_real_netdata_legends(coordinator) -> None:
    """The legend names TrueNAS actually sends must land in the right keys."""
    # Captured from a live TrueNAS 25.10.5 system.
    coordinator.api.query.side_effect = lambda service, params=None: [
        _aggregated("memory", ["time", "available"], {"available": 13086022180.3}),
        _aggregated(
            "cpu",
            ["time", "cpu", "cpu0", "cpu1"],
            {"cpu": 5.31, "cpu0": 5.39, "cpu1": 5.36},
        ),
        _aggregated(
            "load",
            ["time", "shortterm", "midterm", "longterm"],
            {"shortterm": 0.52, "midterm": 1.13, "longterm": 1.16},
        ),
        # netdata calls the ARC dimension "size", not "arc_size".
        _aggregated("arcsize", ["time", "size"], {"size": 6104878524.5}),
        _aggregated(
            "cputemp",
            ["time", "cpu0", "cpu1", "cpu"],
            {"cpu0": 32.2, "cpu1": 35.3, "cpu": 33.7},
        ),
    ]
    coordinator.ds["system_info"]["physmem"] = 34359738368

    await coordinator.get_systemstats()

    system_info = coordinator.ds["system_info"]
    assert system_info["cache_size-arc_value"] == 6104878524.5
    assert system_info["cpu_usage"] == 5.31
    assert system_info["load_shortterm"] == 0.52
    assert system_info["load_longterm"] == 1.16
    assert system_info["cpu_temperature"] == 35.3
    assert system_info["memory-total_value"] == 34359738368
    assert system_info["memory-free_value"] == round(13086022180.3)
    # used is derived, the graph does not report it
    assert system_info["memory-used_value"] == 34359738368 - round(13086022180.3)


async def test_no_junk_cpu_attributes(coordinator) -> None:
    """Per-core legend entries must not become cpu_* attributes."""
    coordinator.api.query.side_effect = lambda service, params=None: [
        _aggregated(
            "cpu",
            ["time", "cpu", "cpu0", "cpu1"],
            {"cpu": 5.0, "cpu0": 4.0, "cpu1": 6.0},
        )
    ]

    await coordinator.get_systemstats()

    # cpu_cpu is the internal key the overall figure lands in; per-core
    # entries and the time axis must not become attributes of their own.
    junk = [
        k
        for k in coordinator.ds["system_info"]
        if re.fullmatch(r"cpu_cpu\d+", k) or k == "cpu_time"
    ]
    assert junk == []
    assert coordinator.ds["system_info"]["cpu_usage"] == 5.0
