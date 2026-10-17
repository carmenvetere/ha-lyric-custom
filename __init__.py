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
from aiolyric.objects.priority import LyricAccessory, LyricPriority, LyricRoom
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

# Resideo serves the /priority endpoint in two schemas depending on the account
# (see https://github.com/timmo001/aiolyric/pull/180). Each pair is
# (current key, classic key). aiolyric 2.0.x reads only the classic keys and
# 2.1.2 reads only the current ones, so we fill in both before parsing.
_PRIORITY_TOP_KEYS = (("priority", "currentPriority"), ("priorityStatus", "status"))
_PRIORITY_ROOM_KEYS = (
    ("name", "roomName"),
    ("avgTemperature", "roomAvgTemp"),
    ("avgHumidity", "roomAvgHumidity"),
)
_PRIORITY_ACCESSORY_KEYS = (
    ("sensorType", "type"),
    ("excludeTemperature", "excludeTemp"),
)


def _with_both_keys(data: dict, pairs: tuple[tuple[str, str], ...]) -> dict:
    """Return a copy of data where each key pair is present under both names."""
    data = dict(data)
    for current, classic in pairs:
        if current in data and classic not in data:
            data[classic] = data[current]
        elif classic in data and current not in data:
            data[current] = data[classic]
    return data


def _normalize_priority_payload(payload: dict) -> dict:
    """Make a /priority response readable by any aiolyric version."""
    payload = _with_both_keys(payload, _PRIORITY_TOP_KEYS)
    current = dict(payload.get("priority") or {})
    rooms = []
    for room in current.get("rooms", []):
        room = _with_both_keys(room, _PRIORITY_ROOM_KEYS)
        room["accessories"] = [
            _with_both_keys(acc, _PRIORITY_ACCESSORY_KEYS)
            for acc in room.get("accessories", [])
        ]
        rooms.append(room)
    current["rooms"] = rooms
    payload["priority"] = payload["currentPriority"] = current
    return payload


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
        location: LyricLocation,
        device: LyricDevice,
        priority_type: str,
        selected_rooms: list[int],
    ) -> None:
        """Set room priority using PUT (aiolyric uses POST which is wrong)."""
        selected_rooms = [int(r) for r in selected_rooms]
        data = {
            "currentPriority": {
                "priorityType": priority_type,
                "selectedRooms": selected_rooms
            }
        }
        url = (
            f"{BASE_URL}/devices/thermostats/{device.device_id}/priority"
            f"?apikey={lyric.client_id}&locationId={location.location_id}"
        )

        # Get access token and make PUT request with proper auth headers
        access_token = await client.async_get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        async with session.put(url, headers=headers, json=data) as response:
            if response.status >= 400:
                error_text = await response.text()
                raise LyricException(f"Failed to set priority: {response.status} - {error_text}")

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
                # Fetch the priority payload ourselves instead of using
                # lyric.get_thermostat_rooms(): Resideo serves two schemas for
                # this endpoint and aiolyric 2.1.2 only parses one of them.
                async def fetch_rooms(location: LyricLocation, device: LyricDevice) -> None:
                    """Fetch room and priority data for a device."""
                    try:
                        url = (
                            f"{BASE_URL}/devices/thermostats/{device.device_id}/priority"
                            f"?apikey={lyric.client_id}&locationId={location.location_id}"
                        )
                        access_token = await client.async_get_access_token()
                        headers = {
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/json",
                        }

                        async with session.get(url, headers=headers) as response:
                            if response.status >= 400:
                                _LOGGER.warning(
                                    "Failed to get priority for device %s: %s",
                                    device.device_id,
                                    response.status,
                                )
                                return
                            payload = await response.json()

                        priority = LyricPriority(_normalize_priority_payload(payload))
                        current = priority.current_priority
                        priority_data = {
                            "type": current.priority_type,
                            "selected_rooms": current.selected_rooms,
                        }
                        rooms = {}
                        for room in current.rooms:
                            room.attributes["priority_data"] = priority_data
                            rooms[room.id] = room
                        lyric.rooms_dict[device.mac_id] = rooms

                    except Exception as err:
                        _LOGGER.warning(
                            "Failed to fetch room data for device %s: %s",
                            device.device_id,
                            err,
                        )

                await asyncio.gather(
                    *(fetch_rooms(location, device) for location, device in lcc_devices)
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

    # Store custom priority function for entities to use
    coordinator.set_room_priority = set_room_priority

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
        # Use custom PUT-based priority update (aiolyric uses POST which is wrong)
        self._update_priority = coordinator.set_room_priority

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
