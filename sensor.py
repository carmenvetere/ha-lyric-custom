"""Support for Honeywell Lyric sensor platform."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from aiolyric import Lyric
from aiolyric.objects.device import LyricDevice
from aiolyric.objects.location import LyricLocation
from aiolyric.objects.priority import LyricAccessory, LyricRoom

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import LyricAccessoryEntity, LyricDeviceEntity
from .const import DOMAIN
from .schedule_sync import STATUSES, ScheduleSyncManager, is_schedulable, signal_update

_LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True, kw_only=True)
class LyricSensorEntityDescription(SensorEntityDescription):
    """Class describing Honeywell Lyric sensor entities."""

    value_fn: Callable[[LyricDevice], StateType | datetime]
    suitable_fn: Callable[[LyricDevice], bool]


@dataclass(frozen=True, kw_only=True)
class LyricSensorAccessoryEntityDescription(SensorEntityDescription):
    """Class describing Honeywell Lyric room sensor entities."""

    value_fn: Callable[[LyricRoom, LyricAccessory], StateType | datetime]
    suitable_fn: Callable[[LyricRoom, LyricAccessory], bool]


def get_ventilation_settings(device: LyricDevice) -> dict[str, Any] | None:
    """Return settings.ventilationModeSettings, present when a ventilator is wired in."""
    settings = device.settings.attributes.get("ventilationModeSettings")
    return settings if isinstance(settings, dict) else None


def get_ventilation_mode(device: LyricDevice) -> str | None:
    """Return the ventilation mode, e.g. 'Off' (the API reports it as a string)."""
    mode = (get_ventilation_settings(device) or {}).get("changeableValues")
    if isinstance(mode, dict):  # in case it's nested like the fan settings
        mode = mode.get("mode")
    return mode


def has_ventilation(device: LyricDevice) -> bool:
    """Return True if the thermostat reports ventilation settings."""
    return get_ventilation_settings(device) is not None


DEVICE_SENSORS: tuple[LyricSensorEntityDescription, ...] = (
    LyricSensorEntityDescription(
        key="indoor_temperature",
        translation_key="indoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.indoor_temperature,
        suitable_fn=lambda device: device.indoor_temperature is not None,
    ),
    LyricSensorEntityDescription(
        key="indoor_humidity",
        translation_key="indoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda device: device.indoor_humidity,
        suitable_fn=lambda device: device.indoor_humidity is not None,
    ),
    LyricSensorEntityDescription(
        key="outdoor_temperature",
        translation_key="outdoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.outdoor_temperature,
        suitable_fn=lambda device: device.outdoor_temperature is not None,
    ),
    LyricSensorEntityDescription(
        key="outdoor_humidity",
        translation_key="outdoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda device: device.displayed_outdoor_humidity,
        suitable_fn=lambda device: device.displayed_outdoor_humidity is not None,
    ),
    LyricSensorEntityDescription(
        key="ventilation_mode",
        translation_key="ventilation_mode",
        name="Ventilation mode",
        icon="mdi:air-filter",
        value_fn=get_ventilation_mode,
        suitable_fn=has_ventilation,
    ),
)

ACCESSORY_SENSORS: tuple[LyricSensorAccessoryEntityDescription, ...] = (
    LyricSensorAccessoryEntityDescription(
        key="room_temperature",
        translation_key="room_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda _, accessory: accessory.temperature,
        suitable_fn=lambda _, accessory: accessory.type == "IndoorAirSensor"
        and accessory.temperature is not None,
    ),
    LyricSensorAccessoryEntityDescription(
        key="room_humidity",
        translation_key="room_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda room, _: room.room_avg_humidity,
        suitable_fn=lambda room, _: room.room_avg_humidity is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Honeywell Lyric sensor platform based on a config entry."""
    coordinator: DataUpdateCoordinator[Lyric] = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        LyricSensor(
            coordinator,
            device_sensor,
            location,
            device,
        )
        for location in coordinator.data.locations
        for device in location.devices
        for device_sensor in DEVICE_SENSORS
        if device_sensor.suitable_fn(device)
    )

    async_add_entities(
        LyricAccessorySensor(
            coordinator, accessory_sensor, location, device, room, accessory
        )
        for location in coordinator.data.locations
        for device in location.devices
        for room in coordinator.data.rooms_dict.get(device.mac_id, {}).values()
        for accessory in room.accessories
        for accessory_sensor in ACCESSORY_SENSORS
        if accessory_sensor.suitable_fn(room, accessory)
    )

    manager: ScheduleSyncManager = coordinator.schedule_sync
    async_add_entities(
        LyricScheduleSyncSensor(coordinator, location, device, manager)
        for location in coordinator.data.locations
        for device in location.devices
        if is_schedulable(device)
    )


