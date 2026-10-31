"""Switch platform for Honeywell Lyric switches."""
from __future__ import annotations

import logging
from typing import Any

from aiolyric import Lyric
from aiolyric.objects.device import LyricDevice
from aiolyric.objects.location import LyricLocation
from aiolyric.objects.priority import LyricRoom

from aiohttp import ClientError

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import LyricDeviceEntity
from .const import (
    DOMAIN,
    LYRIC_EXCEPTIONS,
    LYRIC_HVAC_MODE_EMERGENCY_HEAT,
    PRESET_NO_HOLD,
)
from .schedule_sync import SyncError

_LOGGER = logging.getLogger(__name__)

PRIORITY_TYPE_PICKROOM = "PickARoom"
PRIORITY_TYPE_FOLLOWME = "FollowMe"
PRIORITY_TYPE_WHOLEHOUSE = "WholeHouse"

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Honeywell Lyric switch platform."""
    coordinator: DataUpdateCoordinator[Lyric] = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for location in coordinator.data.locations:
        for device in location.devices:
            if device.device_class == "Thermostat" and device.device_id.startswith("LCC"):
                # Add emergency heat switch
                _LOGGER.debug(
                    "Creating emergency heat switch for device %s (%s)", 
                    device.name,
                    device.device_id
                )
                entities.append(
                    LyricEmergencyHeatSwitch(
                        coordinator,
                        location,
                        device,
                    )
                )
                entities.append(LyricFollowScheduleSwitch(coordinator, location, device))

                # Add room priority switches
                rooms = coordinator.data.rooms_dict.get(device.mac_id, {})
                for room_id, room in rooms.items():
                    _LOGGER.debug(
                        "Creating priority switch for room %s (%s) on device %s", 
                        room.room_name, room_id, device.device_id
                    )
                    entities.append(
                        LyricRoomPrioritySwitch(
                            coordinator,
                            location,
                            device,
                            room_id,
                            room,
                        )
                    )

    async_add_entities(entities)


class LyricEmergencyHeatSwitch(LyricDeviceEntity, SwitchEntity):
    """Representation of a Lyric emergency heat switch."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[Lyric],
        location: LyricLocation,
        device: LyricDevice,
    ) -> None:
        """Initialize the emergency heat switch."""
        super().__init__(
            coordinator,
            location,
            device,
            f"{device.mac_id}_emergency_heat",
        )
        self._attr_name = "Emergency heat"
        self._attr_unique_id = f"{device.mac_id}_emergency_heat"
        _LOGGER.debug(
            "Initialized emergency heat switch for %s (device_id: %s, mac_id: %s)",
            self.name,
            device.device_id,
            device.mac_id
        )

    @property
    def is_on(self) -> bool:
        """Return true if emergency heat is enabled."""
        try:
            device = self.device
            return (
                device.changeable_values.emergency_heat_active or
                device.settings.attributes.get("specialMode", {}).get("emergencyHeatActive", False)
            )
        except Exception as e:
            _LOGGER.error("Error checking emergency heat status: %s", e)
            return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        device = self.device
        return {
            "hvac_mode": device.changeable_values.mode,
            "temperature": device.indoor_temperature,
            "setpoint": device.changeable_values.heat_setpoint,
            "allowed_modes": device.allowed_modes,
            "emergency_heat_active": device.changeable_values.emergency_heat_active,
            "special_mode": device.settings.attributes.get("specialMode", {}),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on emergency heat."""
        try:
            await self._update_thermostat(
                self.location,
                self.device,
                mode=LYRIC_HVAC_MODE_EMERGENCY_HEAT,
            )
            await self.coordinator.async_request_refresh()
        except LYRIC_EXCEPTIONS as exception:
            _LOGGER.error("Failed to enable emergency heat: %s", exception)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off emergency heat."""
        try:
            await self._update_thermostat(
                self.location,
                self.device,
                mode="Heat",
            )
            await self.coordinator.async_request_refresh()
        except LYRIC_EXCEPTIONS as exception:
            _LOGGER.error("Failed to disable emergency heat: %s", exception)


class LyricRoomPrioritySwitch(LyricDeviceEntity, SwitchEntity):
    """Representation of a Lyric room priority switch."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[Lyric],
        location: LyricLocation,
        device: LyricDevice,
        room_id: str,
        room: LyricRoom,
    ) -> None:
        """Initialize the room priority switch."""
        super().__init__(
            coordinator,
            location,
            device,
            f"{device.mac_id}_room_{room_id}_priority",
        )
        self._room_id = room_id
        self._room = room
        self._attr_name = f"{room.room_name} Priority"
        self._attr_unique_id = f"{device.mac_id}_room_{room_id}_priority"

    @property
    def room(self) -> LyricRoom:
        """Get the current room object."""
        return self.coordinator.data.rooms_dict.get(self._mac_id, {}).get(self._room_id)

    @property
    def is_on(self) -> bool:
        """Return true if the room is prioritized."""
        try:
            room = self.room
            if not room:
                return False

            priority_data = room.attributes.get("priority_data", {})
            selected_rooms = priority_data.get("selected_rooms", [])
            return str(self._room_id) in map(str, selected_rooms)
        except Exception as e:
            _LOGGER.error("Error checking priority status: %s", e)
            return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        data = {}
        room = self.room

        if room:
            data = {
                "room_name": room.room_name,
                "room_id": self._room_id,
                "temperature": room.room_avg_temp,
                "humidity": room.room_avg_humidity,
                "accessories": [
                    {
                        "id": acc.id,
                        "type": acc.type,
                    }
                    for acc in room.accessories
                ],
                "priority_data": room.attributes.get("priority_data", {})
            }

        return data

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on room priority."""
        try:
            # Get current priority data and append room
            priority_data = self.room.attributes.get("priority_data", {})
            current_rooms = list(priority_data.get("selected_rooms", []))
            if str(self._room_id) not in map(str, current_rooms):
                current_rooms.append(int(self._room_id))

            await self._update_priority(
                self.location,
                self.device,
                PRIORITY_TYPE_PICKROOM,
                current_rooms,
            )
            await self.coordinator.async_request_refresh()
        except LYRIC_EXCEPTIONS as exception:
            _LOGGER.error("Failed to set room priority: %s", exception)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off room priority."""
        try:
            # Get current priority data and remove room
            priority_data = self.room.attributes.get("priority_data", {})
            current_rooms = list(priority_data.get("selected_rooms", []))
            current_rooms = [int(r) for r in current_rooms if str(r) != str(self._room_id)]

            priority_type = PRIORITY_TYPE_WHOLEHOUSE if not current_rooms else PRIORITY_TYPE_PICKROOM

            await self._update_priority(
                self.location,
                self.device,
                priority_type,
                current_rooms,
            )
            await self.coordinator.async_request_refresh()
        except LYRIC_EXCEPTIONS as exception:
            _LOGGER.error("Failed to clear room priority: %s", exception)

class LyricFollowScheduleSwitch(LyricDeviceEntity, SwitchEntity):
    """On while the thermostat's own schedule is enabled.

    Turning it on enables (resumes) the schedule and cancels any hold, so the
    thermostat follows its schedule again. Turning it off disables (pauses)
    the schedule; the thermostat keeps its current temperatures.
    """

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, location, device) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, location, device, f"{device.mac_id}_follow_schedule")
        self._attr_name = "Follow schedule"

    @property
    def is_on(self) -> bool:
        """Return True when the thermostat's schedule is enabled."""
        if (status := self.device.schedule_status) is not None:
            return str(status).lower() == "resume"
        return self.device.changeable_values.thermostat_setpoint_status == PRESET_NO_HOLD

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the schedule status and the active hold, if any."""
        values = self.device.changeable_values
        return {
            "schedule_status": self.device.schedule_status,
            "hold": values.thermostat_setpoint_status,
            "hold_until": values.next_period_time,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the schedule and cancel any hold."""
        manager = self.coordinator.schedule_sync
        try:
            await manager.async_set_schedule_enabled(self.location, self.device, True)
            await self._update_thermostat(
                self.location, self.device, thermostat_setpoint_status=PRESET_NO_HOLD
            )
        except (*LYRIC_EXCEPTIONS, SyncError, ClientError, TimeoutError) as err:
            raise HomeAssistantError(f"Couldn't resume the schedule: {err}") from err
        await self._async_confirm("resume")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the schedule, keeping the current temperatures."""
        manager = self.coordinator.schedule_sync
        try:
            await manager.async_set_schedule_enabled(self.location, self.device, False)
        except (*LYRIC_EXCEPTIONS, SyncError, ClientError, TimeoutError) as err:
            raise HomeAssistantError(f"Couldn't pause the schedule: {err}") from err
        await self._async_confirm("pause")

    async def _async_confirm(self, expected: str) -> None:
        await self.coordinator.async_refresh()
        status = self.device.schedule_status
        if status is not None and str(status).lower() != expected:
            raise HomeAssistantError(
                f"{self.device.name} accepted the request, but its schedule status is "
                f"still '{status}'"
            )
