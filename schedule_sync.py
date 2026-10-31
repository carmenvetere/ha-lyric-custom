"""Keep thermostat schedules in sync with Home Assistant schedule helpers.

Each T-series thermostat can be linked to one schedule helper (its "schedule
source"). About once a minute, and whenever the source changes, the helper is
converted to thermostat periods. The schedule is only sent to the thermostat
when that conversion differs from what was last sent successfully. Before
sending, the thermostat's current schedule is read; if it already matches,
nothing is sent. After sending, the schedule is read back to confirm the
thermostat applied it, because this API can return success and ignore a write.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import logging
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout
from aiolyric import Lyric
from aiolyric.const import BASE_URL
from aiolyric.objects.device import LyricDevice
from aiolyric.objects.location import LyricLocation

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .schedule_convert import (
    DeviceLimits,
    DeviceSchedule,
    ScheduleError,
    ScheduleFormatError,
    blocks_to_periods,
    build_device_schedule,
    describe,
    fingerprint,
    parse_device_schedule,
    same_schedule,
    slugify,
)

_LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL = timedelta(seconds=60)
THERMOSTAT_READ_INTERVAL = timedelta(hours=6)
RETRY_AFTER = timedelta(minutes=15)
VERIFY_DELAY_SECONDS = 5
VERIFY_ATTEMPTS = 3
REQUEST_TIMEOUT = ClientTimeout(total=30)

# Extra query parameters for the schedule endpoint, tried in order until a
# read succeeds; the one that works is then used for reads and writes. The
# live API rejected requests without a 'type' (HTTP 400), and its docs list a
# required 'type' without stating the value.
SCHEDULE_QUERY_VARIANTS = ("&type=regular", "", "&type=timed")

STORE_VERSION = 1

STATUS_NOT_CONFIGURED = "not_configured"
STATUS_SYNCING = "syncing"
STATUS_SYNCED = "synced"
STATUS_ERROR = "error"
STATUSES = [STATUS_NOT_CONFIGURED, STATUS_SYNCING, STATUS_SYNCED, STATUS_ERROR]


class SyncError(Exception):
    """Sending the schedule to the thermostat failed."""


def signal_update(entry_id: str, mac_id: str) -> str:
    """Dispatcher signal sent when a thermostat's sync state changes."""
    return f"{DOMAIN}_schedule_sync_{entry_id}_{mac_id}"


def is_schedulable(device: LyricDevice) -> bool:
    """Only T-series (LCC) thermostats have an on-device schedule API."""
    return device.device_class == "Thermostat" and str(device.device_id).startswith("LCC")


@dataclass
class SyncState:
    """Sync state for one thermostat."""

    source: str | None = None
    status: str = STATUS_NOT_CONFIGURED
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    schedule: dict[str, str] | None = None  # what the source converts to
    synced_fingerprint: str | None = None
    synced_at: datetime | None = None
    last_checked: datetime | None = None
    thermostat_schedule: dict[str, str] | None = None
    thermostat_schedule_raw: Any = None
    thermostat_read_at: datetime | None = None
    thermostat_read_error: str | None = None
    failed_fingerprint: str | None = None
    failed_at: datetime | None = None


