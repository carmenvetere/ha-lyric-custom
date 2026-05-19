"""Constants for the Honeywell Lyric integration."""

from aiohttp.client_exceptions import ClientResponseError
from aiolyric.exceptions import LyricAuthenticationException, LyricException

DOMAIN = "lyric_custom"

OAUTH2_AUTHORIZE = "https://api.honeywellhome.com/oauth2/authorize"
OAUTH2_TOKEN = "https://api.honeywellhome.com/oauth2/token"

PRESET_NO_HOLD = "NoHold"
PRESET_TEMPORARY_HOLD = "TemporaryHold"
PRESET_HOLD_UNTIL = "HoldUntil"
PRESET_PERMANENT_HOLD = "PermanentHold"
PRESET_VACATION_HOLD = "VacationHold"

# HVAC Mode Constants
LYRIC_HVAC_MODE_EMERGENCY_HEAT = "EmergencyHeat"

LYRIC_EXCEPTIONS = (
    LyricAuthenticationException,
    LyricException,
    ClientResponseError,
)

# Constants related to room priority
PRIORITY_ROOM = "RoomPriority"
PRIORITY_HOME = "Home"
PRIORITY_FOLLOW_SCHEDULE = "FollowSchedule"
PRIORITY_PICK_A_ROOM = "PickARoom"