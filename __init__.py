"""The Honeywell Lyric integration."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from http import HTTPStatus
import logging

from aiohttp.client_exceptions import ClientResponseError
from aiolyric import Lyric
from aiolyric.exceptions import LyricAuthenticationException, LyricException
from aiolyric.objects.device import LyricDevice
from aiolyric.objects.location import LyricLocation
from aiolyric.objects.priority import LyricAccessory, LyricRoom
from aiolyric.const import BASE_URL

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

    async def set_room_priority(
        self,
        location_id: int,
        device_id: str,
        priority_type: str = "PickARoom",
        selected_rooms: list = None,
    ) -> None:
        """Set room priority for a thermostat using direct HTTP request."""
        if selected_rooms is None:
            selected_rooms = []

        # Ensure room IDs are integers
        selected_rooms = [int(room_id) for room_id in selected_rooms]

        data = {
            "currentPriority": {
                "priorityType": priority_type,
                "selectedRooms": selected_rooms
            }
        }

        url = f"{BASE_URL}/devices/thermostats/{device_id}/priority?apikey={self.client_id}&locationId={location_id}"
        _LOGGER.debug("Setting room priority - URL: %s, Data: %s", url, data)

        try:
            # Get the access token from the client
            access_token = await self._client.async_get_access_token()

            # Create headers with the access token
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }

            # Use the session from the client to make a direct PUT request
            async with self._client._session.put(url, headers=headers, json=data) as response:
                # Any 2xx status code is success
                if 200 <= response.status < 300:
                    _LOGGER.debug("Successfully set room priority")
                    return True
                else:
                    _LOGGER.error(
                        "Error setting priority - Status: %s", 
                        response.status
                    )
                    raise LyricException(
                        f"Failed to set priority: {response.status}"
                    )

        except Exception as e:
            _LOGGER.error("Error setting room priority: %s", str(e))
            raise

    async def async_update_data(force_refresh_token: bool = False) -> Lyric:
        """Fetch data from Lyric."""
        try:
            # ADDED
            _LOGGER.debug(
                "Starting Lyric data update (force_refresh_token=%s)",
                force_refresh_token
            )

            if not force_refresh_token:
                await oauth_session.async_ensure_token_valid()
            else:
                await oauth_session.force_refresh_token()

            # ADDED
            _LOGGER.debug("Token is valid; now calling lyric.get_locations()")

            await lyric.get_locations()

            # ADDED
            _LOGGER.debug("Lyric locations after get_locations(): %s", lyric.locations)

            await asyncio.gather(
                *(
                    lyric.get_thermostat_rooms(location.location_id, device.device_id)
                    for location in lyric.locations
                    for device in location.devices
                    if device.device_class == "Thermostat"
                    and device.device_id.startswith("LCC")
                )
            )

            # ADDED
            _LOGGER.debug(
                "Lyric rooms_dict after get_thermostat_rooms: %s", lyric.rooms_dict
            )

            # EXTRA LOGGING ADDED - Dump each location & device fields
            for location in lyric.locations:
                _LOGGER.debug("Location ID %s -> %s", location.location_id, location.__dict__)

                for device in location.devices:
                    _LOGGER.debug("Device %s raw data: %s", device.device_id, device.__dict__)

                    # If the objects have sub-objects like changeable_values, settings, etc.
                    if getattr(device, "changeable_values", None):
                        _LOGGER.debug(
                            "Device %s changeable_values: %s",
                            device.device_id,
                            device.changeable_values.__dict__
                            if hasattr(device.changeable_values, "__dict__")
                            else str(device.changeable_values),
                        )
                    if getattr(device, "settings", None):
                        _LOGGER.debug(
                            "Device %s settings: %s",
                            device.device_id,
                            device.settings.__dict__
                            if hasattr(device.settings, "__dict__")
                            else str(device.settings),
                        )

            # Get priority data for each device
            for location in lyric.locations:
                for device in location.devices:
                    if (
                        device.device_class == "Thermostat"
                        and device.device_id.startswith("LCC")
                    ):
                        try:
                            url = (
                                f"{BASE_URL}/devices/thermostats/{device.device_id}/priority"
                                f"?apikey={lyric.client_id}&locationId={location.location_id}"
                            )
                            _LOGGER.debug(
                                "Requesting priority data for device %s at %s",
                                device.device_id,
                                url
                            )

                            response = await device.client.get(url)
                            priority_data = await response.json()

                            # ADDED
                            _LOGGER.debug(
                                "Priority data for device %s: %s",
                                device.device_id,
                                priority_data
                            )

                            if device.mac_id in lyric.rooms_dict:
                                current_priority = priority_data.get("currentPriority", {})
                                selected_rooms = current_priority.get("selectedRooms", [])
                                priority_type = current_priority.get("priorityType")

                                _LOGGER.debug(
                                    "Retrieved priority for device %s: type=%s, rooms=%s",
                                    device.device_id,
                                    priority_type,
                                    selected_rooms
                                )

                                # Store priority data in rooms
                                rooms = lyric.rooms_dict[device.mac_id]
                                for room in rooms.values():
                                    room.attributes["priority_data"] = {
                                        "type": priority_type,
                                        "selected_rooms": selected_rooms
                                    }

                        except Exception as e:
                            _LOGGER.error(
                                "Error getting priority data for device %s: %s",
                                device.device_id,
                                str(e),
                            )

        except LyricAuthenticationException as exception:
            _LOGGER.debug("Authentication failed. Attempting to refresh token.")
            if not force_refresh_token:
                return await async_update_data(force_refresh_token=True)
            raise ConfigEntryAuthFailed from exception
        except (LyricException, ClientResponseError) as exception:
            raise UpdateFailed(exception) from exception

        return lyric

    # Add the set_room_priority method to the Lyric instance
    lyric.set_room_priority = set_room_priority.__get__(lyric)

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
