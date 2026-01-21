"""Support for Honeywell Lyric climate platform."""

from __future__ import annotations

import asyncio
import enum
import logging
from time import localtime, strftime, time
from typing import Any

from aiolyric import Lyric
from aiolyric.objects.device import LyricDevice
from aiolyric.objects.location import LyricLocation
import voluptuous as vol

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    FAN_AUTO,
    FAN_DIFFUSE,
    FAN_ON,
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    PRECISION_HALVES,
    PRECISION_WHOLE,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import VolDictType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import LyricDeviceEntity
from .const import (
    DOMAIN,
    LYRIC_EXCEPTIONS,
    LYRIC_HVAC_MODE_EMERGENCY_HEAT,
    PRESET_HOLD_UNTIL,
    PRESET_NO_HOLD,
    PRESET_PERMANENT_HOLD,
    PRESET_TEMPORARY_HOLD,
    PRESET_VACATION_HOLD,
)

_LOGGER = logging.getLogger(__name__)

# Only LCC models support presets
SUPPORT_FLAGS_LCC = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.PRESET_MODE
    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
)
SUPPORT_FLAGS_TCC = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
)

LYRIC_HVAC_ACTION_OFF = "EquipmentOff"
LYRIC_HVAC_ACTION_HEAT = "Heat"
LYRIC_HVAC_ACTION_COOL = "Cool"

LYRIC_HVAC_MODE_OFF = "Off"
LYRIC_HVAC_MODE_HEAT = "Heat"
LYRIC_HVAC_MODE_COOL = "Cool"
LYRIC_HVAC_MODE_HEAT_COOL = "Auto"

LYRIC_FAN_MODE_ON = "On"
LYRIC_FAN_MODE_AUTO = "Auto"
LYRIC_FAN_MODE_DIFFUSE = "Circulate"

# Map Home Assistant HVAC modes to Lyric HVAC modes
LYRIC_HVAC_MODES = {
    HVACMode.OFF: LYRIC_HVAC_MODE_OFF,
    HVACMode.HEAT: LYRIC_HVAC_MODE_HEAT,
    HVACMode.COOL: LYRIC_HVAC_MODE_COOL,
    HVACMode.HEAT_COOL: LYRIC_HVAC_MODE_HEAT_COOL,
}

# Map Lyric HVAC modes to Home Assistant HVAC modes
HVAC_MODES = {
    LYRIC_HVAC_MODE_OFF: HVACMode.OFF,
    LYRIC_HVAC_MODE_HEAT: HVACMode.HEAT,
    LYRIC_HVAC_MODE_COOL: HVACMode.COOL,
    LYRIC_HVAC_MODE_HEAT_COOL: HVACMode.HEAT_COOL,
    LYRIC_HVAC_MODE_EMERGENCY_HEAT: HVACMode.HEAT,  # Map EmergencyHeat to HEAT
}

LYRIC_FAN_MODES = {
    FAN_ON: LYRIC_FAN_MODE_ON,
    FAN_AUTO: LYRIC_FAN_MODE_AUTO,
    FAN_DIFFUSE: LYRIC_FAN_MODE_DIFFUSE,
}

FAN_MODES = {
    LYRIC_FAN_MODE_ON: FAN_ON,
    LYRIC_FAN_MODE_AUTO: FAN_AUTO,
    LYRIC_FAN_MODE_DIFFUSE: FAN_DIFFUSE,
}

HVAC_ACTIONS = {
    LYRIC_HVAC_ACTION_OFF: HVACAction.OFF,
    LYRIC_HVAC_ACTION_HEAT: HVACAction.HEATING,
    LYRIC_HVAC_ACTION_COOL: HVACAction.COOLING,
}

SERVICE_HOLD_TIME = "set_hold_time"
ATTR_TIME_PERIOD = "time_period"

SERVICE_SET_ROOM_PRIORITY = "set_room_priority"
ATTR_ROOM_NAMES = "room_names"

