"""TrueNAS Controller."""

from __future__ import annotations

import logging

from datetime import datetime, timedelta
from typing import Any

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
from homeassistant.util.dt import utc_from_timestamp

from .apiparser import parse_api
from .const import (
    DEFAULT_SSL,
    DOMAIN,
    LEGACY_UPDATE_CHECK,
    LEGACY_UPDATE_RUN,
    SYSTEMSTATS_RETRY_AFTER,
    UPDATE_RUN,
    UPDATE_STATUS,
    VM_API_CONTAINER,
    VM_API_LEGACY,
    VM_API_VIRT,
    VM_QUERY_CONTAINER,
    VM_QUERY_LEGACY,
    VM_QUERY_VIRT,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------
#   _graph_key
# ---------------------------
def _graph_key(graph: dict) -> tuple[str, str | None]:
    """Return the identity of a reporting graph.

    The interface graphs are requested once per identifier, so the name alone
    does not identify them.
    """
    return graph.get("name", ""), graph.get("identifier")


# ---------------------------
#   _rename_traffic
# ---------------------------
def _rename_traffic(name: str) -> str:
    """Map the netdata traffic legend onto the rx/tx attribute names."""
    return name.replace("received", "rx").replace("sent", "tx")


# ---------------------------
#   TrueNASControllerData
# ---------------------------
class TrueNASCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """TrueNASCoordinator Class."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        """Initialize TrueNASCoordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
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

        self._systemstats_errored: dict[tuple[str, str | None], int] = {}
        self.datasets_hass_device_id = None
        self.last_updatecheck_update = datetime(1970, 1, 1)

        self._is_virtual = False
        self._methods: set[str] | None = None

    # ---------------------------
    #   supports
    # ---------------------------
    def supports(self, method: str) -> bool:
        """Return whether the NAS exposes an API method.

        Fails open: when the method list could not be retrieved the call is
        attempted anyway, so a NAS that does not implement core.get_methods
        keeps behaving as before.
        """
        return self._methods is None or method in self._methods

    # ---------------------------
    #   get_methods
    # ---------------------------
    async def get_methods(self) -> None:
        """Learn which API methods this TrueNAS exposes."""
        result = await self.api.query("core.get_methods")
        if not isinstance(result, (dict, list)):
            _LOGGER.debug(
                "TrueNAS %s did not report its API methods, calling everything",
                self.host,
            )
            return

        self._methods = set(result)
        _LOGGER.debug(
            "TrueNAS %s exposes %s API methods", self.host, len(self._methods)
        )

    # ---------------------------
    #   _query_collection
    # ---------------------------
    async def _query_collection(self, method: str, **kwargs) -> dict | None:
        """Fetch a keyed collection and rebuild it from scratch.

        Returns None when the query failed, so that the caller keeps the data
        it already has instead of dropping every object. An empty result is a
        real answer and does clear the collection.
        """
        source = await self.api.query(method)
        if source is None:
            return None

        return parse_api(data={}, source=source, **kwargs)

    # ---------------------------
    #   async_close
    # ---------------------------
    async def async_close(self) -> None:
        """Close the connection to TrueNAS."""
        await self.api.disconnect()

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
            self._methods = None
            if not await self.api.connect() and self.api.error == "invalid_key":
                raise ConfigEntryAuthFailed(
                    "TrueNAS rejected the API key, it may have been revoked"
                )

        if self.api.connected() and self._methods is None:
            await self.get_methods()

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
                {"name": "uptimeEpoch", "default": None},
                {"name": "cpu_temperature", "default": 0.0},
                {"name": "load_shortterm", "default": 0.0},
                {"name": "load_midterm", "default": 0.0},
                {"name": "load_longterm", "default": 0.0},
                {"name": "cpu_usage", "default": 0.0},
                {"name": "cache_size-arc_value", "default": 0.0},
                {"name": "memory-used_value", "default": 0.0},
                {"name": "memory-free_value", "default": 0.0},
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

        interface = await self._query_collection(
            "interface.query",
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
        if interface is None:
            return

        self.ds["interface"] = interface

    # ---------------------------
    #   get_updatecheck
    # ---------------------------
    async def get_updatecheck(self) -> None:
        """Get the system update status from TrueNAS."""
        if self.supports(UPDATE_STATUS):
            await self._get_update_status()
        elif self.supports(LEGACY_UPDATE_CHECK):
            await self._get_update_check_available()
        else:
            return

        if not self.api.connected():
            return

        if self.ds["system_info"]["update_version"] == "unknown":
            self.ds["system_info"]["update_version"] = self.ds["system_info"]["version"]

    # ---------------------------
    #   _get_update_status
    # ---------------------------
    async def _get_update_status(self) -> None:
        """Read update.status, the update API of TrueNAS 25.04 and later."""
        self.ds["system_info"] = parse_api(
            data=self.ds["system_info"],
            source=await self.api.query(UPDATE_STATUS),
            vals=[
                {
                    "name": "update_status",
                    "source": "code",
                    "default": "unknown",
                },
                {
                    "name": "update_version",
                    "source": "status/new_version/version",
                    "default": "unknown",
                },
            ],
        )
        if not self.api.connected():
            return

        # update.status reports NORMAL/ERROR; an available update is signalled
        # by status.new_version being populated.
        self.ds["system_info"]["update_available"] = (
            self.ds["system_info"]["update_version"] != "unknown"
        )

    # ---------------------------
    #   _get_update_check_available
    # ---------------------------
    async def _get_update_check_available(self) -> None:
        """Read update.check_available, removed in TrueNAS 25.04."""
        self.ds["system_info"] = parse_api(
            data=self.ds["system_info"],
            source=await self.api.query(LEGACY_UPDATE_CHECK),
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

        self.ds["system_info"]["update_available"] = (
            self.ds["system_info"]["update_status"] == "AVAILABLE"
        )

    # ---------------------------
    #   system_update_method
    # ---------------------------
    def system_update_method(self) -> str:
        """Return the API method that installs a system update.

        TrueNAS 25.04 turned update.update into a method that changes the
        update *configuration*; installing an update moved to update.run.
        """
        return UPDATE_RUN if self.supports(UPDATE_RUN) else LEGACY_UPDATE_RUN

    # ---------------------------
    #   get_systemstats
    # ---------------------------
    async def get_systemstats(self) -> None:
        """Get system statistics."""
        if not self.supports("reporting.netdata_get_data"):
            return

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

        tmp_graphs = [tmp for tmp in tmp_graphs if not self._graph_is_muted(tmp)]

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
                    self._systemstats_errored[_graph_key(tmp)] = SYSTEMSTATS_RETRY_AFTER
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
                means = (tmp_graph[i].get("aggregations") or {}).get("mean") or {}
                readings = [
                    value
                    for value in means.values()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                ]
                self.ds["system_info"]["cpu_temperature"] = (
                    round(max(readings), 2) if readings else 0.0
                )

            # CPU load
            if tmp_graph[i]["name"] == "load":
                tmp_arr = ("shortterm", "midterm", "longterm")
                self._systemstats_process(tmp_arr, tmp_graph[i], "load")

            # CPU usage
            if tmp_graph[i]["name"] == "cpu":
                # The legend is ("cpu", "cpu0", "cpu1", ...): an overall value
                # and one per core. There is no per-mode breakdown to read.
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

                # netdata reports only the available memory for this graph,
                # so used is derived; cached and buffered are not obtainable.
                self.ds["system_info"]["memory-used_value"] = max(
                    self.ds["system_info"]["memory-total_value"]
                    - self.ds["system_info"]["memory-free_value"],
                    0,
                )

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
    #   _graph_is_muted
    # ---------------------------
    def _graph_is_muted(self, graph: dict) -> bool:
        """Return whether a graph is skipped for this cycle.

        A graph that fails is muted for a number of updates rather than for
        the lifetime of the integration: netdata restarting during a system
        update used to freeze a statistic at its last value until Home
        Assistant was restarted, which reads as a plausible but stale number.

        Muting is per identifier as well as per name, so one interface the
        NAS cannot report does not silence the traffic of every other one.
        """
        key = _graph_key(graph)
        remaining = self._systemstats_errored.get(key)
        if remaining is None:
            return False

        if remaining <= 1:
            del self._systemstats_errored[key]
            return False

        self._systemstats_errored[key] = remaining - 1
        return True

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
        service = await self._query_collection(
            "service.query",
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
        if service is None:
            return

        self.ds["service"] = service

        for uid, vals in self.ds["service"].items():
            self.ds["service"][uid]["running"] = vals["state"] == "RUNNING"

    # ---------------------------
    #   get_pool
    # ---------------------------
    async def get_pool(self) -> None:
        """Get pools from TrueNAS."""
        pools = await self.api.query("pool.query")
        boot = await self.api.query("boot.get_state")
        if pools is None or boot is None:
            # Rebuilding from half the sources would drop the other half.
            return

        self.ds["pool"] = parse_api(
            data={},
            source=pools,
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
            source=boot,
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

                self.ds["pool"][uid].pop("root_dataset", None)

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
        dataset = await self._query_collection(
            "pool.dataset.query",
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
        if dataset is None:
            return

        self.ds["dataset"] = dataset

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
        disk = await self._query_collection(
            "disk.query",
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
        if disk is None:
            return

        self.ds["disk"] = disk

        # Get disk temperatures
        if not self.supports("disk.temperatures"):
            return

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
        """Get VMs from TrueNAS.

        The API has moved twice: vm.* exists everywhere, TrueNAS 25.04 added
        the Incus based virt.instance.*, and TrueNAS 26 drops virt.* again in
        favour of container.* for LXC containers. Any of them can hold
        instances, so query whichever the NAS exposes and merge the results.
        """
        vms: dict = {}
        failed = False

        for supported, getter in (
            (self.supports(VM_QUERY_VIRT), self._get_virt_instances),
            (self.supports(VM_QUERY_LEGACY), self._get_legacy_vms),
            (self.supports(VM_QUERY_CONTAINER), self._get_containers),
        ):
            if not supported:
                continue

            parsed = await getter()
            if parsed is None:
                failed = True
            else:
                vms.update(parsed)

        if failed:
            # Keep the machines we cannot re-read rather than dropping them.
            self.ds["vm"].update(vms)
            return

        self.ds["vm"] = vms

    # ---------------------------
    #   _get_virt_instances
    # ---------------------------
    async def _get_virt_instances(self) -> dict | None:
        """Get Incus instances from TrueNAS."""
        vms = await self._query_collection(
            VM_QUERY_VIRT,
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
                {"name": "api", "default": VM_API_VIRT},
            ],
        )
        if vms is None:
            return None

        for vals in vms.values():
            # virt.instance.query reports memory in bytes
            vals["memory"] = round(vals["memory"] / 1024 / 1024 / 1024)
            vals["running"] = vals["status"] == "RUNNING"

        return vms

    # ---------------------------
    #   _get_legacy_vms
    # ---------------------------
    async def _get_legacy_vms(self) -> dict | None:
        """Get libvirt based VMs from TrueNAS."""
        legacy = await self._query_collection(
            VM_QUERY_LEGACY,
            key="id",
            vals=[
                {"name": "id", "default": 0},
                {"name": "name", "default": "unknown"},
                {"name": "cpu", "source": "vcpus", "default": 0},
                {"name": "memory", "default": 0},
                {"name": "autostart", "type": "bool", "default": False},
                {"name": "image", "source": "description", "default": "unknown"},
                {"name": "status", "source": "status/state", "default": "unknown"},
            ],
            ensure_vals=[
                {"name": "type", "default": "VM"},
                {"name": "running", "type": "bool", "default": False},
                {"name": "api", "default": VM_API_LEGACY},
            ],
        )
        if legacy is None:
            return None

        vms = {}
        for uid, vals in legacy.items():
            # vm.query reports memory in megabytes
            vals["memory"] = round(vals["memory"] / 1024)
            vals["running"] = vals["status"] == "RUNNING"
            vms[f"{VM_API_LEGACY}-{uid}"] = vals

        return vms

    # ---------------------------
    #   _get_containers
    # ---------------------------
    async def _get_containers(self) -> dict | None:
        """Get LXC containers from TrueNAS 26 and later."""
        containers = await self._query_collection(
            VM_QUERY_CONTAINER,
            key="id",
            vals=[
                {"name": "id", "default": 0},
                {"name": "name", "default": "unknown"},
                {"name": "autostart", "type": "bool", "default": False},
                {"name": "image", "source": "dataset", "default": "unknown"},
                {"name": "status", "source": "status/state", "default": "unknown"},
            ],
            ensure_vals=[
                # A container has no CPU or memory allocation of its own.
                {"name": "type", "default": "CONTAINER"},
                {"name": "cpu", "default": 0},
                {"name": "memory", "default": 0},
                {"name": "running", "type": "bool", "default": False},
                {"name": "api", "default": VM_API_CONTAINER},
            ],
        )
        if containers is None:
            return None

        vms = {}
        for uid, vals in containers.items():
            vals["running"] = vals["status"] == "RUNNING"
            vms[f"{VM_API_CONTAINER}-{uid}"] = vals

        return vms

    # ---------------------------
    #   get_cloudsync
    # ---------------------------
    async def get_cloudsync(self) -> None:
        """Get cloudsync from TrueNAS."""
        if not self.supports("cloudsync.query"):
            return

        cloudsync = await self._query_collection(
            "cloudsync.query",
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
        if cloudsync is None:
            return

        self.ds["cloudsync"] = cloudsync

    # ---------------------------
    #   get_replication
    # ---------------------------
    async def get_replication(self) -> None:
        """Get replication from TrueNAS."""
        if not self.supports("replication.query"):
            return

        replication = await self._query_collection(
            "replication.query",
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
        if replication is None:
            return

        self.ds["replication"] = replication

    # ---------------------------
    #   get_snapshottask
    # ---------------------------
    async def get_snapshottask(self) -> None:
        """Get periodic snapshot tasks from TrueNAS."""
        if not self.supports("pool.snapshottask.query"):
            return

        snapshottask = await self._query_collection(
            "pool.snapshottask.query",
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
        if snapshottask is None:
            return

        self.ds["snapshottask"] = snapshottask

    # ---------------------------
    #   get_app
    # ---------------------------
    async def get_app(self) -> None:
        """Get Apps from TrueNAS."""
        if not self.supports("app.query"):
            return

        app = await self._query_collection(
            "app.query",
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
        if app is None:
            return

        self.ds["app"] = app

        for uid, vals in self.ds["app"].items():
            self.ds["app"][uid]["running"] = vals["state"] == "RUNNING"
