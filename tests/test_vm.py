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
