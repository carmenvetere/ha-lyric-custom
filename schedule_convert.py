"""Convert Home Assistant schedule helpers to Lyric thermostat schedules.

This module has no Home Assistant imports so it can be tested on its own.

A Home Assistant schedule helper is a list of time blocks per weekday. Each
block is one thermostat period: its start time is the period start, and its
custom data holds the settings, e.g.::

    periodType: Wake          # optional: Wake/Away/Home/Sleep slot to use
    heatSetPoint: 68          # required (also accepted: heat)
    coolSetPoint: 72          # required (also accepted: cool)
    fanSwitchPosition: Auto   # optional: Auto/On/Circulate
    fanSwitchSpeed: 0         # optional
    priorityRooms: office,guest_bedroom   # optional: rooms with priority on

A period lasts until the next one starts, overnight included, which is how
the thermostat itself treats a schedule. Block end times are ignored. Every
block becomes a period exactly as written; the only exception is a block at
00:00 that just continues the previous night with identical settings (a block
can't cross midnight, so such blocks are a common way to write that).

The thermostat side is read-modify-write: we read the thermostat's current
schedule and fill in its own period slots, keeping whatever field names and
extra fields the API returned. That way we only rely on the shape the live
API actually uses.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import time
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import json
import re
from typing import Any

DAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Keys accepted in a schedule block's custom data (compared case-insensitively,
# ignoring underscores and dashes).
HEAT_KEYS = ("heatsetpoint", "heat", "heating", "low")
COOL_KEYS = ("coolsetpoint", "cool", "cooling", "high")
PERIOD_KEYS = ("periodtype", "period", "name")
FAN_POSITION_KEYS = ("fanswitchposition", "fanposition", "fanmode", "fan")
FAN_SPEED_KEYS = ("fanswitchspeed", "fanspeed")
ROOMS_KEYS = ("priorityrooms", "rooms", "priority")


class ScheduleError(Exception):
    """The Home Assistant schedule can't be turned into a thermostat schedule."""


class ScheduleFormatError(Exception):
    """The thermostat's schedule response isn't in a shape we recognize."""


@dataclass(frozen=True)
class Period:
    """One thermostat schedule period.

    fan_position, fan_speed and rooms are None when not specified, meaning
    "leave whatever the thermostat has".
    """

    start: str  # "HH:MM"
    heat: float
    cool: float
    name: str | None = None
    fan_position: str | None = None
    fan_speed: int | None = None
    rooms: tuple[int, ...] | None = None
    rooms_label: str | None = field(default=None, compare=False)

    def same_settings(self, other: Period | None) -> bool:
        """Return True if both periods would make the thermostat behave the same."""
        return other is not None and (
            self.heat,
            self.cool,
            self.fan_position,
            self.fan_speed,
            self.rooms,
        ) == (other.heat, other.cool, other.fan_position, other.fan_speed, other.rooms)

    def describe(self) -> str:
        """Short human-readable form, e.g. 'Wake 08:00 68/72, fan Auto'."""
        label = f"{self.name} " if self.name else ""
        text = f"{label}{self.start} {_fmt(self.heat)}/{_fmt(self.cool)}"
        if self.fan_position is not None:
            text += f" fan {self.fan_position}"
            if self.fan_speed:
                text += f" speed {self.fan_speed}"
        if self.rooms is not None:
            text += f" rooms {self.rooms_label or ','.join(map(str, self.rooms))}"
        return text


@dataclass(frozen=True)
class DeviceLimits:
    """Constraints the thermostat reports about itself."""

    precision: float = 1.0  # 1.0 for Fahrenheit, 0.5 for Celsius
    time_increment: int = 15  # minutes
    min_heat: float | None = None
    max_heat: float | None = None
    min_cool: float | None = None
    max_cool: float | None = None
    deadband: float = 0.0
    fan_modes: tuple[str, ...] = ()  # e.g. ("On", "Auto", "Circulate")
    rooms: dict[str, int] = field(default_factory=dict)  # room name/slug -> id


