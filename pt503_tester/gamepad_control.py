"""Pure gamepad-to-Pelco motion conversion, kept separate for unit tests."""

from __future__ import annotations

import math

from . import gamepad_mapping as mapping
from .protocol import PanDirection, TiltDirection


def shaped_axis(
    value: float,
    *,
    inverted: bool,
    deadzone: float = mapping.STICK_DEADZONE,
    curve: float = mapping.SPEED_CURVE,
) -> float:
    """Apply inversion, deadzone removal and a precision-friendly curve."""

    value = max(-1.0, min(1.0, float(value)))
    if inverted:
        value = -value
    magnitude = abs(value)
    if magnitude <= deadzone:
        return 0.0
    normalized = (magnitude - deadzone) / (1.0 - deadzone)
    return math.copysign(normalized**curve, value)


def axes_to_motion(
    axes: tuple[float, ...], pan_max_speed: int, tilt_max_speed: int
) -> tuple[PanDirection, TiltDirection, int, int] | None:
    """Convert configured stick axes into one Pelco-D jog command."""

    def read(index: int) -> float:
        return axes[index] if 0 <= index < len(axes) else 0.0

    pan_level = shaped_axis(
        read(mapping.PAN_AXIS), inverted=mapping.INVERT_PAN_AXIS
    )
    tilt_level = shaped_axis(
        read(mapping.TILT_AXIS), inverted=mapping.INVERT_TILT_AXIS
    )
    if pan_level == 0.0 and tilt_level == 0.0:
        return None

    pan_direction = (
        PanDirection.RIGHT
        if pan_level > 0
        else PanDirection.LEFT
        if pan_level < 0
        else PanDirection.STOP
    )
    tilt_direction = (
        TiltDirection.UP
        if tilt_level > 0
        else TiltDirection.DOWN
        if tilt_level < 0
        else TiltDirection.STOP
    )
    pan_speed = (
        max(1, round(abs(pan_level) * pan_max_speed)) if pan_level else 0
    )
    tilt_speed = (
        max(1, round(abs(tilt_level) * tilt_max_speed)) if tilt_level else 0
    )
    return pan_direction, tilt_direction, pan_speed, tilt_speed
