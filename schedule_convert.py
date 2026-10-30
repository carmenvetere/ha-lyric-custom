"""Convert Home Assistant schedule helpers to Lyric thermostat schedules.

This module has no Home Assistant imports so it can be tested on its own.

A Home Assistant schedule helper is a list of time blocks per weekday. Each
block's start time becomes a thermostat period, and the block's custom data
holds the temperatures, e.g. ``{"heat": 68, "cool": 72, "period": "Wake"}``.
A period lasts until the next one starts, overnight included, which is how
the thermostat itself treats a schedule. Block end times are ignored.

The thermostat side is read-modify-write: we read the thermostat's current
schedule and fill in its own period slots, keeping whatever field names and
extra fields (fan, sensor priority) the API returned. That way we only rely
on the shape the live API actually uses.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import time
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import json
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

# Keys accepted in a schedule block's custom data.
HEAT_KEYS = ("heat", "heat_setpoint", "heatsetpoint", "heating", "low")
COOL_KEYS = ("cool", "cool_setpoint", "coolsetpoint", "cooling", "high")
PERIOD_KEYS = ("period", "period_type", "periodtype", "name")


class ScheduleError(Exception):
    """The Home Assistant schedule can't be turned into a thermostat schedule."""


class ScheduleFormatError(Exception):
    """The thermostat's schedule response isn't in a shape we recognize."""


@dataclass(frozen=True)
class Period:
    """One thermostat schedule period."""

    start: str  # "HH:MM"
    heat: float
    cool: float
    name: str | None = None

    def same_setpoints(self, other: Period | None) -> bool:
        """Return True if both periods hold the same temperatures."""
        return other is not None and (self.heat, self.cool) == (other.heat, other.cool)

    def describe(self) -> str:
        """Short human-readable form, e.g. 'Wake 08:00 68/72'."""
        label = f"{self.name} " if self.name else ""
        return f"{label}{self.start} {_fmt(self.heat)}/{_fmt(self.cool)}"


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


WeekPeriods = dict[str, list[Period]]


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _ci_get(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Case/underscore-insensitive lookup of the first matching key."""
    norm = {str(k).lower().replace("-", "_"): v for k, v in data.items()}
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
                    f"{label}: the block needs 'heat' and 'cool' temperatures in "
                    "its additional data"
                )
            heat = _round(heat, limits.precision, f"{label} heat", warnings)
            cool = _round(cool, limits.precision, f"{label} cool", warnings)
            _check_setpoints(label, heat, cool, limits)
            name = _ci_get(data, PERIOD_KEYS)
            periods.append(Period(_hhmm(start), heat, cool, str(name) if name else None))
        raw[day] = periods

    if not any(raw.values()):
        raise ScheduleError("The schedule has no time blocks")

    return _drop_redundant(raw), warnings


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


def _drop_redundant(raw: WeekPeriods) -> WeekPeriods:
    """Remove periods that don't change the temperatures already in effect.

    The setting in effect at midnight is carried over from the previous day,
    wrapping Sunday into Monday. A day that ends up with no periods gets one
    at 00:00 with the carried-over temperatures, so no day is left empty.
    """
    last_of_week = next(p for day in reversed(DAYS) for p in reversed(raw[day]))
    current = last_of_week
    result: WeekPeriods = {}
    for day in DAYS:
        kept: list[Period] = []
        for period in raw[day]:
            if period.same_setpoints(current):
                continue
            kept.append(period)
            current = period
        if not kept:
            kept.append(Period("00:00", current.heat, current.cool, current.name))
        result[day] = kept
    return result


def fingerprint(periods: WeekPeriods) -> str:
    """Stable hash of a weekly schedule, used to detect changes."""
    canonical = {
        day: [[p.start, p.heat, p.cool, p.name] for p in periods.get(day, [])]
        for day in DAYS
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]


def describe(periods: WeekPeriods) -> dict[str, str]:
    """Per-day summary, e.g. {'Monday': 'Wake 08:00 68/72, 20:00 66/74'}."""
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
                active.append(
                    Period(
                        _hhmm(_to_minutes(start)),
                        float(slot.get(k["heat"])),
                        float(slot.get(k["cool"])),
                        slot.get(k["type"]),
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


def build_device_schedule(current: DeviceSchedule, periods: WeekPeriods) -> dict[str, Any]:
    """Return a copy of the thermostat's schedule with our periods filled in.

    Periods go into the thermostat's own slots in time order. If a period is
    named (e.g. 'Sleep') and a later unused slot has that name, unused slots
    in between are cancelled so the thermostat shows the right name.
    """
    payload = copy.deepcopy(current.payload)
    fresh = parse_device_schedule(payload)
    k = fresh.keys
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
            slot["_used"] = True
            index += 1
        for slot in slots:
            if not slot.pop("_used", False):
                slot[k["cancelled"]] = True
    return payload


def same_schedule(a: WeekPeriods, b: WeekPeriods) -> bool:
    """Compare start times and temperatures per day (period names ignored)."""
    def key(periods: WeekPeriods) -> dict[str, list[tuple[str, float, float]]]:
        return {
            day: [(p.start, p.heat, p.cool) for p in periods.get(day, [])]
            for day in DAYS
        }
    return key(a) == key(b)