WeekPeriods = dict[str, list[Period]]


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def slugify(name: Any) -> str:
    """'Front Guest Bedroom' -> 'front_guest_bedroom'."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def _norm_key(key: Any) -> str:
    return str(key).lower().replace("_", "").replace("-", "")


def _ci_get(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Look up the first matching key, ignoring case, underscores and dashes."""
    norm = {_norm_key(k): v for k, v in data.items()}
    for key in keys:
        if key in norm:
            return norm[key]
    return None


def _to_minutes(value: Any) -> int:
    """Return minutes after midnight for a time object or 'HH:MM[:SS]' string."""
    if isinstance(value, time):
        if value == time.max:
            return 24 * 60
        return value.hour * 60 + value.minute
    parts = str(value).split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (IndexError, ValueError) as err:
        raise ScheduleError(f"Can't read time {value!r}") from err


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _round(value: Any, precision: float, label: str, warnings: list[str]) -> float:
    try:
        exact = Decimal(str(value))
    except Exception as err:  # noqa: BLE001
        raise ScheduleError(f"{label}: {value!r} is not a temperature") from err
    step = Decimal(str(precision))
    rounded = (exact / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step
    if rounded != exact:
        warnings.append(
            f"{label}: {_fmt(float(exact))} rounded to {_fmt(float(rounded))} "
            f"(the thermostat uses steps of {_fmt(precision)})"
        )
    return float(rounded)


def blocks_to_periods(
    ha_schedule: dict[str, list[dict[str, Any]]],
    limits: DeviceLimits,
) -> tuple[WeekPeriods, list[str]]:
    """Turn schedule helper blocks into thermostat periods for each day.

    ``ha_schedule`` is what ``schedule.get_schedule`` returns for one entity:
    ``{"monday": [{"from": time, "to": time, "data": {...}}, ...], ...}``.
    Returns the periods per day and a list of warnings (e.g. rounding).
    Raises ScheduleError if the schedule can't be represented.
    """
    warnings: list[str] = []
    raw: WeekPeriods = {}
    for day in DAYS:
        periods: list[Period] = []
        blocks = ha_schedule.get(day.lower()) or []
        for block in sorted(blocks, key=lambda b: _to_minutes(b["from"])):
            start = _to_minutes(block["from"])
            label = f"{day} {_hhmm(start)}"
            if start % limits.time_increment:
                raise ScheduleError(
                    f"{label}: periods must start on {limits.time_increment}-minute "
                    "steps (e.g. :00, :15, :30, :45)"
                )
            data = block.get("data") or {}
            heat = _ci_get(data, HEAT_KEYS)
            cool = _ci_get(data, COOL_KEYS)
            if heat is None or cool is None:
                raise ScheduleError(
                    f"{label}: the block needs heatSetPoint and coolSetPoint in its "
                    "additional data"
                )
            heat = _round(heat, limits.precision, f"{label} heat", warnings)
            cool = _round(cool, limits.precision, f"{label} cool", warnings)
            _check_setpoints(label, heat, cool, limits)
            name = _ci_get(data, PERIOD_KEYS)
            rooms, rooms_label = _parse_rooms(label, _ci_get(data, ROOMS_KEYS), limits)
            periods.append(
                Period(
                    _hhmm(start),
                    heat,
                    cool,
                    str(name) if name else None,
                    _parse_fan_position(label, _ci_get(data, FAN_POSITION_KEYS), limits),
                    _parse_fan_speed(label, _ci_get(data, FAN_SPEED_KEYS)),
                    rooms,
                    rooms_label,
                )
            )
        raw[day] = periods

    if not any(raw.values()):
        raise ScheduleError("The schedule has no time blocks")

    return _drop_midnight_continuations(raw), warnings


def _parse_fan_position(label: str, value: Any, limits: DeviceLimits) -> str | None:
    if value is None or value == "":
        return None
    if not limits.fan_modes:
        return str(value)
    for mode in limits.fan_modes:
        if str(value).lower() == mode.lower():
            return mode
    raise ScheduleError(
        f"{label}: fan position {value!r} isn't one of {', '.join(limits.fan_modes)}"
    )


def _parse_fan_speed(label: str, value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise ScheduleError(f"{label}: fan speed {value!r} isn't a whole number") from err


def _parse_rooms(
    label: str, value: Any, limits: DeviceLimits
) -> tuple[tuple[int, ...] | None, str | None]:
    """'office,guest_bedroom' -> room IDs, matching names case/spacing-insensitively."""
    if value is None or value == "":
        return None, None
    names = [part.strip() for part in str(value).split(",") if part.strip()]
    ids: list[int] = []
    for name in names:
        if name.isdigit() and int(name) in limits.rooms.values():
            ids.append(int(name))
        elif slugify(name) in limits.rooms:
            ids.append(limits.rooms[slugify(name)])
        else:
            known = ", ".join(sorted(limits.rooms)) or "none found"
            raise ScheduleError(
                f"{label}: room {name!r} isn't one of this thermostat's rooms ({known})"
            )
    return tuple(sorted(set(ids))), ",".join(names)


def _check_setpoints(label: str, heat: float, cool: float, limits: DeviceLimits) -> None:
    if limits.min_heat is not None and heat < limits.min_heat:
        raise ScheduleError(f"{label}: heat {_fmt(heat)} is below the minimum {_fmt(limits.min_heat)}")
    if limits.max_heat is not None and heat > limits.max_heat:
        raise ScheduleError(f"{label}: heat {_fmt(heat)} is above the maximum {_fmt(limits.max_heat)}")
    if limits.min_cool is not None and cool < limits.min_cool:
        raise ScheduleError(f"{label}: cool {_fmt(cool)} is below the minimum {_fmt(limits.min_cool)}")
    if limits.max_cool is not None and cool > limits.max_cool:
        raise ScheduleError(f"{label}: cool {_fmt(cool)} is above the maximum {_fmt(limits.max_cool)}")
    if cool - heat < max(limits.deadband, 0):
        raise ScheduleError(
            f"{label}: cool {_fmt(cool)} must be at least {_fmt(limits.deadband)} "
            f"above heat {_fmt(heat)}"
        )


def _drop_midnight_continuations(raw: WeekPeriods) -> WeekPeriods:
    """Drop 00:00 blocks that only continue the previous night unchanged.

    A schedule block can't cross midnight, so a night period is often written
    as 20:00-24:00 plus 00:00-08:00 the next day. On the thermostat the night
    period simply keeps running, so the 00:00 copy is dropped when its settings
    match the period carried over from the previous day (wrapping Sunday into
    Monday). Every other block is kept exactly as written. A day with no blocks
    gets one 00:00 period with the carried-over settings, so no day is empty.
    """
    last_of_week = next(p for day in reversed(DAYS) for p in reversed(raw[day]))
    current = last_of_week
    result: WeekPeriods = {}
    for day in DAYS:
        kept: list[Period] = []
        for period in raw[day]:
            if not kept and period.start == "00:00" and period.same_settings(current):
                continue
            kept.append(period)
        if kept:
            current = kept[-1]
        else:
            kept.append(
                Period(
                    "00:00",
                    current.heat,
                    current.cool,
                    current.name,
                    current.fan_position,
                    current.fan_speed,
                    current.rooms,
                    current.rooms_label,
                )
            )
        result[day] = kept
    return result


def fingerprint(periods: WeekPeriods) -> str:
    """Stable hash of a weekly schedule, used to detect changes."""
    canonical = {
        day: [
            [p.start, p.heat, p.cool, p.name, p.fan_position, p.fan_speed,
             list(p.rooms) if p.rooms is not None else None]
            for p in periods.get(day, [])
        ]
        for day in DAYS
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]


def describe(periods: WeekPeriods) -> dict[str, str]:
    """Per-day summary, e.g. {'Monday': 'Wake 08:00 68/72, Sleep 20:00 66/74'}."""
    return {
        day: ", ".join(p.describe() for p in periods.get(day, [])) or "(none)"
        for day in DAYS
    }


# --- Thermostat side -----------------------------------------------------


def _find_key(data: dict[str, Any], *names: str) -> str | None:
    lowered = {str(k).lower(): k for k in data}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _find_room_list(slot: dict[str, Any]) -> tuple[str, str] | None:
    """Locate a per-period room priority list: (priority key, room list key).

    Looks for a priority object holding a list of room IDs, e.g.
    {"priority": {"priorityType": "PickARoom", "selectedRooms": [0, 2]}}.
    """
    for key, value in slot.items():
        if "priority" in str(key).lower() and isinstance(value, dict):
            list_key = _find_key(value, "selectedRooms", "rooms", "roomIds")
            if list_key is not None and isinstance(value[list_key], list):
                return key, list_key
    return None


@dataclass
class DeviceSchedule:
    """A thermostat schedule as returned by the API, plus where things live."""

    payload: dict[str, Any]
    days: dict[str, list[dict[str, Any]]]  # day name -> period slot dicts (live)
    keys: dict[str, str]  # logical field -> actual key used by the API

    @property
    def slots_per_day(self) -> int:
        return min(len(slots) for slots in self.days.values())

    def periods(self) -> WeekPeriods:
        """Active (not cancelled) periods per day, in time order."""
        k = self.keys
        result: WeekPeriods = {}
        for day, slots in self.days.items():
            active = []
            for slot in slots:
                if slot.get(k["cancelled"]) is True:
                    continue
                start = slot.get(k["start"])
                if start is None:
                    continue
                fan_position = fan_speed = None
                if (fan_key := _find_key(slot, "fanSwitch")) and isinstance(slot[fan_key], dict):
                    fan = slot[fan_key]
                    if (pos_key := _find_key(fan, "position")) is not None:
                        fan_position = fan[pos_key]
                    if (speed_key := _find_key(fan, "speed")) is not None:
                        fan_speed = fan[speed_key]
                rooms = None
                if (found := _find_room_list(slot)) is not None:
                    rooms = tuple(sorted(slot[found[0]][found[1]]))
                active.append(
                    Period(
                        _hhmm(_to_minutes(start)),
                        float(slot.get(k["heat"])),
                        float(slot.get(k["cool"])),
                        slot.get(k["type"]),
                        fan_position,
                        fan_speed,
                        rooms,
                    )
                )
            result[day] = sorted(active, key=lambda p: p.start)
        return result


def parse_device_schedule(payload: Any) -> DeviceSchedule:
    """Locate the weekly period slots in a GET /devices/schedule response."""
    if not isinstance(payload, dict):
        raise ScheduleFormatError(f"Expected an object, got {type(payload).__name__}")
    timed_key = _find_key(payload, "timedSchedule")
    if timed_key is None or not isinstance(payload[timed_key], dict):
        raise ScheduleFormatError(
            "No 'timedSchedule' in the thermostat's schedule (it may be on a "
            f"geofence schedule). Top-level keys: {sorted(payload)}"
        )
    timed = payload[timed_key]
    days_key = _find_key(timed, "days")
    if days_key is None or not isinstance(timed[days_key], list):
        raise ScheduleFormatError(f"No 'days' list in timedSchedule. Keys: {sorted(timed)}")

    days: dict[str, list[dict[str, Any]]] = {}
    keys: dict[str, str] = {}
    for entry in timed[days_key]:
        day_key = _find_key(entry, "day")
        periods_key = _find_key(entry, "periods")
        if day_key is None or periods_key is None:
            raise ScheduleFormatError(f"Day entry without day/periods: {sorted(entry)}")
        day = str(entry[day_key]).capitalize()
        if day not in DAYS:
            raise ScheduleFormatError(f"Unknown day {entry[day_key]!r}")
        slots = entry[periods_key]
        if not slots:
            raise ScheduleFormatError(f"{day} has no period slots")
        days[day] = slots
        for slot in slots:
            for logical, names in (
                ("type", ("periodType",)),
                ("start", ("startTime",)),
                ("heat", ("heatSetPoint", "heatSetpoint")),
                ("cool", ("coolSetPoint", "coolSetpoint")),
                ("cancelled", ("isCancelled",)),
            ):
                if logical not in keys and (found := _find_key(slot, *names)):
                    keys[logical] = found

    missing = [d for d in DAYS if d not in days]
    if missing:
        raise ScheduleFormatError(f"Days missing from the schedule: {missing}")
    for logical in ("start", "heat", "cool", "cancelled"):
        if logical not in keys:
            raise ScheduleFormatError(f"Couldn't find the {logical} field in period slots")
    keys.setdefault("type", "periodType")
    return DeviceSchedule(payload, days, keys)


def build_device_schedule(
    current: DeviceSchedule, periods: WeekPeriods
) -> tuple[dict[str, Any], list[str]]:
    """Return a copy of the thermostat's schedule with our periods filled in.

    Periods go into the thermostat's own slots in time order. If a period is
    named (e.g. 'Sleep') and a later unused slot has that name, unused slots
    in between are cancelled so the thermostat shows the right name.
    Returns the payload and any warnings (e.g. a setting the slots can't hold).
    """
    payload = copy.deepcopy(current.payload)
    fresh = parse_device_schedule(payload)
    k = fresh.keys
    warnings: set[str] = set()
    for day in DAYS:
        slots = fresh.days[day]
        wanted = periods.get(day, [])
        if len(wanted) > len(slots):
            raise ScheduleError(
                f"{day} has {len(wanted)} periods but the thermostat allows {len(slots)}"
            )
        sample_start = next((s.get(k["start"]) for s in slots if s.get(k["start"])), "00:00:00")
        with_seconds = str(sample_start).count(":") == 2
        index = 0
        for n, period in enumerate(wanted):
            remaining_after = len(wanted) - n - 1
            if period.name:
                for j in range(index, len(slots) - remaining_after):
                    if str(slots[j].get(k["type"], "")).lower() == period.name.lower():
                        index = j
                        break
            slot = slots[index]
            slot[k["cancelled"]] = False
            slot[k["start"]] = f"{period.start}:00" if with_seconds else period.start
            slot[k["heat"]] = period.heat
            slot[k["cool"]] = period.cool
            _write_fan(slot, period)
            if period.rooms is not None:
                if (found := _find_room_list(slot)) is not None:
                    priority = slot[found[0]]
                    priority[found[1]] = list(period.rooms)
                    if (type_key := _find_key(priority, "priorityType")) is not None:
                        priority[type_key] = "PickARoom"
                else:
                    warnings.add(
                        "priorityRooms wasn't sent: the thermostat's schedule has no "
                        "per-period room priority field"
                    )
            slot["_used"] = True
            index += 1
        for slot in slots:
            if not slot.pop("_used", False):
                slot[k["cancelled"]] = True
    return payload, sorted(warnings)


def _write_fan(slot: dict[str, Any], period: Period) -> None:
    if period.fan_position is None and period.fan_speed is None:
        return
    fan_key = _find_key(slot, "fanSwitch") or "fanSwitch"
    fan = slot.get(fan_key)
    if not isinstance(fan, dict):
        fan = slot[fan_key] = {}
    if period.fan_position is not None:
        fan[_find_key(fan, "position") or "position"] = period.fan_position
    if period.fan_speed is not None:
        fan[_find_key(fan, "speed") or "speed"] = period.fan_speed


def same_schedule(actual: WeekPeriods, wanted: WeekPeriods) -> bool:
    """Return True if the thermostat's periods match what we want.

    Start times and temperatures must match. Fan and rooms are compared only
    where the wanted period sets them and the thermostat reports them.
    Period names are ignored.
    """
    for day in DAYS:
        have, want = actual.get(day, []), wanted.get(day, [])
        if len(have) != len(want):
            return False
        for a, w in zip(have, want):
            if (a.start, a.heat, a.cool) != (w.start, w.heat, w.cool):
                return False
            if w.fan_position is not None and a.fan_position is not None:
                if str(a.fan_position).lower() != w.fan_position.lower():
                    return False
            if w.fan_speed is not None and a.fan_speed is not None:
                if int(a.fan_speed) != w.fan_speed:
                    return False
            if w.rooms is not None and a.rooms is not None and a.rooms != w.rooms:
                return False
    return True
