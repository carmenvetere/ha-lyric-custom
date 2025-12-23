# Honeywell Lyric Custom Integration Review

## Executive Summary

This review analyzes the current state of the `aiolyric` library, Honeywell/Resideo API, and Home Assistant climate features to identify opportunities for improving a custom Honeywell Lyric integration focused on:

1. **Room Priority** - ✅ Now supported in aiolyric
2. **Emergency Heat** - ⚠️ Partially available in API, not in official HA integration
3. **Fresh Air Ventilation** - ⚠️ Limited API support, requires investigation

---

## 1. aiolyric Library Analysis

### Current Version: 2.0.2 (August 2024)

**Repository:** [timmo001/aiolyric](https://github.com/timmo001/aiolyric)

### Key Methods Available

| Method | Description | Status |
|--------|-------------|--------|
| `get_locations()` | Fetch all available locations | ✅ Available |
| `get_devices(location_id)` | Retrieve devices for a location | ✅ Available |
| `get_thermostat_rooms(location_id, device_id)` | Get room priority data with accessory info | ✅ Available |
| `update_thermostat()` | Adjust mode, setpoints, changeover | ✅ Available |
| `update_fan(location, device, mode)` | Modify fan operation mode | ✅ Available |
| `update_priority(location, device, priority_type, rooms)` | **Configure room priority** | ✅ **NEW in PR #116** |

### Changeable Values Properties (device.py)

```python
# Available in aiolyric ChangeableValues class:
- mode
- heat_cool_mode
- heat_setpoint / cool_setpoint
- end_heat_setpoint / end_cool_setpoint
- thermostat_setpoint_status
- auto_changeover_active
- emergency_heat_active  # ✅ Available!
- next_period_time
```

### Recent Changes (2024-2025)

1. **PR #116 (August 2024)**: Added `update_priority()` endpoint for room priority control
2. **v2.0.0 (April 2024)**: Major refactor with snake_case properties
3. **v1.1.0 (July 2023)**: Added room support for thermostat accessories

### Gap Analysis for aiolyric

| Feature | aiolyric Support | Notes |
|---------|-----------------|-------|
| Room Priority | ✅ Full | `update_priority()` method added |
| Emergency Heat | ⚠️ Read-only | `emergency_heat_active` property exists but no setter method |
| Ventilation/Fresh Air | ❌ Not found | No IAQ or ventilation properties in library |

---

## 2. Honeywell/Resideo API Analysis

### API Base URL
`https://api.honeywellhome.com/v2/`

### Key Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/locations` | GET | Get all locations and devices |
| `/devices/thermostats/{deviceId}` | GET | Get thermostat details |
| `/devices/thermostats/{deviceId}` | POST | Update thermostat settings |
| `/devices/thermostats/{deviceId}/priority` | GET | Get room priority |
| `/devices/thermostats/{deviceId}/priority` | PUT | **Set room priority** |

### Room Priority API (T9/T10 thermostats)

**Endpoint:** `PUT /devices/thermostats/{deviceId}/priority`

```json
{
  "currentPriority": {
    "priorityType": "PickARoom",  // "PickARoom" | "FollowMe" | "WholeHouse"
    "selectedRooms": [0, 1]       // Array of room indices
  }
}
```

**Priority Types:**
- `WholeHouse`: Uses all rooms for temperature averaging
- `FollowMe`: Prioritizes rooms with detected motion
- `PickARoom`: Manually select specific rooms

### Emergency Heat API

**Reading Emergency Heat Status:**
```json
// In thermostat response under settings.specialMode:
{
  "settings": {
    "specialMode": {
      "autoChangeoverActive": false,
      "emergencyHeatActive": true
    }
  }
}
```

**Setting Emergency Heat Mode:**
```json
// POST to /devices/thermostats/{deviceId}
{
  "mode": "EmergencyHeat",
  "heatSetpoint": 70,
  "coolSetpoint": 75,
  "thermostatSetpointStatus": "NoHold"
}
```

**Known Issues:**
- When changing ANY thermostat setting while in heat mode, you must include `emergencyHeatActive` in the request or it will fail with error 400
- `EmergencyHeat` must be listed in `allowedModes` for the device to support it

### Fresh Air Ventilation API

**Current Findings:**
- The T6 Pro supports ventilation control via installer setup (ISU) options
- Fan modes available: `On`, `Auto`, `Circulate`
- No dedicated `freshAirActive` or ventilation endpoint found in public API documentation
- Ventilation control may be handled through the Equipment Interface Module (EIM) for Prestige thermostats

**Potential API fields to investigate:**
- `settings.fan.changeableValues.mode`
- IAQ-related equipment control (may require Prestige/RedLINK setup)

---

## 3. Home Assistant Climate Entity Analysis

### Current Climate Features (2024.12+)

| Feature | Status | Notes |
|---------|--------|-------|
| `set_hvac_mode` | ✅ Standard | heat, cool, auto, off |
| `set_temperature` | ✅ Standard | Single or dual setpoint |
| `set_fan_mode` | ✅ Standard | on, auto, circulate |
| `set_preset_mode` | ✅ Standard | away, eco, etc. |
| `set_aux_heat` | ⚠️ **Deprecated** | Being removed from climate entity |
| `set_swing_horizontal_mode` | ✅ New in 2024.12 | For AC units |
| HVACAction | ✅ Standard | heating, cooling, idle, off |

### Aux Heat Deprecation Notice

> **Important:** The `climate.set_aux_heat` action is being **deprecated** in Home Assistant. The `aux_heat` property will be removed from the climate entity model.

**Recommended Alternative:**
- Create a separate `switch` entity for emergency/aux heat control
- Or use a `select` entity for HVAC mode that includes emergency heat

### Emergency Heat Best Practices

From [Home Assistant Architecture Discussion #932](https://github.com/home-assistant/architecture/discussions/932):

1. **Option A - Switch Entity:** Create an `aux_heat_only` switch that toggles emergency heat mode
2. **Option B - Select Entity:** Create a custom `select` entity with all HVAC modes including "Emergency Heat"
3. **Option C - Service Call:** Implement a custom service like `lyric.set_emergency_heat`

### HVAC Modes Available

```python
class HVACMode(enum):
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    HEAT_COOL = "heat_cool"  # Auto
    AUTO = "auto"
    DRY = "dry"
    FAN_ONLY = "fan_only"
    # Note: No EMERGENCY_HEAT mode in standard enum
```

---

## 4. Recommendations for Custom Integration

### 4.1 Room Priority Implementation

**Status:** ✅ Ready to implement

The aiolyric library now supports room priority via the `update_priority()` method.

**Suggested Implementation:**
```python
# Create a select entity for room priority type
class LyricRoomPrioritySelect(SelectEntity):
    _attr_options = ["Whole House", "Follow Me", "Pick a Room"]

    async def async_select_option(self, option: str) -> None:
        priority_type = {
            "Whole House": "WholeHouse",
            "Follow Me": "FollowMe",
            "Pick a Room": "PickARoom"
        }[option]
        await self.lyric.update_priority(
            location_id, device_id, priority_type, selected_rooms
        )

# Create a multi-select or checkboxes for room selection
# when "Pick a Room" is selected
```

**Additional entities to create:**
- Sensor entities for each room sensor (temperature, humidity, occupancy)
- These are already available via `get_thermostat_rooms()`

### 4.2 Emergency Heat Implementation

**Status:** ⚠️ Requires custom implementation

**Recommended Approach:**

```python
# Option 1: Switch entity (preferred for HA direction)
class LyricEmergencyHeatSwitch(SwitchEntity):
    @property
    def is_on(self) -> bool:
        return self.device.changeable_values.emergency_heat_active

    async def async_turn_on(self) -> None:
        await self.lyric.update_thermostat(
            location_id, device_id,
            mode="EmergencyHeat",
            # Include current setpoints
        )

    async def async_turn_off(self) -> None:
        await self.lyric.update_thermostat(
            location_id, device_id,
            mode="Heat",  # Return to normal heat
        )

# Option 2: Binary sensor to show when aux is ACTIVE
class LyricAuxHeatActiveSensor(BinarySensorEntity):
    """Shows when aux/emergency heat is currently running."""
    _attr_device_class = BinarySensorDeviceClass.RUNNING
```

**API Considerations:**
- Check if `EmergencyHeat` is in `allowed_modes` before enabling
- When updating other settings while in heat mode, include `emergencyHeatActive: false` to avoid API errors

### 4.3 Fresh Air Ventilation Implementation

**Status:** ❓ Requires further API investigation

**Current Options:**

1. **Fan Mode Control (Available Now):**
   ```python
   # Use existing fan modes for basic ventilation
   await self.lyric.update_fan(location_id, device_id, "Circulate")
   ```

2. **Investigate Prestige/IAQ API:**
   - The Prestige thermostats with Equipment Interface Module (EIM) have dedicated ventilation control
   - May require additional API endpoints not in public documentation

3. **Service for Manual Investigation:**
   ```python
   # Create a diagnostic service to inspect full API response
   async def async_get_raw_thermostat_data(self):
       """Return raw API response for debugging."""
       return await self.lyric.get_devices(location_id)
   ```

**Recommended Next Steps for Ventilation:**
1. Capture raw API response from your thermostat
2. Look for any ventilation-related fields in the settings
3. Check if your thermostat model (T6 Pro with U wire) exposes ventilation control

---

## 5. Proposed Integration Architecture

```
custom_components/
└── lyric_extended/
    ├── __init__.py           # Integration setup
    ├── manifest.json         # Dependencies (aiolyric>=2.0.2)
    ├── config_flow.py        # OAuth configuration
    ├── coordinator.py        # Data update coordinator
    ├── const.py              # Constants
    ├── climate.py            # Climate entity (extends base lyric)
    ├── switch.py             # Emergency heat switch
    ├── select.py             # Room priority type selector
    ├── sensor.py             # Room sensors, aux heat status
    ├── binary_sensor.py      # Aux heat active indicator
    └── services.yaml         # Custom services
```

### Custom Services to Consider

```yaml
# services.yaml
set_room_priority:
  description: Set room priority for thermostat
  fields:
    priority_type:
      description: Priority type (WholeHouse, FollowMe, PickARoom)
      required: true
      selector:
        select:
          options:
            - WholeHouse
            - FollowMe
            - PickARoom
    selected_rooms:
      description: List of room indices for PickARoom mode
      required: false
      selector:
        object:

set_emergency_heat:
  description: Enable or disable emergency heat mode
  fields:
    enabled:
      description: Enable emergency heat
      required: true
      selector:
        boolean:
```

---

## 6. Summary of Findings

| Feature | aiolyric | Honeywell API | HA Climate | Recommendation |
|---------|----------|---------------|------------|----------------|
| Room Priority | ✅ v2.0.2 | ✅ Full support | N/A | Implement as Select + Sensors |
| Emergency Heat | ⚠️ Read-only | ✅ Full support | ⚠️ Deprecated | Implement as Switch entity |
| Aux Heat Status | ✅ Property | ✅ Available | ⚠️ Deprecated | Implement as Binary Sensor |
| Fresh Air Vent | ❌ Not found | ❓ Limited/Unknown | N/A | Investigate API further |
| Fan Control | ✅ Full | ✅ Full | ✅ Standard | Use existing methods |

---

## Sources

- [aiolyric GitHub Repository](https://github.com/timmo001/aiolyric)
- [Resideo Developer Portal](https://developer.honeywellhome.com/)
- [Home Assistant Lyric Integration](https://www.home-assistant.io/integrations/lyric/)
- [Home Assistant Climate Entity Docs](https://developers.home-assistant.io/docs/core/entity/climate/)
- [HA Architecture Discussion #932 - Aux Heat Removal](https://github.com/home-assistant/architecture/discussions/932)
- [HA Issue #128605 - EmergencyHeat in AllowedModes](https://github.com/home-assistant/core/issues/128605)
- [HA Issue #132448 - Emergency Heat Mode Missing](https://github.com/home-assistant/core/issues/132448)
- [Honeywell Room Priority Documentation](https://www.honeywellhome.com/blogs/support/what-does-room-prioritization-mean-and-how-does-it-work-using-the-t9-t10-thermostat)
