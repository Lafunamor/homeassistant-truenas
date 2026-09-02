"""End to end test of the integration against a canned TrueNAS API."""

from __future__ import annotations
from unittest.mock import MagicMock, patch
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.truenas.const import DOMAIN

SYSTEM_INFO = {
    "version": "TrueNAS-25.10.0",
    "hostname": "truenas",
    "uptime_seconds": 86400,
    "system_serial": "1234",
    "system_product": "Custom",
    "system_manufacturer": "ASUS",
    "physmem": 34359738368,
}
RESPONSES = {
    "system.info": SYSTEM_INFO,
    "interface.query": [
        {
            "id": "eno1",
            "name": "eno1",
            "description": "",
            "mtu": 1500,
            "state": {
                "link_state": "LINK_STATE_UP",
                "active_media_type": "Ethernet",
                "active_media_subtype": "1000baseT",
                "link_address": "aa:bb",
            },
        }
    ],
    "reporting.netdata_get_data": [
        {"name": "cpu", "legend": ["cpu"], "aggregations": {"mean": {"cpu": 7.5}}},
        {
            "name": "memory",
            "legend": ["available"],
            "aggregations": {"mean": {"available": 16000000000}},
        },
        {
            "name": "load",
            "legend": ["shortterm", "midterm", "longterm"],
            "aggregations": {
                "mean": {"shortterm": 0.5, "midterm": 0.4, "longterm": 0.3}
            },
        },
        {
            "name": "arcsize",
            "legend": ["arc_size"],
            "aggregations": {"mean": {"arc_size": 8000000000}},
        },
        {"name": "cputemp", "legend": ["0"], "aggregations": {"mean": {"0": 41.0}}},
        {
            "name": "interface",
            "identifier": "eno1",
            "legend": ["received", "sent"],
            "aggregations": {"mean": {"received": 1000.0, "sent": 2000.0}},
        },
    ],
    "service.query": [{"id": 1, "service": "ssh", "enable": True, "state": "RUNNING"}],
    "pool.query": [
        {
            "guid": "111",
            "id": 1,
            "name": "persistent",
            "path": "/mnt/persistent",
            "status": "ONLINE",
            "healthy": True,
            "is_decrypted": True,
            "autotrim": {"parsed": True},
            "scan": {"function": "SCRUB", "state": "FINISHED"},
        }
    ],
    "boot.get_state": {
        "name": "boot-pool",
        "path": "/",
        "status": "ONLINE",
        "healthy": True,
        "is_decrypted": True,
        "autotrim": {"parsed": False},
        "allocated": 10,
        "free": 90,
        "root_dataset": {
            "properties": {"available": {"parsed": 90}, "used": {"parsed": 10}}
        },
        "scan": {"function": "SCRUB", "state": "FINISHED"},
    },
    "pool.dataset.query": [
        {
            "id": "persistent",
            "type": "FILESYSTEM",
            "name": "persistent",
            "pool": "persistent",
            "mountpoint": "/mnt/persistent",
            "used": {"parsed": 100},
            "available": {"parsed": 900},
        }
    ],
    "disk.query": [
        {
            "identifier": "d1",
            "name": "sda",
            "devname": "sda",
            "serial": "S1",
            "size": 1000,
            "model": "WD",
            "type": "HDD",
            "zfs_guid": "g1",
        }
    ],
    "disk.temperatures": {"sda": 35},
    "virt.instance.query": [
        {
            "id": "vm1",
            "name": "vm1",
            "type": "VM",
            "cpu": 2,
            "memory": 4294967296,
            "autostart": True,
            "status": "RUNNING",
            "image": {"description": "debian"},
        }
    ],
    "cloudsync.query": [],
    "replication.query": [],
    "pool.snapshottask.query": [],
    "app.query": [
        {
            "id": "plex",
            "name": "plex",
            "human_version": "1.0",
            "version": "1.0",
            "latest_version": "1.1",
            "upgrade_available": True,
            "state": "RUNNING",
            "portals": {"Web UI": "http://x"},
        }
    ],
    "update.check_available": {"status": "AVAILABLE", "version": "TrueNAS-25.10.1"},
}


async def test_entities_are_created(hass) -> None:
    """Setting up the entry creates every entity with a usable state."""
    api = MagicMock()
    api.connected.return_value = True
    api.error = ""
    api.query.side_effect = lambda service, params=None: RESPONSES.get(service)
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
    with patch("custom_components.truenas.coordinator.TrueNASAPI", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    states = hass.states.async_all()
    entity_ids = {state.entity_id for state in states}

    assert "sensor.truenas_system_cpu_usage" in entity_ids
    assert "sensor.truenas_disks_sda" in entity_ids
    assert "binary_sensor.truenas_apps_plex" in entity_ids
    assert "binary_sensor.truenas_vms_vm1" in entity_ids
    # No trailing separator: the system update entity has no description name.
    assert "update.truenas_system" in entity_ids

    assert hass.states.get("sensor.truenas_system_cpu_usage").state == "7.5"
    assert hass.states.get("sensor.truenas_system_temperature").state == "41.0"
    assert hass.states.get("sensor.truenas_disks_sda").state == "35"
    assert hass.states.get("sensor.truenas_system_memory_usage").state == "53"

    unusable = [
        state.entity_id for state in states if state.state in ("unknown", "unavailable")
    ]
    assert not unusable
