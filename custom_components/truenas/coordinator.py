"""TrueNAS Controller."""

from __future__ import annotations

import logging

from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from homeassistant.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_NAME,
    CONF_SSL,
    CONF_VERIFY_SSL,
)

from .api import TrueNASAPI
from .apiparser import parse_api, utc_from_timestamp
from .const import DEFAULT_SSL, DOMAIN

_LOGGER = logging.getLogger(__name__)


# ---------------------------
#   _rename_traffic
# ---------------------------
def _rename_traffic(name: str) -> str:
    """Map the netdata traffic legend onto the rx/tx attribute names."""
    return name.replace("received", "rx").replace("sent", "tx")


# ---------------------------
#   TrueNASControllerData
# ---------------------------
class TrueNASCoordinator(DataUpdateCoordinator[None]):
    """TrueNASCoordinator Class."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        """Initialize TrueNASCoordinator."""
        self.hass = hass
        self.config_entry: ConfigEntry = config_entry

        super().__init__(
            self.hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=60),
        )

        self.name = config_entry.data[CONF_NAME]
        self.host = config_entry.data[CONF_HOST]

        self.ds = {
            "interface": {},
            "disk": {},
            "pool": {},
            "dataset": {},
            "system_info": {},
            "service": {},
            "vm": {},
            "cloudsync": {},
            "replication": {},
            "snapshottask": {},
            "app": {},
        }

        self.api = TrueNASAPI(
            config_entry.data[CONF_HOST],
            config_entry.data[CONF_API_KEY],
            config_entry.data[CONF_VERIFY_SSL],
            config_entry.data.get(CONF_SSL, DEFAULT_SSL),
        )

        self._systemstats_errored = []
        self.datasets_hass_device_id = None
        self.last_updatecheck_update = datetime(1970, 1, 1)

        self._is_virtual = False

    # ---------------------------
    #   connected
    # ---------------------------
    def connected(self) -> bool:
        """Return connected state."""
        return self.api.connected()

    # ---------------------------
    #   _async_update_data
    # ---------------------------
    async def _async_update_data(self):
        """Update TrueNAS data."""
        if not self.api.connected():
            if not await self.api.connect() and self.api.error == "invalid_key":
                raise ConfigEntryAuthFailed(
                    "TrueNAS rejected the API key, it may have been revoked"
                )

        jobs = [
            self.get_systeminfo,
            self.get_systemstats,
            self.get_service,
            self.get_disk,
            self.get_dataset,
            self.get_pool,
            self.get_vm,
            self.get_cloudsync,
            self.get_replication,
            self.get_snapshottask,
            self.get_app,
        ]

        for job in jobs:
            if self.api.connected():
                await job()

        delta = datetime.now().replace(microsecond=0) - self.last_updatecheck_update
        if self.api.connected() and delta.total_seconds() > 60 * 60 * 12:
            await self.get_updatecheck()
            self.last_updatecheck_update = datetime.now().replace(microsecond=0)

        if not self.api.connected():
            if self.api.error == "invalid_key":
                raise ConfigEntryAuthFailed(
                    "TrueNAS rejected the API key, it may have been revoked"
                )

            raise UpdateFailed("TrueNas Disconnected")

        return self.ds

    # ---------------------------
    #   get_systeminfo
    # ---------------------------
    async def get_systeminfo(self) -> None:
        """Get system info from TrueNAS."""
        self.ds["system_info"] = parse_api(
            data=self.ds["system_info"],
            source=await self.api.query("system.info"),
            vals=[
                {"name": "version", "default": "unknown"},
                {"name": "hostname", "default": "unknown"},
                {"name": "uptime_seconds", "default": 0},
                {"name": "system_serial", "default": "unknown"},
                {"name": "system_product", "default": "unknown"},
                {"name": "system_manufacturer", "default": "unknown"},
                {"name": "physmem", "default": 0},
            ],
            ensure_vals=[
                {"name": "uptimeEpoch", "default": 0},
                {"name": "cpu_temperature", "default": 0.0},
                {"name": "load_shortterm", "default": 0.0},
                {"name": "load_midterm", "default": 0.0},
                {"name": "load_longterm", "default": 0.0},
                {"name": "cpu_interrupt", "default": 0.0},
                {"name": "cpu_system", "default": 0.0},
                {"name": "cpu_user", "default": 0.0},
                {"name": "cpu_nice", "default": 0.0},
                {"name": "cpu_idle", "default": 0.0},
                {"name": "cpu_usage", "default": 0.0},
                {"name": "cache_size-arc_value", "default": 0.0},
                {"name": "memory-used_value", "default": 0.0},
                {"name": "memory-free_value", "default": 0.0},
                {"name": "memory-cached_value", "default": 0.0},
                {"name": "memory-buffered_value", "default": 0.0},
                {"name": "memory-total_value", "default": 0.0},
                {"name": "memory-usage_percent", "default": 0},
                {"name": "update_available", "type": "bool", "default": False},
                {"name": "update_progress", "default": 0},
                {"name": "update_jobid", "default": 0},
                {"name": "update_state", "default": "unknown"},
            ],
        )
        if not self.api.connected():
            return

        if not self.ds["system_info"]["update_available"]:
            self.ds["system_info"]["update_version"] = self.ds["system_info"]["version"]

        if self.ds["system_info"]["update_jobid"]:
            self.ds["system_info"] = parse_api(
                data=self.ds["system_info"],
                source=await self.api.query(
                    "core.get_jobs",
                    params=[[["id", "=", self.ds["system_info"]["update_jobid"]]]],
                ),
                vals=[
                    {
                        "name": "update_progress",
                        "source": "progress/percent",
                        "default": 0,
                    },
                    {
                        "name": "update_state",
                        "source": "state",
                        "default": "unknown",
                    },
                ],
            )
            if not self.api.connected():
                return

            if (
                self.ds["system_info"]["update_state"] != "RUNNING"
                or not self.ds["system_info"]["update_available"]
            ):
                self.ds["system_info"]["update_progress"] = 0
                self.ds["system_info"]["update_jobid"] = 0
                self.ds["system_info"]["update_state"] = "unknown"

        self._is_virtual = self.ds["system_info"]["system_manufacturer"] in [
            "QEMU",
            "VMware, Inc.",
            "Microsoft Corporation",
            "Xen",
        ] or self.ds["system_info"]["system_product"] in [
            "VirtualBox",
            "Virtual Machine",
        ]

        if self.ds["system_info"]["uptime_seconds"] > 0:
            now = datetime.now().replace(microsecond=0)
            uptime_tm = datetime.timestamp(
                now - timedelta(seconds=int(self.ds["system_info"]["uptime_seconds"]))
            )
            self.ds["system_info"]["uptimeEpoch"] = utc_from_timestamp(uptime_tm)

        self.ds["interface"] = parse_api(
            data=self.ds["interface"],
            source=await self.api.query("interface.query"),
            key="id",
            vals=[
                {"name": "id", "default": "unknown"},
                {"name": "name", "default": "unknown"},
                {"name": "description", "default": "unknown"},
                {"name": "mtu", "default": "unknown"},
                {
                    "name": "link_state",
                    "source": "state/link_state",
                    "default": "unknown",
                },
                {
                    "name": "active_media_type",
                    "source": "state/active_media_type",
                    "default": "unknown",
                },
                {
                    "name": "active_media_subtype",
                    "source": "state/active_media_subtype",
                    "default": "unknown",
                },
                {
                    "name": "link_address",
                    "source": "state/link_address",
                    "default": "unknown",
                },
            ],
            ensure_vals=[
                {"name": "rx", "default": 0},
                {"name": "tx", "default": 0},
            ],
        )

    # ---------------------------
    #   get_updatecheck
    # ---------------------------
    async def get_updatecheck(self) -> None:
        self.ds["system_info"] = parse_api(
            data=self.ds["system_info"],
            source=await self.api.query("update.check_available"),
            vals=[
                {
                    "name": "update_status",
                    "source": "status",
                    "default": "unknown",
                },
                {
                    "name": "update_version",
                    "source": "version",
                    "default": "unknown",
                },
            ],
        )

        if not self.api.connected():
            return

        if (
            self.ds["system_info"]["update_version"] == "unknown"
            and self.ds["system_info"]["version"]
        ):
            self.ds["system_info"]["update_version"] = self.ds["system_info"]["version"]

        self.ds["system_info"]["update_available"] = (
            self.ds["system_info"]["update_status"] == "AVAILABLE"
        )

    # ---------------------------
    #   get_systemstats
    # ---------------------------
    async def get_systemstats(self) -> None:
        """Get system statistics."""
        tmp_graphs = [
            {"name": "load"},
            {"name": "cputemp"},
            {"name": "cpu"},
            {"name": "arcsize"},
            {"name": "memory"},
        ]

        for uid in self.ds["interface"]:
            tmp_graphs.append({"name": "interface", "identifier": uid})

        if self._is_virtual:
            tmp_graphs = [tmp for tmp in tmp_graphs if tmp["name"] != "cputemp"]

        tmp_graphs = [
            tmp for tmp in tmp_graphs if tmp["name"] not in self._systemstats_errored
        ]

        if not tmp_graphs:
            return

        tmp_graph = await self._query_graphs(tmp_graphs)
        if tmp_graph is None:
            if not self.api.connected():
                return

            # Retry every graph on its own, so that a single graph the NAS
            # cannot report does not cost us all the other statistics.
            tmp_graph = []
            failed = []
            for tmp in tmp_graphs:
                tmp2 = await self._query_graphs([tmp])
                if tmp2 is None:
                    if not self.api.connected():
                        return

                    failed.append(tmp["name"])
                    self._systemstats_errored.append(tmp["name"])
                else:
                    tmp_graph.extend(tmp2)

            if failed:
                _LOGGER.warning(
                    "TrueNAS %s fetching following graphs failed, check your NAS: %s",
                    self.host,
                    failed,
                )

        for i in range(len(tmp_graph)):
            if "name" not in tmp_graph[i]:
                continue

            # CPU temperature
            if tmp_graph[i]["name"] == "cputemp":
                if "aggregations" in tmp_graph[i]:
                    self.ds["system_info"]["cpu_temperature"] = round(
                        max(tmp_graph[i]["aggregations"]["mean"].values()), 2
                    )
                else:
                    self.ds["system_info"]["cpu_temperature"] = 0.0

            # CPU load
            if tmp_graph[i]["name"] == "load":
                tmp_arr = ("shortterm", "midterm", "longterm")
                self._systemstats_process(tmp_arr, tmp_graph[i], "load")

            # CPU usage
            if tmp_graph[i]["name"] == "cpu":
                tmp_arr = ("cpu",)
                self._systemstats_process(tmp_arr, tmp_graph[i], "cpu")
                self.ds["system_info"]["cpu_usage"] = round(
                    self.ds["system_info"].get("cpu_cpu", 0.0), 2
                )

            # Interface
            if tmp_graph[i]["name"] == "interface":
                tmp_etc = tmp_graph[i]["identifier"]
                if tmp_etc in self.ds["interface"]:
                    tmp_arr = ("rx", "tx")
                    tmp_mean = (tmp_graph[i].get("aggregations") or {}).get("mean")
                    if tmp_mean:
                        legend = [
                            _rename_traffic(tmp)
                            for tmp in tmp_graph[i].get("legend") or []
                        ]
                        tmp_mean = {_rename_traffic(k): v for k, v in tmp_mean.items()}

                        for tmp_var in legend:
                            if tmp_var in tmp_arr:
                                tmp_val = tmp_mean.get(tmp_var) or 0.0
                                self.ds["interface"][tmp_etc][tmp_var] = round(
                                    (tmp_val * 0.12207), 2
                                )

                    else:
                        for tmp_load in tmp_arr:
                            self.ds["interface"][tmp_etc][tmp_load] = 0.0

            # memory
            if tmp_graph[i]["name"] == "memory":
                tmp_arr = ("available",)
                self.ds["system_info"]["memory-total_value"] = round(
                    self.ds["system_info"].get("physmem", 0)
                )

                self._systemstats_process(tmp_arr, tmp_graph[i], "memory")
                if self.ds["system_info"]["memory-total_value"] > 0:
                    self.ds["system_info"]["memory-usage_percent"] = round(
                        100
                        * (
                            float(self.ds["system_info"]["memory-total_value"])
                            - float(self.ds["system_info"]["memory-free_value"])
                        )
                        / float(self.ds["system_info"]["memory-total_value"])
                    )

            # arcsize
            if tmp_graph[i]["name"] == "arcsize":
                # netdata names this dimension "size"; the graph itself is
                # called arcsize.
                tmp_arr = ("size", "arc_size")
                self._systemstats_process(tmp_arr, tmp_graph[i], "arcsize")

    # ---------------------------
    #   _query_graphs
    # ---------------------------
    async def _query_graphs(self, graphs: list) -> list | None:
        """Query netdata for a set of graphs, None when the call failed."""
        report_epoch = int(datetime.now().replace(microsecond=0).timestamp())
        tmp_graph = await self.api.query(
            "reporting.netdata_get_data",
            params=[
                graphs,
                {
                    "start": report_epoch - 30,
                    "end": report_epoch - 90,
                    "aggregate": True,
                },
            ],
        )

        return tmp_graph if isinstance(tmp_graph, list) else None

    # ---------------------------
    #   _systemstats_process
    # ---------------------------
    def _systemstats_process(self, arr, graph, t) -> None:
        """Store the aggregated values of a graph in system_info."""
        if "aggregations" in graph:
            means = graph["aggregations"]["mean"]
            values = {
                name: (means[name] or 0.0)
                for name in graph.get("legend", [])
                if name in arr and name in means
            }
        else:
            values = {name: 0.0 for name in arr}

        for name, value in values.items():
            if t == "memory":
                if name == "available":
                    self.ds["system_info"]["memory-free_value"] = round(value)
            elif t == "cpu":
                self.ds["system_info"][f"cpu_{name}"] = round(value, 2)
            elif t == "load":
                self.ds["system_info"][f"load_{name}"] = round(value, 2)
            elif t == "arcsize":
                self.ds["system_info"]["cache_size-arc_value"] = round(value, 2)
            else:
                self.ds["system_info"][name] = round(value, 2)

    # ---------------------------
    #   get_service
    # ---------------------------
    async def get_service(self) -> None:
        """Get service info from TrueNAS."""
        self.ds["service"] = parse_api(
            data=self.ds["service"],
            source=await self.api.query("service.query"),
            key="id",
            vals=[
                {"name": "id", "default": 0},
                {"name": "service", "default": "unknown"},
                {"name": "enable", "type": "bool", "default": False},
                {"name": "state", "default": "unknown"},
            ],
            ensure_vals=[
                {"name": "running", "type": "bool", "default": False},
            ],
        )

        for uid, vals in self.ds["service"].items():
            self.ds["service"][uid]["running"] = vals["state"] == "RUNNING"

    # ---------------------------
    #   get_pool
    # ---------------------------
    async def get_pool(self) -> None:
        """Get pools from TrueNAS."""
        self.ds["pool"] = parse_api(
            data=self.ds["pool"],
            source=await self.api.query("pool.query"),
            key="guid",
            vals=[
                {"name": "guid", "default": 0},
                {"name": "id", "default": 0},
                {"name": "name", "default": "unknown"},
                {"name": "path", "default": "unknown"},
                {"name": "status", "default": "unknown"},
                {"name": "healthy", "type": "bool", "default": False},
                {"name": "is_decrypted", "type": "bool", "default": False},
                {
                    "name": "autotrim",
                    "source": "autotrim/parsed",
                    "type": "bool",
                    "default": False,
                },
                {
                    "name": "scan_function",
                    "source": "scan/function",
                    "default": "unknown",
                },
                {"name": "scrub_state", "source": "scan/state", "default": "unknown"},
                {
                    "name": "scrub_start",
                    "source": "scan/start_time/$date",
                    "default": 0,
                    "convert": "utc_from_timestamp",
                },
                {
                    "name": "scrub_end",
                    "source": "scan/end_time/$date",
                    "default": 0,
                    "convert": "utc_from_timestamp",
                },
                {
                    "name": "scrub_secs_left",
                    "source": "scan/total_secs_left",
                    "default": 0,
                },
            ],
            ensure_vals=[
                {"name": "available", "default": 0.0},
                {"name": "total", "default": 0.0},
                {"name": "usage", "default": 0.0},
            ],
        )

        self.ds["pool"] = parse_api(
            data=self.ds["pool"],
            source=await self.api.query("boot.get_state"),
            key="name",
            vals=[
                {"name": "guid", "default": "boot-pool"},
                {"name": "id", "default": "boot-pool"},
                {"name": "name", "default": "unknown"},
                {"name": "path", "default": "unknown"},
                {"name": "status", "default": "unknown"},
                {"name": "healthy", "type": "bool", "default": False},
                {"name": "is_decrypted", "type": "bool", "default": False},
                {
                    "name": "autotrim",
                    "source": "autotrim/parsed",
                    "type": "bool",
                    "default": False,
                },
                {"name": "root_dataset"},
                {
                    "name": "root_dataset_available",
                    "source": "root_dataset/properties/available/parsed",
                    "default": 0,
                },
                {
                    "name": "root_dataset_used",
                    "source": "root_dataset/properties/used/parsed",
                    "default": 0,
                },
                {
                    "name": "scan_function",
                    "source": "scan/function",
                    "default": "unknown",
                },
                {"name": "scrub_state", "source": "scan/state", "default": "unknown"},
                {
                    "name": "scrub_start",
                    "source": "scan/start_time/$date",
                    "default": 0,
                    "convert": "utc_from_timestamp",
                },
                {
                    "name": "scrub_end",
                    "source": "scan/end_time/$date",
                    "default": 0,
                    "convert": "utc_from_timestamp",
                },
                {
                    "name": "scrub_secs_left",
                    "source": "scan/total_secs_left",
                    "default": 0,
                },
                {"name": "allocated", "default": 0},
                {"name": "free", "default": 0},
            ],
            ensure_vals=[
                {"name": "available", "default": 0.0},
                {"name": "total", "default": 0.0},
                {"name": "usage", "default": 0.0},
            ],
        )
        if not self.api.connected():
            return

        # Process pools
        tmp_dataset_available = {}
        tmp_dataset_total = {}
        for uid, vals in self.ds["dataset"].items():
            tmp_dataset_available[self.ds["dataset"][uid]["mountpoint"]] = vals[
                "available"
            ]

            tmp_dataset_total[self.ds["dataset"][uid]["mountpoint"]] = (
                vals["available"] + vals["used"]
            )

        for uid, vals in self.ds["pool"].items():
            if vals["path"] in tmp_dataset_available:
                self.ds["pool"][uid]["available"] = tmp_dataset_available[vals["path"]]

            if vals["path"] in tmp_dataset_total:
                self.ds["pool"][uid]["total"] = tmp_dataset_total[vals["path"]]

            if vals["name"] in ["boot-pool", "freenas-boot"]:
                self.ds["pool"][uid]["available"] = vals["free"]
                self.ds["pool"][uid]["total"] = vals["free"] + vals["allocated"]

                self.ds["pool"][uid].pop("root_dataset")

            if self.ds["pool"][uid]["total"] > 0:
                self.ds["pool"][uid]["usage"] = round(
                    (
                        (
                            self.ds["pool"][uid]["total"]
                            - self.ds["pool"][uid]["available"]
                        )
                        / self.ds["pool"][uid]["total"]
                    )
                    * 100
                )
            else:
                self.ds["pool"][uid]["usage"] = 0

    # ---------------------------
    #   get_dataset
    # ---------------------------
    async def get_dataset(self) -> None:
        """Get datasets from TrueNAS."""
        self.ds["dataset"] = parse_api(
            data={},
            source=await self.api.query("pool.dataset.query"),
            key="id",
            vals=[
                {"name": "id", "default": "unknown"},
                {"name": "type", "default": "unknown"},
                {"name": "name", "default": "unknown"},
                {"name": "pool", "default": "unknown"},
                {"name": "mountpoint", "default": "unknown"},
                {"name": "comments", "source": "comments/parsed", "default": ""},
                {
                    "name": "deduplication",
                    "source": "deduplication/parsed",
                    "type": "bool",
                    "default": False,
                },
                {
                    "name": "atime",
                    "source": "atime/parsed",
                    "type": "bool",
                    "default": False,
                },
                {
                    "name": "casesensitivity",
                    "source": "casesensitivity/parsed",
                    "default": "unknown",
                },
                {"name": "checksum", "source": "checksum/parsed", "default": "unknown"},
                {
                    "name": "exec",
                    "source": "exec/parsed",
                    "type": "bool",
                    "default": False,
                },
                {"name": "sync", "source": "sync/parsed", "default": "unknown"},
                {
                    "name": "compression",
                    "source": "compression/parsed",
                    "default": "unknown",
                },
                {
                    "name": "compressratio",
                    "source": "compressratio/parsed",
                    "default": "unknown",
                },
                {"name": "quota", "source": "quota/parsed", "default": "unknown"},
                {"name": "copies", "source": "copies/parsed", "default": 0},
                {
                    "name": "readonly",
                    "source": "readonly/parsed",
                    "type": "bool",
                    "default": False,
                },
                {"name": "recordsize", "source": "recordsize/parsed", "default": 0},
                {
                    "name": "encryption_algorithm",
                    "source": "encryption_algorithm/parsed",
                    "default": "unknown",
                },
                {"name": "used", "source": "used/parsed", "default": 0},
                {"name": "available", "source": "available/parsed", "default": 0},
            ],
        )

        if len(self.ds["dataset"]) == 0:
            return

        # entities_to_be_removed = []
        # if not self.datasets_hass_device_id:
        #     device_registry = dr.async_get(self.hass)
        #     for device in device_registry.devices.values():
        #         if (
        #             self.config_entry.entry_id in device.config_entries
        #             and device.name.endswith("Datasets")
        #         ):
        #             self.datasets_hass_device_id = device.id
        #             _LOGGER.debug(f"datasets device: {device.name}")
        #
        #     if not self.datasets_hass_device_id:
        #         return
        #
        # _LOGGER.debug(f"datasets_hass_device_id: {self.datasets_hass_device_id}")
        # entity_registry = er.async_get(self.hass)
        # entity_entries = async_entries_for_config_entry(
        #     entity_registry, self.config_entry.entry_id
        # )
        # for entity in entity_entries:
        #     if (
        #         entity.device_id == self.datasets_hass_device_id
        #         and entity.unique_id.removeprefix(f"{self.name.lower()}-dataset-")
        #         not in map(
        #             lambda x: str.replace(x, "/", "_"),
        #             map(str.lower, self.ds["dataset"].keys()),
        #         )
        #     ):
        #         _LOGGER.debug(f"dataset to be removed: {entity.unique_id}")
        #         entities_to_be_removed.append(entity.entity_id)
        #
        # for entity_id in entities_to_be_removed:
        #     entity_registry.async_remove(entity_id)

    # ---------------------------
    #   get_disk
    # ---------------------------
    async def get_disk(self) -> None:
        """Get disks from TrueNAS."""
        self.ds["disk"] = parse_api(
            data=self.ds["disk"],
            source=await self.api.query("disk.query"),
            key="identifier",
            vals=[
                {"name": "name", "default": "unknown"},
                {"name": "devname", "default": "unknown"},
                {"name": "serial", "default": "unknown"},
                {"name": "size", "default": "unknown"},
                {"name": "hddstandby", "default": "unknown"},
                {"name": "hddstandby_force", "type": "bool", "default": False},
                {"name": "advpowermgmt", "default": "unknown"},
                {"name": "acousticlevel", "default": "unknown"},
                {"name": "togglesmart", "type": "bool", "default": False},
                {"name": "model", "default": "unknown"},
                {"name": "rotationrate", "default": "unknown"},
                {"name": "type", "default": "unknown"},
                {"name": "zfs_guid", "default": "unknown"},
                {"name": "identifier", "default": "unknown"},
            ],
            ensure_vals=[
                {"name": "temperature", "default": 0},
            ],
        )

        # Get disk temperatures
        temps = await self.api.query(
            "disk.temperatures",
            params={},
        )

        if temps:
            for uid, vals in self.ds["disk"].items():
                if vals["name"] in temps:  # looks for devname here
                    self.ds["disk"][uid]["temperature"] = temps[vals["name"]]
                    # return devname temp to uid disk
                    # I feel like this will break in the future when TrueNAS updates to a more sensible system. Currently their own long term stats are broken by the changing devnames.

    # ---------------------------
    #   get_vm
    # ---------------------------
    async def get_vm(self) -> None:
        """Get VMs from TrueNAS."""
        self.ds["vm"] = parse_api(
            data=self.ds["vm"],
            source=await self.api.query("virt.instance.query"),
            key="id",
            vals=[
                {"name": "id", "default": 0},
                {"name": "name", "default": "unknown"},
                {"name": "type", "default": "unknown"},
                {"name": "cpu", "default": 0},
                {"name": "memory", "default": 0},
                {"name": "autostart", "type": "bool", "default": False},
                {"name": "image", "source": "image/description", "default": "unknown"},
                {"name": "status", "default": "unknown"},
            ],
            ensure_vals=[
                {"name": "running", "type": "bool", "default": False},
            ],
        )

        for uid, vals in self.ds["vm"].items():
            self.ds["vm"][uid]["memory"] = round(vals["memory"] / 1024 / 1024 / 1024)
            self.ds["vm"][uid]["running"] = vals["status"] == "RUNNING"

    # ---------------------------
    #   get_cloudsync
    # ---------------------------
    async def get_cloudsync(self) -> None:
        """Get cloudsync from TrueNAS."""
        self.ds["cloudsync"] = parse_api(
            data=self.ds["cloudsync"],
            source=await self.api.query("cloudsync.query"),
            key="id",
            vals=[
                {"name": "id", "default": "unknown"},
                {"name": "description", "default": "unknown"},
                {"name": "direction", "default": "unknown"},
                {"name": "path", "default": "unknown"},
                {"name": "enabled", "type": "bool", "default": False},
                {"name": "transfer_mode", "default": "unknown"},
                {"name": "snapshot", "type": "bool", "default": False},
                {"name": "state", "source": "job/state", "default": "unknown"},
                {
                    "name": "time_started",
                    "source": "job/time_started/$date",
                    "default": 0,
                    "convert": "utc_from_timestamp",
                },
                {
                    "name": "time_finished",
                    "source": "job/time_finished/$date",
                    "default": 0,
                    "convert": "utc_from_timestamp",
                },
                {"name": "job_percent", "source": "job/progress/percent", "default": 0},
                {
                    "name": "job_description",
                    "source": "job/progress/description",
                    "default": "unknown",
                },
            ],
        )

    # ---------------------------
    #   get_replication
    # ---------------------------
    async def get_replication(self) -> None:
        """Get replication from TrueNAS."""
        self.ds["replication"] = parse_api(
            data=self.ds["replication"],
            source=await self.api.query("replication.query"),
            key="id",
            vals=[
                {"name": "id", "default": 0},
                {"name": "name", "default": "unknown"},
                {"name": "source_datasets", "default": "unknown"},
                {"name": "target_dataset", "default": "unknown"},
                {"name": "recursive", "type": "bool", "default": False},
                {"name": "enabled", "type": "bool", "default": False},
                {"name": "direction", "default": "unknown"},
                {"name": "transport", "default": "unknown"},
                {"name": "auto", "type": "bool", "default": False},
                {"name": "retention_policy", "default": "unknown"},
                {"name": "state", "source": "job/state", "default": "unknown"},
                {
                    "name": "time_started",
                    "source": "job/time_started/$date",
                    "default": 0,
                    "convert": "utc_from_timestamp",
                },
                {
                    "name": "time_finished",
                    "source": "job/time_finished/$date",
                    "default": 0,
                    "convert": "utc_from_timestamp",
                },
                {"name": "job_percent", "source": "job/progress/percent", "default": 0},
                {
                    "name": "job_description",
                    "source": "job/progress/description",
                    "default": "unknown",
                },
            ],
        )

    # ---------------------------
    #   get_snapshottask
    # ---------------------------
    async def get_snapshottask(self) -> None:
        """Get replication from TrueNAS."""
        self.ds["snapshottask"] = parse_api(
            data=self.ds["snapshottask"],
            source=await self.api.query("pool.snapshottask.query"),
            key="id",
            vals=[
                {"name": "id", "default": 0},
                {"name": "dataset", "default": "unknown"},
                {"name": "recursive", "type": "bool", "default": False},
                {"name": "lifetime_value", "default": 0},
                {"name": "lifetime_unit", "default": "unknown"},
                {"name": "enabled", "type": "bool", "default": False},
                {"name": "naming_schema", "default": "unknown"},
                {"name": "allow_empty", "type": "bool", "default": False},
                {"name": "vmware_sync", "type": "bool", "default": False},
                {"name": "state", "source": "state/state", "default": "unknown"},
                {
                    "name": "datetime",
                    "source": "state/datetime/$date",
                    "default": 0,
                    "convert": "utc_from_timestamp",
                },
            ],
        )

    # ---------------------------
    #   get_app
    # ---------------------------
    async def get_app(self) -> None:
        """Get Apps from TrueNAS."""
        self.ds["app"] = parse_api(
            data=self.ds["app"],
            source=await self.api.query("app.query"),
            key="id",
            vals=[
                {"name": "id", "default": 0},
                {"name": "name", "default": "unknown"},
                {"name": "human_version", "default": "unknown"},
                {"name": "version", "default": "unknown"},
                {"name": "latest_version", "default": "unknown"},
                {"name": "custom_app", "type": "bool", "default": False},
                {
                    "name": "update_available",
                    "source": "upgrade_available",
                    "type": "bool",
                    "default": False,
                },
                {
                    "name": "image_updates_available",
                    "type": "bool",
                    "default": False,
                },
                {
                    "name": "portal",
                    "source": "portals/Web UI",
                    "default": "unknown",
                },
                {"name": "state", "default": "unknown"},
            ],
            ensure_vals=[
                {"name": "running", "type": "bool", "default": False},
            ],
        )

        for uid, vals in self.ds["app"].items():
            self.ds["app"][uid]["running"] = vals["state"] == "RUNNING"
