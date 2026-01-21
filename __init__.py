"""The Honeywell Lyric integration."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from aiohttp.client_exceptions import ClientResponseError
from aiolyric import Lyric
from aiolyric.exceptions import LyricAuthenticationException, LyricException
from aiolyric.objects.device import LyricDevice
from aiolyric.objects.location import LyricLocation
from aiolyric.objects.priority import LyricAccessory, LyricRoom

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import (
    aiohttp_client,
    config_entry_oauth2_flow,
    config_validation as cv,
    device_registry as dr,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    ConfigEntryLyricClient,
    LyricLocalOAuth2Implementation,
    OAuth2SessionLyric,
)
from .const import DOMAIN

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.SWITCH, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Honeywell Lyric from a config entry."""
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )
    if not isinstance(implementation, LyricLocalOAuth2Implementation):
        raise TypeError("Unexpected auth implementation; can't find oauth client id")

    session = aiohttp_client.async_get_clientsession(hass)
    oauth_session = OAuth2SessionLyric(hass, entry, implementation)

    client = ConfigEntryLyricClient(session, oauth_session)

    client_id = implementation.client_id
    lyric = Lyric(client, client_id)

    async def async_update_data(force_refresh_token: bool = False) -> Lyric:
        """Fetch data from Lyric."""
        try:
            if not force_refresh_token:
                await oauth_session.async_ensure_token_valid()
            else:
                await oauth_session.force_refresh_token()

            await lyric.get_locations()

            # Fetch room data for all LCC thermostats in parallel
            lcc_devices = [
                (location, device)
                for location in lyric.locations
                for device in location.devices
                if device.device_class == "Thermostat"
                and device.device_id.startswith("LCC")
            ]

            if lcc_devices:
                await asyncio.gather(
                    *(
                        lyric.get_thermostat_rooms(location.location_id, device.device_id)
                        for location, device in lcc_devices
                    )
                )

                # Fetch priority data for all devices in parallel
                async def fetch_priority(location: LyricLocation, device: LyricDevice) -> None:
                    """Fetch and store priority data for a device."""
                    try:
                        # Use the rooms data which includes priority info from get_thermostat_rooms
                        if device.mac_id not in lyric.rooms_dict:
                            return

                        rooms = lyric.rooms_dict[device.mac_id]
                        if not rooms:
                            return

                        # Get priority from first room's data (it's the same for all rooms)
                        first_room = next(iter(rooms.values()))
                        priority_data = getattr(first_room, 'priority', None)

                        if priority_data:
                            selected_rooms = getattr(priority_data, 'selected_rooms', [])
                            priority_type = getattr(priority_data, 'priority_type', None)
                        else:
                            # Fallback: check room attributes
                            selected_rooms = first_room.attributes.get("priority_data", {}).get("selected_rooms", [])
                            priority_type = first_room.attributes.get("priority_data", {}).get("type")

                        # Store priority data in all rooms
                        for room in rooms.values():
                            room.attributes["priority_data"] = {
                                "type": priority_type,
                                "selected_rooms": selected_rooms
                            }

                    except Exception as err:
                        _LOGGER.warning(
                            "Error processing priority data for device %s: %s",
                            device.device_id,
                            err,
                        )

                await asyncio.gather(
                    *(fetch_priority(location, device) for location, device in lcc_devices)
                )

        except LyricAuthenticationException as exception:
            if not force_refresh_token:
                return await async_update_data(force_refresh_token=True)
            raise ConfigEntryAuthFailed from exception
        except (LyricException, ClientResponseError) as exception:
            raise UpdateFailed(exception) from exception

        return lyric

    coordinator = DataUpdateCoordinator[Lyric](
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update_data,
        update_interval=timedelta(seconds=300),
    )

    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class LyricEntity(CoordinatorEntity[DataUpdateCoordinator[Lyric]]):
    """Defines a base Honeywell Lyric entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[Lyric],
        location: LyricLocation,
        device: LyricDevice,
        key: str,
    ) -> None:
        """Initialize the Honeywell Lyric entity."""
        super().__init__(coordinator)
        self._key = key
        self._location = location
        self._mac_id = device.mac_id
        self._update_thermostat = coordinator.data.update_thermostat
        self._update_fan = coordinator.data.update_fan
        self._update_priority = coordinator.data.update_priority

    @property
    def unique_id(self) -> str:
        """Return the unique ID for this entity."""
        return self._key

    @property
    def location(self) -> LyricLocation:
        """Get the Lyric Location."""
        return self.coordinator.data.locations_dict[self._location.location_id]

    @property
    def device(self) -> LyricDevice:
        """Get the Lyric Device."""
        return self.location.devices_dict[self._mac_id]


class LyricDeviceEntity(LyricEntity):
    """Defines a Honeywell Lyric device entity."""

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this Honeywell Lyric instance."""
        return DeviceInfo(
            identifiers={(dr.CONNECTION_NETWORK_MAC, self._mac_id)},
            connections={(dr.CONNECTION_NETWORK_MAC, self._mac_id)},
            manufacturer="Honeywell",
            model=self.device.device_model,
            name=f"{self.device.name} Thermostat",
        )


class LyricAccessoryEntity(LyricDeviceEntity):
    """Defines a Honeywell Lyric accessory entity, a sub-device of a thermostat."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[Lyric],
        location: LyricLocation,
        device: LyricDevice,
        room: LyricRoom,
        accessory: LyricAccessory,
        key: str,
    ) -> None:
        """Initialize the Honeywell Lyric accessory entity."""
        super().__init__(coordinator, location, device, key)
        self._room_id = room.id
        self._accessory_id = accessory.id

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this Honeywell Lyric instance."""
        return DeviceInfo(
            identifiers={
                (
                    f"{dr.CONNECTION_NETWORK_MAC}_room_accessory",
                    f"{self._mac_id}_room{self._room_id}_accessory{self._accessory_id}",
                )
            },
            manufacturer="Honeywell",
            model="RCHTSENSOR",
            name=f"{self.room.room_name} Sensor",
            via_device=(dr.CONNECTION_NETWORK_MAC, self._mac_id),
        )

    @property
    def room(self) -> LyricRoom:
        """Get the Lyric Room."""
        return self.coordinator.data.rooms_dict[self._mac_id][self._room_id]

    @property
    def accessory(self) -> LyricAccessory:
        """Get the Lyric Accessory."""
        return next(
            accessory
            for accessory in self.room.accessories
            if accessory.id == self._accessory_id
        )