class LyricSensor(LyricDeviceEntity, SensorEntity):
    """Define a Honeywell Lyric sensor."""

    entity_description: LyricSensorEntityDescription

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[Lyric],
        description: LyricSensorEntityDescription,
        location: LyricLocation,
        device: LyricDevice,
    ) -> None:
        """Initialize."""
        super().__init__(
            coordinator,
            location,
            device,
            f"{device.mac_id}_{description.key}",
        )
        self.entity_description = description
        if description.device_class == SensorDeviceClass.TEMPERATURE:
            if device.units == "Fahrenheit":
                self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
            else:
                self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @property
    def native_value(self) -> StateType | datetime:
        """Return the state."""
        return self.entity_description.value_fn(self.device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        if self.entity_description.key == "ventilation_mode":
            settings = get_ventilation_settings(self.device) or {}
            return {
                "ventilating": settings.get("ventilationFanRequested"),
                "speed": settings.get("ventilationSpeed"),
                "timer": settings.get("ventilationTimer"),
                "boost_timer": settings.get("ventilationBoostTimer"),
                "core_timer": settings.get("ventilationCoreTimer"),
            }
        return {}


class LyricAccessorySensor(LyricAccessoryEntity, SensorEntity):
    """Define a Honeywell Lyric accessory sensor."""

    entity_description: LyricSensorAccessoryEntityDescription

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[Lyric],
        description: LyricSensorAccessoryEntityDescription,
        location: LyricLocation,
        parentDevice: LyricDevice,
        room: LyricRoom,
        accessory: LyricAccessory,
    ) -> None:
        """Initialize."""
        super().__init__(
            coordinator,
            location,
            parentDevice,
            room,
            accessory,
            f"{parentDevice.mac_id}_room{room.id}_acc{accessory.id}_{description.key}",
        )
        self.entity_description = description
        self._room = room
        if description.device_class == SensorDeviceClass.TEMPERATURE:
            if parentDevice.units == "Fahrenheit":
                self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
            else:
                self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @property
    def native_value(self) -> StateType | datetime:
        """Return the state."""
        return self.entity_description.value_fn(self._room, self.accessory)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        data = super().extra_state_attributes or {}
        data["room_name"] = self._room.room_name
        return data

class LyricScheduleSyncSensor(LyricDeviceEntity, SensorEntity):
    """Shows whether the thermostat's schedule matches its schedule helper.

    Attributes include the schedule as converted from the helper and the
    schedule last read from the thermostat, so the two can be compared.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUSES
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:calendar-check"
    _unrecorded_attributes = frozenset(
        {"schedule", "thermostat_schedule", "thermostat_schedule_raw", "warnings"}
    )

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[Lyric],
        location: LyricLocation,
        device: LyricDevice,
        manager: ScheduleSyncManager,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, location, device, f"{device.mac_id}_schedule_sync")
        self._attr_name = "Schedule sync"
        self._manager = manager

    @property
    def native_value(self) -> str:
        """Return the sync status."""
        return self._manager.state(self._mac_id).status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details about the last sync and both schedules."""
        state = self._manager.state(self._mac_id)
        return {
            "source": state.source,
            "error": state.error,
            "warnings": state.warnings,
            "last_synced": state.synced_at,
            "last_checked": state.last_checked,
            "schedule": state.schedule,
            "thermostat_schedule": state.thermostat_schedule,
            "thermostat_schedule_read_at": state.thermostat_read_at,
            "thermostat_schedule_error": state.thermostat_read_error,
            "thermostat_schedule_raw": state.thermostat_schedule_raw,
        }

    async def async_added_to_hass(self) -> None:
        """Update when the sync state changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self._manager.entry_id, self._mac_id),
                self.async_write_ha_state,
            )
        )
