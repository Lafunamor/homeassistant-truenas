"""TrueNAS binary sensor platform."""

from __future__ import annotations
from logging import getLogger

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .binary_sensor_types import (
    SENSOR_SERVICES,
    SENSOR_TYPES,
)
from .const import SERVICE_CONTROL, VM_API_LEGACY
from .entity import TrueNASEntity, async_add_entities

_LOGGER = getLogger(__name__)


# ---------------------------
#   async_setup_entry
# ---------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    _async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors for the TrueNAS component."""
    dispatcher = {
        "TrueNASBinarySensor": TrueNASBinarySensor,
        "TrueNASVMBinarySensor": TrueNASVMBinarySensor,
        "TrueNASServiceBinarySensor": TrueNASServiceBinarySensor,
        "TrueNASAppBinarySensor": TrueNASAppBinarySensor,
    }
    await async_add_entities(
        hass,
        config_entry,
        _async_add_entities,
        dispatcher,
        SENSOR_TYPES,
        SENSOR_SERVICES,
    )


# ---------------------------
#   TrueNASBinarySensor
# ---------------------------
class TrueNASBinarySensor(TrueNASEntity, BinarySensorEntity):
    """Define an TrueNAS Binary Sensor."""

    @property
    def is_on(self) -> bool:
        """Return true if device is on."""
        return self._data[self.entity_description.data_is_on]

    @property
    def icon(self) -> str | None:
        """Return the icon."""
        if self.entity_description.icon_enabled:
            if self._data[self.entity_description.data_is_on]:
                return self.entity_description.icon_enabled
            else:
                return self.entity_description.icon_disabled

        return None


# ---------------------------
#   TrueNASVMBinarySensor
# ---------------------------
class TrueNASVMBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS VM Binary Sensor."""

    @property
    def _is_legacy(self) -> bool:
        """Return whether this VM lives on the libvirt based vm.* API."""
        return self._data.get("api") == VM_API_LEGACY

    async def _async_state(self) -> str | None:
        """Return the current state of the VM, None when it cannot be read."""
        method = "vm.get_instance" if self._is_legacy else "virt.instance.get_instance"
        instance = await self.coordinator.api.query(method, [self._data["id"]])

        if not isinstance(instance, dict):
            _LOGGER.error("VM %s (%s) invalid", self._data["name"], self._data["id"])
            return None

        # vm.query nests the state, virt.instance.query reports it directly.
        status = instance.get("status")
        state = (status or {}).get("state") if self._is_legacy else status
        if not state:
            _LOGGER.error("VM %s (%s) invalid", self._data["name"], self._data["id"])
            return None

        return state

    async def start(self, overcommit: bool = False):
        """Start a VM."""
        state = await self._async_state()
        if state is None:
            return

        if state != "STOPPED":
            _LOGGER.warning(
                "VM %s (%s) is not down", self._data["name"], self._data["id"]
            )
            return

        if self._is_legacy:
            params = [self._data["id"], {"overcommit": overcommit}]
        else:
            params = [self._data["id"]]

        await self.coordinator.api.query(
            "vm.start" if self._is_legacy else "virt.instance.start",
            params,
        )

    async def stop(self):
        """Stop a VM."""
        state = await self._async_state()
        if state is None:
            return

        if state != "RUNNING":
            _LOGGER.warning(
                "VM %s (%s) is not up", self._data["name"], self._data["id"]
            )
            return

        if self._is_legacy:
            params = [self._data["id"], {"force_after_timeout": True}]
        else:
            params = [self._data["id"], {"timeout": 0, "force": True}]

        await self.coordinator.api.query(
            "vm.stop" if self._is_legacy else "virt.instance.stop",
            params,
        )


# ---------------------------
#   TrueNASServiceBinarySensor
# ---------------------------
class TrueNASServiceBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS Service Binary Sensor."""

    async def _async_control(self, verb: str, require_running: bool) -> None:
        """Run a service operation once the current state allows it."""
        service = await self.coordinator.api.query(
            "service.get_instance",
            [self._data["id"]],
        )

        if not isinstance(service, dict) or "state" not in service:
            _LOGGER.error(
                "Service %s (%s) invalid", self._data["service"], self._data["id"]
            )
            return

        stopped = service["state"] == "STOPPED"
        if require_running and stopped:
            _LOGGER.warning(
                "Service %s (%s) is not running",
                self._data["service"],
                self._data["id"],
            )
            return

        if not require_running and not stopped:
            _LOGGER.warning(
                "Service %s (%s) is not stopped",
                self._data["service"],
                self._data["id"],
            )
            return

        # TrueNAS 26 replaced service.start/stop/restart/reload with a single
        # service.control taking the operation as its first argument.
        if self.coordinator.supports(SERVICE_CONTROL):
            await self.coordinator.api.query(
                SERVICE_CONTROL,
                [verb, self._data["service"]],
            )
        else:
            await self.coordinator.api.query(
                f"service.{verb.lower()}",
                [self._data["service"]],
            )

        await self.coordinator.async_refresh()

    async def start(self):
        """Start a Service."""
        await self._async_control("START", require_running=False)

    async def stop(self):
        """Stop a Service."""
        await self._async_control("STOP", require_running=True)

    async def restart(self):
        """Restart a Service."""
        await self._async_control("RESTART", require_running=True)

    async def reload(self):
        """Reload a Service."""
        await self._async_control("RELOAD", require_running=True)


# ---------------------------
#   TrueNASAppsBinarySensor
# ---------------------------
class TrueNASAppBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS Applications Binary Sensor."""

    async def start(self):
        """Start an App."""
        tmp_app = await self.coordinator.api.query(
            "app.get_instance",
            [self._data["id"]],
        )

        if not isinstance(tmp_app, dict) or "state" not in tmp_app:
            _LOGGER.error("App %s (%s) invalid", self._data["name"], self._data["id"])
            return

        if tmp_app["state"] == "RUNNING":
            _LOGGER.warning(
                "App %s (%s) is not down", self._data["name"], self._data["id"]
            )
            return

        await self.coordinator.api.query(
            "app.start",
            [self._data["id"]],
        )

    async def stop(self):
        """Stop an App."""
        tmp_app = await self.coordinator.api.query(
            "app.get_instance",
            [self._data["id"]],
        )

        if not isinstance(tmp_app, dict) or "state" not in tmp_app:
            _LOGGER.error("App %s (%s) invalid", self._data["name"], self._data["id"])
            return

        if tmp_app["state"] != "RUNNING":
            _LOGGER.warning(
                "App %s (%s) is not up", self._data["name"], self._data["id"]
            )
            return

        await self.coordinator.api.query(
            "app.stop",
            [self._data["id"]],
        )
