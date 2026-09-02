"""Tests for the service control entity actions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.truenas.binary_sensor import TrueNASServiceBinarySensor


def _sensor(state: str, methods: set[str] | None) -> TrueNASServiceBinarySensor:
    """Build a service sensor with a mocked coordinator."""
    sensor = TrueNASServiceBinarySensor.__new__(TrueNASServiceBinarySensor)
    sensor._data = {"id": 1, "service": "ssh"}
    sensor.coordinator = MagicMock()
    sensor.coordinator.async_refresh = AsyncMock()
    sensor.coordinator.supports = lambda method: methods is None or method in methods
    sensor.coordinator.api.query = AsyncMock(
        side_effect=lambda method, params=None: (
            {"id": 1, "service": "ssh", "state": state}
            if method == "service.get_instance"
            else None
        )
    )
    return sensor


def _calls(sensor) -> list:
    return [c.args for c in sensor.coordinator.api.query.call_args_list]


@pytest.mark.parametrize(
    ("action", "state", "verb"),
    [
        ("start", "STOPPED", "START"),
        ("stop", "RUNNING", "STOP"),
        ("restart", "RUNNING", "RESTART"),
        ("reload", "RUNNING", "RELOAD"),
    ],
)
async def test_uses_service_control_when_available(action, state, verb) -> None:
    """TrueNAS 26 takes the operation as an argument to service.control."""
    sensor = _sensor(state, {"service.control"})

    await getattr(sensor, action)()

    assert _calls(sensor)[-1] == ("service.control", [verb, "ssh"])


@pytest.mark.parametrize(
    ("action", "state", "method"),
    [
        ("start", "STOPPED", "service.start"),
        ("stop", "RUNNING", "service.stop"),
        ("restart", "RUNNING", "service.restart"),
        ("reload", "RUNNING", "service.reload"),
    ],
)
async def test_falls_back_to_the_old_methods(action, state, method) -> None:
    """An older TrueNAS keeps its per-operation methods."""
    sensor = _sensor(state, {"service.query"})

    await getattr(sensor, action)()

    assert _calls(sensor)[-1] == (method, ["ssh"])


async def test_stopped_service_is_not_restarted() -> None:
    """Operations that need a running service are skipped when it is stopped."""
    sensor = _sensor("STOPPED", {"service.control"})

    await sensor.restart()

    assert _calls(sensor) == [("service.get_instance", [1])]


async def test_running_service_is_not_started_again() -> None:
    """Starting an already running service is skipped."""
    sensor = _sensor("RUNNING", {"service.control"})

    await sensor.start()

    assert _calls(sensor) == [("service.get_instance", [1])]
