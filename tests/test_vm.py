"""Tests for virtual machine discovery across both TrueNAS VM APIs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.truenas.const import DOMAIN
from custom_components.truenas.coordinator import TrueNASCoordinator

VIRT_INSTANCE = {
    "id": "incus-vm",
    "name": "incus-vm",
    "type": "VM",
    "cpu": 4,
    "memory": 8 * 1024**3,
    "autostart": True,
    "status": "RUNNING",
    "image": {"description": "Debian 13"},
}
LEGACY_VM = {
    "id": 3,
    "name": "legacy-vm",
    "description": "an older VM",
    "vcpus": 2,
    "cores": 1,
    "threads": 1,
    "memory": 4096,
    "autostart": False,
    "status": {"state": "STOPPED", "pid": None, "domain_state": "SHUTOFF"},
}


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


def _responses(**by_method):
    def query(service, params=None):
        return by_method.get(service)

    return query


async def test_both_apis_are_merged(coordinator) -> None:
    """Instances from either API end up as separate VMs."""
    coordinator.api.query.side_effect = _responses(
        **{"virt.instance.query": [VIRT_INSTANCE], "vm.query": [LEGACY_VM]}
    )

    await coordinator.get_vm()

    vms = coordinator.ds["vm"]
    assert len(vms) == 2
    names = {vm["name"] for vm in vms.values()}
    assert names == {"incus-vm", "legacy-vm"}


async def test_legacy_vm_fields(coordinator) -> None:
    """A libvirt VM is mapped onto the same attributes as an Incus one."""
    coordinator.api.query.side_effect = _responses(**{"vm.query": [LEGACY_VM]})

    await coordinator.get_vm()

    vm = coordinator.ds["vm"]["vm-3"]
    assert vm["id"] == 3
    assert vm["name"] == "legacy-vm"
    assert vm["cpu"] == 2
    # vm.query reports megabytes, the entity shows GiB
    assert vm["memory"] == 4
    assert vm["type"] == "VM"
    assert vm["status"] == "STOPPED"
    assert vm["running"] is False
    assert vm["api"] == "vm"


async def test_virt_instance_fields(coordinator) -> None:
    """An Incus instance keeps its existing mapping."""
    coordinator.api.query.side_effect = _responses(
        **{"virt.instance.query": [VIRT_INSTANCE]}
    )

    await coordinator.get_vm()

    vm = coordinator.ds["vm"]["incus-vm"]
    # virt.instance.query reports bytes, the entity shows GiB
    assert vm["memory"] == 8
    assert vm["cpu"] == 4
    assert vm["running"] is True
    assert vm["image"] == "Debian 13"
    assert vm["api"] == "virt"


async def test_keys_do_not_collide(coordinator) -> None:
    """A libvirt id and an Incus id that stringify alike stay separate."""
    coordinator.api.query.side_effect = _responses(
        **{
            "virt.instance.query": [{**VIRT_INSTANCE, "id": "3", "name": "incus"}],
            "vm.query": [LEGACY_VM],
        }
    )

    await coordinator.get_vm()

    assert set(coordinator.ds["vm"]) == {"3", "vm-3"}


async def test_only_supported_apis_are_queried(coordinator) -> None:
    """A NAS without the legacy API is not asked for it."""
    coordinator._methods = {"virt.instance.query"}
    coordinator.api.query.side_effect = _responses(
        **{"virt.instance.query": [VIRT_INSTANCE]}
    )

    await coordinator.get_vm()

    assert [call.args[0] for call in coordinator.api.query.call_args_list] == [
        "virt.instance.query"
    ]


async def test_failed_query_keeps_known_vms(coordinator) -> None:
    """A transient failure does not wipe the VMs already discovered."""
    coordinator.api.query.side_effect = _responses(**{"vm.query": [LEGACY_VM]})
    await coordinator.get_vm()

    coordinator.api.query.side_effect = _responses()
    await coordinator.get_vm()

    assert "vm-3" in coordinator.ds["vm"]


async def test_memory_is_not_converted_twice(coordinator) -> None:
    """A failed query must not re-divide an already converted memory value."""
    coordinator._methods = {"virt.instance.query"}
    coordinator.api.query.side_effect = _responses(
        **{"virt.instance.query": [VIRT_INSTANCE]}
    )
    await coordinator.get_vm()
    assert coordinator.ds["vm"]["incus-vm"]["memory"] == 8

    coordinator.api.query.side_effect = _responses()
    await coordinator.get_vm()

    assert coordinator.ds["vm"]["incus-vm"]["memory"] == 8


async def test_removed_vm_is_dropped(coordinator) -> None:
    """A VM deleted on the NAS leaves the collection."""
    coordinator._methods = {"vm.query"}
    coordinator.api.query.side_effect = _responses(**{"vm.query": [LEGACY_VM]})
    await coordinator.get_vm()
    assert "vm-3" in coordinator.ds["vm"]

    coordinator.api.query.side_effect = _responses(**{"vm.query": []})
    await coordinator.get_vm()

    assert coordinator.ds["vm"] == {}


CONTAINER = {
    "id": 7,
    "uuid": "abcd",
    "name": "jellyfin",
    "description": "media",
    "autostart": True,
    "dataset": "tank/containers/jellyfin",
    "status": {"state": "RUNNING", "pid": 4242, "domain_state": "running"},
}


async def test_containers_are_discovered(coordinator) -> None:
    """TrueNAS 26 exposes LXC containers under container.query."""
    coordinator._methods = {"vm.query", "container.query"}
    coordinator.api.query.side_effect = _responses(
        **{"vm.query": [LEGACY_VM], "container.query": [CONTAINER]}
    )

    await coordinator.get_vm()

    container = coordinator.ds["vm"]["container-7"]
    assert container["name"] == "jellyfin"
    assert container["type"] == "CONTAINER"
    assert container["running"] is True
    assert container["api"] == "container"
    # A container has no allocation of its own.
    assert container["cpu"] == 0
    assert container["memory"] == 0
    # The libvirt VM is still there alongside it.
    assert "vm-3" in coordinator.ds["vm"]


async def test_container_is_not_queried_before_truenas_26(coordinator) -> None:
    """A NAS without container.query is not asked for containers."""
    coordinator._methods = {"virt.instance.query"}
    coordinator.api.query.side_effect = _responses(
        **{"virt.instance.query": [VIRT_INSTANCE]}
    )

    await coordinator.get_vm()

    assert [c.args[0] for c in coordinator.api.query.call_args_list] == [
        "virt.instance.query"
    ]


@pytest.mark.parametrize(
    ("api", "get_method", "start_method", "stop_method"),
    [
        (
            "virt",
            "virt.instance.get_instance",
            "virt.instance.start",
            "virt.instance.stop",
        ),
        ("vm", "vm.get_instance", "vm.start", "vm.stop"),
        ("container", "container.get_instance", "container.start", "container.stop"),
    ],
)
async def test_actions_reach_the_right_api(
    api, get_method, start_method, stop_method
) -> None:
    """Each machine is driven through the API it came from."""
    from unittest.mock import AsyncMock as _AsyncMock

    from custom_components.truenas.binary_sensor import TrueNASVMBinarySensor

    sensor = TrueNASVMBinarySensor.__new__(TrueNASVMBinarySensor)
    sensor._data = {"id": 1, "name": "guest", "api": api}
    sensor.coordinator = MagicMock()

    nested = api != "virt"
    stopped = {"status": {"state": "STOPPED"}} if nested else {"status": "STOPPED"}
    sensor.coordinator.api.query = _AsyncMock(
        side_effect=lambda method, params=None: (
            stopped if method == get_method else None
        )
    )
    await sensor.start()
    calls = [c.args for c in sensor.coordinator.api.query.call_args_list]
    assert calls[0][0] == get_method
    assert calls[1][0] == start_method

    running = {"status": {"state": "RUNNING"}} if nested else {"status": "RUNNING"}
    sensor.coordinator.api.query = _AsyncMock(
        side_effect=lambda method, params=None: (
            running if method == get_method else None
        )
    )
    await sensor.stop()
    calls = [c.args for c in sensor.coordinator.api.query.call_args_list]
    assert calls[1][0] == stop_method


async def test_overcommit_only_goes_to_the_legacy_api() -> None:
    """Only vm.start understands memory overcommitment."""
    from unittest.mock import AsyncMock as _AsyncMock

    from custom_components.truenas.binary_sensor import TrueNASVMBinarySensor

    for api, expected in (("vm", [1, {"overcommit": True}]), ("container", [1])):
        sensor = TrueNASVMBinarySensor.__new__(TrueNASVMBinarySensor)
        sensor._data = {"id": 1, "name": "guest", "api": api}
        sensor.coordinator = MagicMock()
        sensor.coordinator.api.query = _AsyncMock(
            side_effect=lambda method, params=None: (
                {"status": {"state": "STOPPED"}} if "get_instance" in method else None
            )
        )

        await sensor.start(overcommit=True)

        assert sensor.coordinator.api.query.call_args_list[1].args[1] == expected
