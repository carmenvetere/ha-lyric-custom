"""Select platform for Honeywell Lyric: which schedule helper each thermostat follows."""

from __future__ import annotations

from aiolyric import Lyric
from aiolyric.objects.device import LyricDevice
from aiolyric.objects.location import LyricLocation

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import LyricDeviceEntity
from .const import DOMAIN
from .schedule_sync import ScheduleSyncManager, is_schedulable, signal_update

NOT_SYNCED = "Not synced"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the schedule source selects."""
    coordinator: DataUpdateCoordinator[Lyric] = hass.data[DOMAIN][entry.entry_id]
    manager: ScheduleSyncManager = coordinator.schedule_sync
    async_add_entities(
        LyricScheduleSourceSelect(coordinator, location, device, manager)
        for location in coordinator.data.locations
        for device in location.devices
        if is_schedulable(device)
    )


class LyricScheduleSourceSelect(LyricDeviceEntity, SelectEntity):
    """Choose the schedule helper whose schedule is sent to the thermostat.

    "Not synced" leaves the thermostat's own schedule alone. Automations can
    switch sources, e.g. a guest-mode schedule for the office.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-sync"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[Lyric],
        location: LyricLocation,
        device: LyricDevice,
        manager: ScheduleSyncManager,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator, location, device, f"{device.mac_id}_schedule_source")
        self._attr_name = "Schedule source"
        self._manager = manager

    @property
    def current_option(self) -> str:
        """Return the linked schedule helper."""
        return self._manager.state(self._mac_id).source or NOT_SYNCED

    @property
    def options(self) -> list[str]:
        """Return 'Not synced' plus every schedule helper in Home Assistant."""
        helpers = sorted(self.hass.states.async_entity_ids("schedule"))
        current = self._manager.state(self._mac_id).source
        if current and current not in helpers:
            helpers.append(current)  # keep a deleted helper visible until changed
        return [NOT_SYNCED, *helpers]

    async def async_select_option(self, option: str) -> None:
        """Link the thermostat to a schedule helper."""
        await self._manager.async_set_source(
            self._mac_id, None if option == NOT_SYNCED else option
        )

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
