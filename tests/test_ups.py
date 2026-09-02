"""Tests for the UPS sensors.

The values come from the reporting graphs because the ups.* API namespace
only covers configuration. The dimension inside each graph is named by the
UPS driver, so it is read by position; these tests use both a plausible name
and a deliberately odd one to keep that property honest.
"""

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


def _graph(name, mean, identifier=None):
    graph = {"name": name, "legend": ["time", *mean], "aggregations": {"mean": mean}}
    if identifier is not None:
        graph["identifier"] = identifier
    return graph


def _ups_running(coordinator, running=True, enable=True):
    coordinator.ds["service"] = {
        1: {"id": 1, "service": "ups", "running": running, "enable": enable},
        2: {"id": 2, "service": "ssh", "running": True, "enable": True},
    }


async def test_no_ups_means_no_sensors(coordinator) -> None:
    """A NAS without a configured UPS must not grow empty sensors."""
    # This is what a live TrueNAS with no UPS returns: the graphs succeed
    # but carry no dimensions, so failure alone cannot be the signal.
    coordinator.ds["service"] = {
        1: {"id": 1, "service": "ups", "running": False, "enable": False}
    }

    await coordinator.get_ups()

    assert coordinator.ds["ups"] == {}
    coordinator.api.query.assert_not_called()


async def test_ups_service_absent_entirely(coordinator) -> None:
    """An older TrueNAS may not list a ups service at all."""
    coordinator.ds["service"] = {1: {"id": 1, "service": "ssh", "running": True}}

    await coordinator.get_ups()

    assert coordinator.ds["ups"] == {}
    coordinator.api.query.assert_not_called()


async def test_values_are_collected(coordinator) -> None:
    """Each graph maps onto its own value."""
    _ups_running(coordinator)
    coordinator.api.query.side_effect = lambda service, params=None: [
        _graph("upscharge", {"charge": 100.0}),
        _graph("upsruntime", {"runtime": 1680.0}),
        _graph("upsload", {"load": 18.5}),
        _graph("upstemperature", {"temp": 27.4}),
        _graph("upscurrent", {"current": 0.6}),
        _graph("upsfrequency", {"frequency": 50.0}),
        _graph("upsvoltage", {"voltage": 27.3}, identifier="battery"),
        _graph("upsvoltage", {"voltage": 232.0}, identifier="input"),
        _graph("upsvoltage", {"voltage": 230.0}, identifier="output"),
    ]

    await coordinator.get_ups()

    assert coordinator.ds["ups"] == {
        "charge": 100.0,
        "runtime": 1680.0,
        "load": 18.5,
        "temperature": 27.4,
        "current": 0.6,
        "frequency": 50.0,
        "voltage_battery": 27.3,
        "voltage_input": 232.0,
        "voltage_output": 230.0,
    }


async def test_dimension_name_does_not_matter(coordinator) -> None:
    """The dimension is named by the UPS driver, so it is read by position."""
    _ups_running(coordinator)
    coordinator.api.query.side_effect = lambda service, params=None: [
        _graph("upscharge", {"battery.charge": 87.0}),
        _graph("upsvoltage", {"whatever_the_driver_calls_it": 231.5}, "input"),
    ]

    await coordinator.get_ups()

    assert coordinator.ds["ups"]["charge"] == 87.0
    assert coordinator.ds["ups"]["voltage_input"] == 231.5


async def test_empty_graph_yields_no_value(coordinator) -> None:
    """A graph with no dimensions contributes nothing rather than a zero."""
    _ups_running(coordinator)
    coordinator.api.query.side_effect = lambda service, params=None: [
        _graph("upscharge", {}),
        _graph("upsload", {"load": 12.0}),
    ]

    await coordinator.get_ups()

    assert "charge" not in coordinator.ds["ups"]
    assert coordinator.ds["ups"]["load"] == 12.0


async def test_ambiguous_graph_is_skipped(coordinator) -> None:
    """More than one dimension means the value cannot be read by position."""
    _ups_running(coordinator)
    coordinator.api.query.side_effect = lambda service, params=None: [
        _graph("upscharge", {"a": 1.0, "b": 2.0}),
    ]

    await coordinator.get_ups()

    assert coordinator.ds["ups"] == {}


async def test_failed_query_keeps_previous_values(coordinator) -> None:
    """A transient failure must not clear the sensors."""
    _ups_running(coordinator)
    coordinator.api.query.side_effect = lambda service, params=None: [
        _graph("upsload", {"load": 20.0})
    ]
    await coordinator.get_ups()

    coordinator.api.query.side_effect = lambda service, params=None: None
    await coordinator.get_ups()

    assert coordinator.ds["ups"]["load"] == 20.0


async def test_enabled_but_stopped_is_still_collected(coordinator) -> None:
    """A UPS service that is configured but momentarily down still counts."""
    _ups_running(coordinator, running=False, enable=True)
    coordinator.api.query.side_effect = lambda service, params=None: [
        _graph("upscharge", {"charge": 55.0})
    ]

    await coordinator.get_ups()

    assert coordinator.ds["ups"]["charge"] == 55.0


async def test_ratings_are_not_readings(coordinator) -> None:
    """The graphs of a real UPS carry its ratings next to the measurement.

    This is the reply of a TrueNAS with a Back-UPS XS 1000M attached: the
    voltage graphs report the nominal voltage as a second dimension, and the
    graphs the driver does not feed come back empty.
    """
    _ups_running(coordinator, running=True, enable=False)
    coordinator.api.query.side_effect = lambda service, params=None: [
        _graph("upscharge", {"charge": 90.0}),
        _graph("upsruntime", {"runtime": 3007.4333333333334}),
        _graph("upsload", {}),
        _graph("upstemperature", {}),
        _graph("upscurrent", {}),
        _graph("upsfrequency", {}),
        _graph("upsvoltage", {"voltage": 28.399999999999984, "nominal": 24.0}, "battery"),
        _graph("upsvoltage", {"voltage": 235.7, "nominal": 230.0}, "input"),
        _graph("upsvoltage", {}, "output"),
    ]

    await coordinator.get_ups()

    assert coordinator.ds["ups"] == {
        "charge": 90.0,
        "runtime": 3007.43,
        "voltage_battery": 28.4,
        "voltage_input": 235.7,
    }


async def test_rating_only_graph_yields_no_value(coordinator) -> None:
    """A graph that reports nothing but a rating is not a measurement."""
    _ups_running(coordinator)
    coordinator.api.query.side_effect = lambda service, params=None: [
        _graph("upscurrent", {"nominal": 4.0}),
        _graph("upsfrequency", {"frequency": 49.9, "nominal": 50.0}),
    ]

    await coordinator.get_ups()

    assert "current" not in coordinator.ds["ups"]
    assert coordinator.ds["ups"]["frequency"] == 49.9


async def test_graph_without_aggregation_yields_no_value(coordinator) -> None:
    """A variable the driver does not report arrives without a mean.

    Its graph exists and is filled with zeros, but TrueNAS leaves an
    all-zero dimension out of the aggregation, so nothing has to be told
    apart from a real reading of zero.
    """
    _ups_running(coordinator)
    coordinator.api.query.side_effect = lambda service, params=None: [
        {"name": "upsload", "legend": ["time", "load"], "aggregations": {"mean": {}}},
        {"name": "upstemperature", "legend": ["time", "temp"], "aggregations": {}},
        {"name": "upscharge", "legend": ["time", "charge"]},
        _graph("upsruntime", {"runtime": 3045.6666666666665}),
    ]

    await coordinator.get_ups()

    assert coordinator.ds["ups"] == {"runtime": 3045.67}