SCHEMA_HOLD_TIME: VolDictType = {
    vol.Required(ATTR_TIME_PERIOD, default="01:00:00"): vol.All(
        cv.time_period,
        cv.positive_timedelta,
        lambda td: strftime("%H:%M:%S", localtime(time() + td.total_seconds())),
    )
}

SCHEMA_SET_ROOM_PRIORITY = vol.Schema(
    {
        vol.Required(ATTR_ROOM_NAMES): vol.All(cv.ensure_list, [cv.string]),
    }
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Honeywell Lyric climate platform based on a config entry."""
    coordinator: DataUpdateCoordinator[Lyric] = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        (
            LyricClimate(
                coordinator,
                ClimateEntityDescription(
                    key=f"{device.mac_id}_thermostat",
                    name=device.name,
                ),
                location,
                device,
            )
            for location in coordinator.data.locations
            for device in location.devices
        ),
        True,
    )

    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(
            SERVICE_HOLD_TIME,
            {
                vol.Required(ATTR_TIME_PERIOD, default="01:00:00"): vol.All(
                    cv.time_period,
                    cv.positive_timedelta,
                    lambda td: strftime("%H:%M:%S", localtime(time() + td.total_seconds())),
                )
            },
            "async_set_hold_time",
        )

    platform.async_register_entity_service(
        SERVICE_SET_ROOM_PRIORITY,
        {
            vol.Required(ATTR_ROOM_NAMES): vol.All(cv.ensure_list, [cv.string]),
        },
        "async_set_room_priority",
    )


class LyricThermostatType(enum.Enum):
    """Lyric thermostats are classified as TCC or LCC devices."""

    TCC = enum.auto()
    LCC = enum.auto()


class LyricClimate(LyricDeviceEntity, ClimateEntity):
    """Defines a Honeywell Lyric climate entity."""

    coordinator: DataUpdateCoordinator[Lyric]
    entity_description: ClimateEntityDescription

    _attr_name = None
    _attr_preset_modes = [
        PRESET_NO_HOLD,
        PRESET_HOLD_UNTIL,
        PRESET_PERMANENT_HOLD,
        PRESET_TEMPORARY_HOLD,
        PRESET_VACATION_HOLD,
    ]
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[Lyric],
        description: ClimateEntityDescription,
        location: LyricLocation,
        device: LyricDevice,
    ) -> None:
        """Initialize Honeywell Lyric climate entity."""
        # Define thermostat type (TCC - e.g., Lyric round; LCC - e.g., T5,6)
        if device.changeable_values.thermostat_setpoint_status:
            self._attr_thermostat_type = LyricThermostatType.LCC
        else:
            self._attr_thermostat_type = LyricThermostatType.TCC

        # Use the native temperature unit from the device settings
        if device.units == "Fahrenheit":
            self._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
            self._attr_precision = PRECISION_WHOLE
        else:
            self._attr_temperature_unit = UnitOfTemperature.CELSIUS
            self._attr_precision = PRECISION_HALVES

        # Setup supported hvac modes
        self._attr_hvac_modes = [HVACMode.OFF]

        # Add supported lyric thermostat features
        if LYRIC_HVAC_MODE_HEAT in device.allowed_modes:
            self._attr_hvac_modes.append(HVACMode.HEAT)

        if LYRIC_HVAC_MODE_COOL in device.allowed_modes:
            self._attr_hvac_modes.append(HVACMode.COOL)

        if LYRIC_HVAC_MODE_HEAT_COOL in device.allowed_modes:
            self._attr_hvac_modes.append(HVACMode.HEAT_COOL)

        # Setup supported features
        if self._attr_thermostat_type is LyricThermostatType.LCC:
            self._attr_supported_features = SUPPORT_FLAGS_LCC
        else:
            self._attr_supported_features = SUPPORT_FLAGS_TCC

        # Setup supported fan modes
        if device_fan_modes := device.settings.attributes.get("fan", {}).get(
            "allowedModes"
        ):
            self._attr_fan_modes = [
                FAN_MODES[device_fan_mode]
                for device_fan_mode in device_fan_modes
                if device_fan_mode in FAN_MODES
            ]
            self._attr_supported_features |= ClimateEntityFeature.FAN_MODE

        if len(self.hvac_modes) > 1:
            self._attr_supported_features |= (
                ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
            )

        super().__init__(
            coordinator,
            location,
            device,
            f"{device.mac_id}_thermostat",
        )
        self.entity_description = description

    @property
    def unique_id(self) -> str:
        """Return a unique ID for this climate device."""
        return f"{self.device.mac_id}_thermostat_test"

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self.device.indoor_temperature

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current hvac action."""
        action = HVAC_ACTIONS.get(self.device.operation_status.mode, None)
        if action == HVACAction.OFF and self.hvac_mode != HVACMode.OFF:
            action = HVACAction.IDLE
        return action

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the hvac mode."""
        lyric_mode = self.device.changeable_values.mode
        return HVAC_MODES.get(lyric_mode, HVACMode.OFF)

    @property
    def preset_mode(self) -> str | None:
        """Return current preset mode."""
        return self.device.changeable_values.thermostat_setpoint_status

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        device = self.device
        if (
            device.changeable_values.auto_changeover_active
            or self.hvac_mode == HVACMode.OFF
        ):
            return None
        if self.hvac_mode == HVACMode.COOL:
            return device.changeable_values.cool_setpoint
        return device.changeable_values.heat_setpoint

    @property
    def target_temperature_high(self) -> float | None:
        """Return the highbound target temperature we try to reach."""
        device = self.device
        if (
            not device.changeable_values.auto_changeover_active
            or self.hvac_mode == HVACMode.OFF
        ):
            return None
        return device.changeable_values.cool_setpoint

    @property
    def target_temperature_low(self) -> float | None:
        """Return the lowbound target temperature we try to reach."""
        device = self.device
        if (
            not device.changeable_values.auto_changeover_active
            or self.hvac_mode == HVACMode.OFF
        ):
            return None
        return device.changeable_values.heat_setpoint

    @property
    def min_temp(self) -> float:
        """Identify min_temp in Lyric API or defaults if not available."""
        device = self.device
        if LYRIC_HVAC_MODE_COOL in device.allowed_modes:
            return device.min_cool_setpoint
        return device.min_heat_setpoint

    @property
    def max_temp(self) -> float:
        """Identify max_temp in Lyric API or defaults if not available."""
        device = self.device
        if LYRIC_HVAC_MODE_HEAT in device.allowed_modes:
            return device.max_heat_setpoint
        return device.max_cool_setpoint

    @property
    def fan_mode(self) -> str | None:
        """Return current fan mode."""
        device = self.device
        return FAN_MODES.get(
            device.settings.attributes.get("fan", {})
            .get("changeableValues", {})
            .get("mode")
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        data = {}
        _LOGGER.debug("Device mac_id: %s", self.device.mac_id)
        
        # Get rooms data
        rooms = self.coordinator.data.rooms_dict.get(self.device.mac_id, {})
        _LOGGER.debug("Rooms for device %s: %s", self.device.name, rooms)
        
        try:
            # Get room data directly from rooms
            room_priorities = {}
            for room_id, room in rooms.items():
                room_priorities[room.room_name] = {
                    "id": room_id,
                    "temperature": room.room_avg_temp,
                    "humidity": room.room_avg_humidity,
                    "accessories": [
                        {
                            "id": acc.id,
                            "type": acc.type,
                        }
                        for acc in room.accessories
                    ]
                }
            
            data["room_priorities"] = room_priorities
            _LOGGER.debug("Room priorities for %s: %s", self.device.name, room_priorities)
            
        except Exception as e:
            _LOGGER.error("Error processing room priorities: %s", str(e))
            _LOGGER.debug("Error details:", exc_info=True)
        
        return data

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if self.hvac_mode == HVACMode.OFF:
            return

        device = self.device
        target_temp_low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        target_temp_high = kwargs.get(ATTR_TARGET_TEMP_HIGH)

        if device.changeable_values.mode == LYRIC_HVAC_MODE_HEAT_COOL:
            if target_temp_low is None or target_temp_high is None:
                raise HomeAssistantError(
                    "Could not find target_temp_low and/or target_temp_high in arguments"
                )

            # If TCC device pass the heatCoolMode value, otherwise
            # if LCC device can skip the mode altogether
            if self._attr_thermostat_type is LyricThermostatType.TCC:
                mode = HVAC_MODES[device.changeable_values.heat_cool_mode]
            else:
                mode = None

            _LOGGER.debug(
                "Set temperature: %s - %s", target_temp_low, target_temp_high
            )
            try:
                await self._update_thermostat(
                    self.location,
                    device,
                    cool_setpoint=target_temp_high,
                    heat_setpoint=target_temp_low,
                    mode=mode,
                )
            except LYRIC_EXCEPTIONS as exception:
                _LOGGER.error(exception)
            await self.coordinator.async_refresh()
        else:
            temp = kwargs.get(ATTR_TEMPERATURE)
            _LOGGER.debug("Set temperature: %s", temp)
            try:
                if self.hvac_mode == HVACMode.COOL:
                    await self._update_thermostat(
                        self.location, device, cool_setpoint=temp
                    )
                else:
                    await self._update_thermostat(
                        self.location, device, heat_setpoint=temp
                    )
            except LYRIC_EXCEPTIONS as exception:
                _LOGGER.error(exception)
            await self.coordinator.async_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set hvac mode."""
        _LOGGER.debug("HVAC mode: %s", hvac_mode)
        try:
            lyric_mode = LYRIC_HVAC_MODES[hvac_mode]
            await self._update_thermostat(
                self.location,
                self.device,
                mode=lyric_mode,
            )
        except LYRIC_EXCEPTIONS as exception:
            _LOGGER.error(exception)
        await self.coordinator.async_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set preset mode."""
        _LOGGER.debug("Set preset mode: %s", preset_mode)
        try:
            await self._update_thermostat(
                self.location,
                self.device,
                thermostat_setpoint_status=preset_mode,
            )
        except LYRIC_EXCEPTIONS as exception:
            _LOGGER.error(exception)
        await self.coordinator.async_refresh()

    async def async_set_hold_time(self, time_period: str) -> None:
        """Set the time to hold until."""
        _LOGGER.debug("set_hold_time: %s", time_period)
        try:
            await self._update_thermostat(
                self.location,
                self.device,
                thermostat_setpoint_status=PRESET_HOLD_UNTIL,
                next_period_time=time_period,
            )
        except LYRIC_EXCEPTIONS as exception:
            _LOGGER.error(exception)
        await self.coordinator.async_refresh()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan mode."""
        _LOGGER.debug("Set fan mode: %s", fan_mode)
        try:
            _LOGGER.debug("Fan mode passed to lyric: %s", LYRIC_FAN_MODES[fan_mode])
            await self._update_fan(
                self.location, self.device, mode=LYRIC_FAN_MODES[fan_mode]
            )
        except LYRIC_EXCEPTIONS as exception:
            _LOGGER.error(exception)
        except KeyError:
            _LOGGER.error(
                "The fan mode requested does not have a corresponding mode in lyric: %s",
                fan_mode,
            )
        await self.coordinator.async_refresh()

    async def async_set_room_priority(self, room_names: list[str]) -> None:
        """Set room priority."""
        try:
            rooms = self.coordinator.data.rooms_dict.get(self.device.mac_id, {})

            # Build list of room IDs that match provided names
            room_ids = [
                int(room_id) for room_id, room in rooms.items()
                if room.room_name in room_names
            ]

            if not room_ids:
                _LOGGER.error("No valid room names provided")
                return

            await self._update_priority(
                self.location,
                self.device,
                "PickARoom",
                room_ids,
            )
            await self.coordinator.async_request_refresh()
        except LYRIC_EXCEPTIONS as exception:
            _LOGGER.error("Failed to set room priority: %s", exception)

