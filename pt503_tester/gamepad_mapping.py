"""USB gamepad fallback mapping for the verified RAW controller."""

USE_SDL_CONTROLLER = False

# Analog stick -------------------------------------------------------------
# Linux pygame RAW mapping verified on the target controller:
#   Left stick  = axes 0 / 1
#   Right stick horizontal = axis 5 -> PAN
#   Right stick vertical   = axis 2 -> TILT
#
# Browser Gamepad API and pygame RAW numbering are not guaranteed to match.
PAN_AXIS = 5
TILT_AXIS = 2
INVERT_PAN_AXIS = False
INVERT_TILT_AXIS = True

STICK_DEADZONE = 0.15
SPEED_CURVE = 1.50

# D-pad --------------------------------------------------------------------
# Browser Gamepad API에서는 POV가 axes[9]로 보이지만,
# pygame raw joystick API에서는 D-pad를 별도 hat으로 읽으므로 hat 0 사용.
HAT_INDEX: int | None = 0
DPAD_LEFT_BUTTON: int | None = None
DPAD_RIGHT_BUTTON: int | None = None
DPAD_UP_BUTTON: int | None = None
DPAD_DOWN_BUTTON: int | None = None
SPEED_STEP = 1

# Buttons ------------------------------------------------------------------
# Verified RAW order:
# 0=Y, 1=B, 2=A, 3=X, 4=LB, 5=RB, 6=LT, 7=RT,
# 8=BACK, 9=NEXT, 10=L3, 11=R3
BUTTON_GOTO_SELECTION: int | None = 2   # A
BUTTON_STOP: int | None = 1             # B
BUTTON_HOME: int | None = 3             # X
BUTTON_LASER_TOGGLE: int | None = 0     # Y
BUTTON_RECIPE_PREVIOUS: int | None = 4  # LB
BUTTON_RECIPE_NEXT: int | None = 5      # RB

# LT/RT are buttons on this controller, not analog axes.
TRIGGER_POINT_PREVIOUS_AXIS: int | None = None
TRIGGER_POINT_NEXT_AXIS: int | None = None
TRIGGER_POINT_PREVIOUS_BUTTON: int | None = 6  # LT
TRIGGER_POINT_NEXT_BUTTON: int | None = 7      # RT
TRIGGER_THRESHOLD = 0.50

# Compatibility names ------------------------------------------------------
BUTTON_QUERY_POSITION: int | None = None
BUTTON_CALL_PRESET: int | None = None
BUTTON_LASER_PULSE: int | None = None
BUTTON_PRESET_PREVIOUS: int | None = BUTTON_RECIPE_PREVIOUS
BUTTON_PRESET_NEXT: int | None = BUTTON_RECIPE_NEXT
BUTTON_LASER_OFF: int | None = None
BUTTON_TOGGLE_MONITOR: int | None = None

# Communication ------------------------------------------------------------
POLL_INTERVAL_MS = 40
RUNAWAY_RESEND_SECONDS = 3.5

BUTTON_LABELS = {
    BUTTON_GOTO_SELECTION: "A = goto selected recipe point",
    BUTTON_STOP: "B = STOP",
    BUTTON_HOME: "X = goto 0,0",
    BUTTON_LASER_TOGGLE: "Y = laser ON/OFF",
    BUTTON_RECIPE_PREVIOUS: "LB = previous recipe",
    BUTTON_RECIPE_NEXT: "RB = next recipe",
    TRIGGER_POINT_PREVIOUS_BUTTON: "LT = previous point",
    TRIGGER_POINT_NEXT_BUTTON: "RT = next point",
}
