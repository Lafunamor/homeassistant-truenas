"""TrueNAS HA shared entity model."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from logging import getLogger
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ATTRIBUTION, CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform as ep
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import (
    ATTRIBUTION,
    DOMAIN,
)
from .coordinator import TrueNASCoordinator
from .helper import format_attribute

_LOGGER = getLogger(__name__)


# ---------------------------
#   async_add_entities
# ---------------------------
async def async_add_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
    dispatcher: dict[str, Callable],
    descriptions: Sequence[Any],
    services: Sequence[tuple],
) -> None:
    """Add the entities of a platform and keep up with new ones."""
    platform = ep.async_get_current_platform()

    for service in services:
        platform.async_register_entity_service(service[0], service[1], service[2])

    coordinator: TrueNASCoordinator = config_entry.runtime_data
    known: set[str] = set()

    @callback
    def async_update_entities() -> None:
        """Create entities for objects that appeared since the last update."""
        entities: list[TrueNASEntity] = []
        for entity_description in descriptions:
            data = coordinator.data.get(entity_description.data_path) or {}
            if not entity_description.data_reference:
                if data.get(entity_description.data_attribute) is None:
                    continue

                uids: list[str | None] = [None]
            else:
                uids = list(data)

            for uid in uids:
                obj = dispatcher[entity_description.func](
                    coordinator, entity_description, uid, platform.domain
                )
                if obj.unique_id in known:
                    continue

                known.add(obj.unique_id)
                entities.append(obj)

        if entities:
            add_entities(entities)

    async_update_entities()
    config_entry.async_on_unload(coordinator.async_add_listener(async_update_entities))


# ---------------------------
#   TrueNASEntity
# ---------------------------
class TrueNASEntity(CoordinatorEntity[TrueNASCoordinator], Entity):
    """Define entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        entity_description,
        uid: str | None = None,
        platform_domain: str | None = None,
    ):
        """Initialize entity."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._inst = coordinator.config_entry.data[CONF_NAME]
        self._config_entry = self.coordinator.config_entry
        self._attr_extra_state_attributes = {ATTR_ATTRIBUTION: ATTRIBUTION}
        self._uid = uid
        self._data = self._current_data() or {}

        # Entities are also created from a coordinator callback, where there
        # is no current platform to ask.
        if platform_domain is None:
            platform_domain = ep.async_get_current_platform().domain

        dev_group = self.entity_description.ha_group
        if self.entity_description.ha_group.startswith("data__"):
            dev_group = self.entity_description.ha_group[6:]
            if dev_group in self._data:
                dev_group = self._data[dev_group]

        # An entity description without a name (the system update entity)
        # would otherwise produce a trailing separator, which is not a valid
        # entity id.
        object_id = "_".join(
            part
            for part in (
                slugify(self._inst.lower()),
                slugify(str(dev_group).lower()),
                slugify(str(self.name).lower()),
            )
            if part
        )
        self.entity_id = f"{platform_domain}.{object_id}"

    # ---------------------------
    #   _current_data
    # ---------------------------
    def _current_data(self) -> dict | None:
        """Return this entity's slice of the coordinator data, if it is there."""
        data = self.coordinator.data.get(self.entity_description.data_path)
        if data is None:
            return None

        if self._uid is None:
            return data

        return data.get(self._uid)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Keep the last known data when the object is gone from the NAS."""
        data = self._current_data()
        if data is not None:
            self._data = data

        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return whether the object still exists on the NAS."""
        return super().available and self._current_data() is not None

    @property
    def name(self) -> str:
        """Return the name for this entity."""
        if not self._uid:
            return f"{self.entity_description.name}"

        if self.entity_description.name:
            return f"{self._data[self.entity_description.data_name]} {self.entity_description.name}"

        return f"{self._data[self.entity_description.data_name]}"

    @property
    def unique_id(self) -> str:
        """Return a unique id for this entity."""
        if self._uid:
            return f"{self._inst.lower()}-{self.entity_description.key}-{slugify(str(self._data[self.entity_description.data_reference]).lower())}"
        else:
            return f"{self._inst.lower()}-{self.entity_description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return a description for device registry."""
        dev_connection = DOMAIN
        dev_connection_value = f"{self._inst}_{self.entity_description.ha_group}"
        dev_group = self.entity_description.ha_group
        if self.entity_description.ha_group == "System":
            dev_connection_value = (
                f"{self._inst}_{self.coordinator.data['system_info']['hostname']}"
            )

        if self.entity_description.ha_group.startswith("data__"):
            dev_group = self.entity_description.ha_group[6:]
            if dev_group in self._data:
                dev_group = self._data[dev_group]
                dev_connection_value = dev_group

        if self.entity_description.ha_connection:
            dev_connection = self.entity_description.ha_connection

        if self.entity_description.ha_connection_value:
            dev_connection_value = self.entity_description.ha_connection_value
            if dev_connection_value.startswith("data__"):
                field = dev_connection_value[6:]
                dev_connection_value = f"{self._inst}_{self._data.get(field, field)}"

        if self.entity_description.ha_group == "System":
            return DeviceInfo(
                connections={(dev_connection, f"{dev_connection_value}")},
                identifiers={(dev_connection, f"{dev_connection_value}")},
                name=dev_group,
                model=f"{self.coordinator.data['system_info']['system_product']}",
                manufacturer=f"{self.coordinator.data['system_info']['system_manufacturer']}",
                sw_version=f"{self.coordinator.data['system_info']['version']}",
                configuration_url=f"http://{self.coordinator.config_entry.data[CONF_HOST]}",
            )
        else:
            return DeviceInfo(
                connections={(dev_connection, f"{dev_connection_value}")},
                default_name=dev_group,
                default_model=f"{self.coordinator.data['system_info']['system_product']}",
                default_manufacturer=f"{self.coordinator.data['system_info']['system_manufacturer']}",
                via_device=(
                    DOMAIN,
                    f"{self._inst}_{self.coordinator.data['system_info']['hostname']}",
                ),
            )

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the state attributes."""
        attributes = super().extra_state_attributes
        for variable in self.entity_description.data_attributes_list:
            if variable in self._data:
                attributes[format_attribute(variable)] = self._data[variable]

        return attributes

    async def start(self):
        """Run function."""
        raise NotImplementedError()

    async def stop(self):
        """Stop function."""
        raise NotImplementedError()

    async def restart(self):
        """Restart function."""
        raise NotImplementedError()

    async def reload(self):
        """Reload function."""
        raise NotImplementedError()

    async def snapshot(self):
        """Snapshot function."""
        raise NotImplementedError()