class ScheduleSyncManager:
    """Sync schedule helpers to the thermostats of one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        coordinator: DataUpdateCoordinator[Lyric],
        client: Any,
        session: ClientSession,
    ) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.entry_id = entry_id
        self._coordinator = coordinator
        self._client = client
        self._session = session
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.schedule_sync.{entry_id}"
        )
        self.states: dict[str, SyncState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._schedule_query: str | None = None  # working SCHEDULE_QUERY_VARIANTS entry

    # --- lifecycle -------------------------------------------------------

    async def async_setup(self) -> None:
        """Load saved state and start watching for changes."""
        stored = await self._store.async_load() or {}
        for mac_id, data in stored.get("devices", {}).items():
            state = self.state(mac_id)
            state.source = data.get("source")
            state.synced_fingerprint = data.get("synced_fingerprint")
            if synced_at := data.get("synced_at"):
                state.synced_at = dt_util.parse_datetime(synced_at)

        self._unsubs.append(
            async_track_time_interval(self.hass, self._interval_check, CHECK_INTERVAL)
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._interval_read, THERMOSTAT_READ_INTERVAL
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED,
                self._handle_source_state_change,
                event_filter=self._is_source_event,
            )
        )
        self._unsubs.append(async_at_started(self.hass, self._async_started))

    @callback
    def async_unload(self) -> None:
        """Stop watching for changes."""
        while self._unsubs:
            self._unsubs.pop()()

    async def _async_started(self, _hass: HomeAssistant) -> None:
        await self.async_read_all()
        await self.async_check_all()

    @callback
    def _interval_check(self, _now: datetime) -> None:
        self.hass.async_create_task(
            self.async_check_all(), f"{DOMAIN} schedule sync check", eager_start=False
        )

    @callback
    def _interval_read(self, _now: datetime) -> None:
        self.hass.async_create_task(
            self.async_read_all(), f"{DOMAIN} schedule read", eager_start=False
        )

    @callback
    def _is_source_event(self, event_data: Any) -> bool:
        entity_id = event_data.get("entity_id")
        return any(state.source == entity_id for state in self.states.values())

    @callback
    def _handle_source_state_change(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        for mac_id, state in self.states.items():
            if state.source == entity_id:
                self.hass.async_create_task(
                    self.async_check(mac_id), f"{DOMAIN} schedule sync {mac_id}"
                )

    # --- state helpers -----------------------------------------------------

    def state(self, mac_id: str) -> SyncState:
        """Return the sync state for a thermostat, creating it if needed."""
        return self.states.setdefault(mac_id, SyncState())

    def _lock(self, mac_id: str) -> asyncio.Lock:
        return self._locks.setdefault(mac_id, asyncio.Lock())

    @callback
    def _notify(self, mac_id: str) -> None:
        async_dispatcher_send(self.hass, signal_update(self.entry_id, mac_id))

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "devices": {
                    mac_id: {
                        "source": state.source,
                        "synced_fingerprint": state.synced_fingerprint,
                        "synced_at": state.synced_at.isoformat()
                        if state.synced_at
                        else None,
                    }
                    for mac_id, state in self.states.items()
                }
            }
        )

    def _devices(self) -> list[tuple[LyricLocation, LyricDevice]]:
        lyric = self._coordinator.data
        if lyric is None:
            return []
        return [
            (location, device)
            for location in lyric.locations
            for device in location.devices
            if is_schedulable(device)
        ]

    def _device(self, mac_id: str) -> tuple[LyricLocation, LyricDevice] | None:
        return next(
            ((loc, dev) for loc, dev in self._devices() if dev.mac_id == mac_id), None
        )

    def _limits(self, device: LyricDevice) -> DeviceLimits:
        fan = device.settings.attributes.get("fan") or {}
        rooms = self._coordinator.data.rooms_dict.get(device.mac_id, {})
        return DeviceLimits(
            precision=1.0 if device.units == "Fahrenheit" else 0.5,
            time_increment=int(device.allowed_time_increments or 15),
            min_heat=device.min_heat_setpoint,
            max_heat=device.max_heat_setpoint,
            min_cool=device.min_cool_setpoint,
            max_cool=device.max_cool_setpoint,
            deadband=float(device.deadband or 0),
            fan_modes=tuple(fan.get("allowedModes") or ()),
            rooms={slugify(room.room_name): int(room.id) for room in rooms.values()},
        )

    # --- public API --------------------------------------------------------

    async def async_set_source(self, mac_id: str, source: str | None) -> None:
        """Link a thermostat to a schedule helper (or None to stop syncing)."""
        state = self.state(mac_id)
        state.source = source
        state.failed_fingerprint = None
        state.failed_at = None
        await self._async_save()
        self._notify(mac_id)
        await self.async_check(mac_id)

    async def async_check_all(self) -> None:
        """Check every thermostat that has a schedule source."""
        for mac_id, state in list(self.states.items()):
            if state.source:
                await self.async_check(mac_id)

    async def async_read_all(self) -> None:
        """Read every T-series thermostat's current schedule (read-only)."""
        for location, device in self._devices():
            async with self._lock(device.mac_id):
                state = self.state(device.mac_id)
                try:
                    await self._async_read(location, device, state)
                except (
                    SyncError,
                    ScheduleFormatError,
                    ClientError,
                    TimeoutError,
                    HomeAssistantError,
                ) as err:
                    _LOGGER.warning(
                        "Couldn't read the schedule of %s: %s", device.name, err
                    )
                self._notify(device.mac_id)

    async def async_check(self, mac_id: str, force: bool = False) -> SyncState:
        """Send the source schedule to the thermostat if it changed."""
        async with self._lock(mac_id):
            state = self.state(mac_id)
            now = dt_util.utcnow()
            state.last_checked = now

            if not state.source:
                state.status = STATUS_NOT_CONFIGURED
                state.error = None
                state.schedule = None
                state.warnings = []
                self._notify(mac_id)
                return state

            if (found := self._device(mac_id)) is None:
                self._set_error(state, f"Thermostat {mac_id} not found")
                self._notify(mac_id)
                return state
            location, device = found

            try:
                ha_schedule = await self._async_get_ha_schedule(state.source)
                periods, warnings = blocks_to_periods(ha_schedule, self._limits(device))
            except (ScheduleError, HomeAssistantError) as err:
                self._set_error(state, f"{device.name} ({state.source}): {err}")
                self._notify(mac_id)
                return state

            state.warnings = warnings
            state.schedule = describe(periods)
            wanted = fingerprint(periods)

            if not force and wanted == state.synced_fingerprint:
                state.status = STATUS_SYNCED
                state.error = None
                self._notify(mac_id)
                return state

            if (
                not force
                and wanted == state.failed_fingerprint
                and state.failed_at is not None
                and now - state.failed_at < RETRY_AFTER
            ):
                return state  # wait before retrying the same schedule

            state.status = STATUS_SYNCING
            self._notify(mac_id)
            try:
                await self._async_push(location, device, state, periods)
            except (
                SyncError,
                ScheduleError,
                ScheduleFormatError,
                ClientError,
                TimeoutError,
                HomeAssistantError,
            ) as err:
                state.failed_fingerprint = wanted
                state.failed_at = now
                self._set_error(state, f"{device.name} ({state.source}): {err}")
                self._notify(mac_id)
                return state

            state.status = STATUS_SYNCED
            state.error = None
            state.synced_fingerprint = wanted
            state.synced_at = dt_util.utcnow()
            state.failed_fingerprint = None
            state.failed_at = None
            await self._async_save()
            self._notify(mac_id)
            _LOGGER.info("Schedule from %s synced to %s", state.source, device.name)
            return state

    async def async_set_schedule_enabled(
        self, location: LyricLocation, device: LyricDevice, enabled: bool
    ) -> None:
        """Enable (resume) or disable (pause) the thermostat's own schedule.

        This is separate from holds: a thermostat with no hold still ignores
        its schedule while the schedule is paused.
        """
        if self._schedule_query is None:
            await self._async_get_schedule(location, device)  # learn the query form
        url = self._url(
            location,
            device,
            self._schedule_query or "",
            path=f"/status/{'resume' if enabled else 'pause'}",
        )
        try:
            await self._async_request("PUT", url, {})
        except SyncError as err:
            if "HTTP 400" not in str(err):
                raise
            await self._async_request("PUT", url, None)  # some endpoints want no body

    # --- internals ---------------------------------------------------------

    def _set_error(self, state: SyncState, message: str) -> None:
        if state.error != message:
            _LOGGER.warning("Schedule sync: %s", message)
        state.status = STATUS_ERROR
        state.error = message

    async def _async_get_ha_schedule(self, entity_id: str) -> dict[str, Any]:
        state = self.hass.states.get(entity_id)
        if state is None or state.attributes.get("restored"):
            raise ScheduleError(
                "this schedule helper doesn't exist (it may have been deleted); "
                "pick another in the thermostat's Schedule source"
            )
        response = await self.hass.services.async_call(
            "schedule",
            "get_schedule",
            target={"entity_id": entity_id},
            blocking=True,
            return_response=True,
        )
        if not response or entity_id not in response:
            raise ScheduleError("Home Assistant returned no schedule")
        return response[entity_id]

    async def _async_push(
        self,
        location: LyricLocation,
        device: LyricDevice,
        state: SyncState,
        periods: dict,
    ) -> None:
        current = await self._async_read(location, device, state)
        if same_schedule(current.periods(), periods):
            _LOGGER.debug("%s already has this schedule; nothing sent", device.name)
            return

        payload, warnings = build_device_schedule(current, periods)
        state.warnings = [*state.warnings, *warnings]
        await self._async_request(
            "POST", self._url(location, device, self._schedule_query or ""), payload
        )

        # The cloud can take a moment to reflect a write, so check a few times.
        for _attempt in range(VERIFY_ATTEMPTS):
            await asyncio.sleep(VERIFY_DELAY_SECONDS)
            after = await self._async_read(location, device, state)
            if same_schedule(after.periods(), periods):
                return
        raise SyncError(
            "The thermostat accepted the schedule but didn't apply it. "
            f"It now has: {describe(after.periods())}"
        )

    async def _async_read(
        self, location: LyricLocation, device: LyricDevice, state: SyncState
    ) -> DeviceSchedule:
        data = await self._async_get_schedule(location, device)
        state.thermostat_schedule_raw = data
        state.thermostat_read_at = dt_util.utcnow()
        try:
            parsed = parse_device_schedule(data)
        except ScheduleFormatError as err:
            state.thermostat_schedule = None
            state.thermostat_read_error = str(err)
            raise
        state.thermostat_schedule = describe(parsed.periods())
        state.thermostat_read_error = None
        return parsed

    def _url(
        self,
        location: LyricLocation,
        device: LyricDevice,
        extra: str = "",
        path: str = "",
    ) -> str:
        return (
            f"{BASE_URL}/devices/schedule/{device.device_id}{path}"
            f"?apikey={self._coordinator.data.client_id}"
            f"&locationId={location.location_id}{extra}"
        )

    async def _async_get_schedule(self, location: LyricLocation, device: LyricDevice) -> Any:
        """GET the schedule, finding which query parameters the API accepts."""
        if self._schedule_query is not None:
            return await self._async_request(
                "GET", self._url(location, device, self._schedule_query)
            )
        failures = []
        for extra in SCHEDULE_QUERY_VARIANTS:
            try:
                data = await self._async_request("GET", self._url(location, device, extra))
            except SyncError as err:
                if "HTTP 400" not in str(err):
                    raise
                failures.append(f"{extra.lstrip('&') or 'no type'}: {err}")
                continue
            self._schedule_query = extra
            _LOGGER.info(
                "Schedule endpoint accepts '%s'", extra.lstrip("&") or "no type parameter"
            )
            return data
        raise SyncError("GET schedule was rejected in every known form: " + "; ".join(failures))

    async def _async_request(
        self, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> Any:
        token = await self._client.async_get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with self._session.request(
            method, url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise SyncError(
                    f"{method} schedule failed: HTTP {response.status} {text[:300]}"
                )
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            raise SyncError(f"{method} schedule returned non-JSON: {text[:300]}") from err
