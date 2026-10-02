"""Pelco-D packet building and response decoding for BIT-PT503/PT510.

The manufacturer supplied command sheet uses standard Pelco-D frames plus a
small set of vendor mappings.  This module has no Qt or serial dependency, so
the protocol can be unit-tested without hardware.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from dataclasses import dataclass
from enum import Enum

SYNC = 0xFF
MIN_ADDRESS = 1
MAX_ADDRESS = 255
MIN_SPEED = 0x00
MAX_SPEED = 0x3F
AUTO_PAN_SPEED = 0x3F
AUTO_TILT_SPEED = MAX_SPEED
MIN_MANUAL_SPEED_LEVEL = 1
MAX_MANUAL_SPEED_LEVEL = 8


def manual_speed_percent(level: int) -> int:
    """Return the displayed percentage for a manual speed level (1..8)."""
    if not MIN_MANUAL_SPEED_LEVEL <= level <= MAX_MANUAL_SPEED_LEVEL:
        raise ProtocolError("수동 속도 단계는 1~8 범위여야 합니다.")
    return round(1 + (level - 1) * 99 / (MAX_MANUAL_SPEED_LEVEL - 1))


def manual_speed_value(level: int, maximum: int = MAX_SPEED) -> int:
    """Map a manual 1..8 level to a non-zero Pelco speed value."""
    if maximum < 1:
        raise ProtocolError("수동 속도 최댓값은 1 이상이어야 합니다.")
    return max(1, round(maximum * manual_speed_percent(level) / 100))


def auto_position_speed(address: int) -> "OutgoingCommand":
    """Select the fixed maximum speed used by every non-manual move."""
    return set_position_speed(address, AUTO_PAN_SPEED, AUTO_TILT_SPEED)


class ProtocolError(ValueError):
    """Raised when a Pelco-D value or frame is invalid."""


class PanDirection(Enum):
    STOP = 0
    RIGHT = 0x02
    LEFT = 0x04


class TiltDirection(Enum):
    STOP = 0
    UP = 0x08
    DOWN = 0x10


@dataclass(frozen=True)
class OutgoingCommand:
    data: bytes
    description: str
    category: str = "command"


@dataclass(frozen=True)
class DecodedResponse:
    data: bytes
    description: str
    kind: str
    address: int | None = None
    value: float | int | None = None
    checksum_ok: bool | None = None


def _byte(value: int, name: str) -> int:
    if not 0 <= int(value) <= 0xFF:
        raise ProtocolError(f"{name} 값은 0~255 범위여야 합니다: {value}")
    return int(value)


def validate_address(address: int) -> int:
    if not MIN_ADDRESS <= int(address) <= MAX_ADDRESS:
        raise ProtocolError(f"주소는 {MIN_ADDRESS}~{MAX_ADDRESS} 범위여야 합니다.")
    return int(address)


def pelco_checksum(payload: Iterable[int]) -> int:
    """Return the modulo-256 sum used by Pelco-D."""

    return sum(int(item) for item in payload) & 0xFF


def build_frame(
    address: int,
    command1: int,
    command2: int,
    data1: int,
    data2: int,
) -> bytes:
    """Build a seven-byte Pelco-D command frame."""

    address = validate_address(address)
    payload = bytes(
        (
            address,
            _byte(command1, "Command1"),
            _byte(command2, "Command2"),
            _byte(data1, "Data1"),
            _byte(data2, "Data2"),
        )
    )
    return bytes((SYNC,)) + payload + bytes((pelco_checksum(payload),))


def verify_frame_checksum(frame: bytes) -> bool:
    return (
        len(frame) == 7 and frame[0] == SYNC and pelco_checksum(frame[1:6]) == frame[6]
    )


def _command(
    address: int,
    command1: int,
    command2: int,
    data1: int,
    data2: int,
    description: str,
    category: str = "command",
) -> OutgoingCommand:
    return OutgoingCommand(
        build_frame(address, command1, command2, data1, data2),
        description,
        category,
    )


def stop(address: int) -> OutgoingCommand:
    return _command(address, 0, 0, 0, 0, "모든 Pan/Tilt/렌즈 동작 정지", "safety")


def manual_motion(
    address: int,
    pan: PanDirection = PanDirection.STOP,
    tilt: TiltDirection = TiltDirection.STOP,
    pan_speed: int = 0x20,
    tilt_speed: int = 0x20,
) -> OutgoingCommand:
    """Build a BIT-PT503/PT510 manual Pelco-D motion command.

    BIT's command sheet defines the speed byte of a stopped axis as ``0x00``
    (for example: Pan Right = ``FF ADD 00 02 PAN_SPEED 00 SUM``).  Some generic
    Pelco-D implementations ignore the unused speed byte, but relying on that
    behavior can make vendor firmware re-evaluate the stopped axis and produce
    uneven manual motion.  Always zero the speed byte for an inactive axis.
    """

    if not MIN_SPEED <= pan_speed <= MAX_SPEED:
        raise ProtocolError("Pan 속도는 0x00~0x3F 범위여야 합니다.")
    if not MIN_SPEED <= tilt_speed <= MAX_SPEED:
        raise ProtocolError("Tilt 속도는 0x00~0x3F 범위여야 합니다.")

    if pan is PanDirection.STOP and tilt is TiltDirection.STOP:
        return stop(address)

    effective_pan_speed = pan_speed if pan is not PanDirection.STOP else 0
    effective_tilt_speed = tilt_speed if tilt is not TiltDirection.STOP else 0

    command2 = pan.value | tilt.value
    names = []
    if pan is not PanDirection.STOP:
        names.append(f"Pan {pan.name} 속도 {effective_pan_speed}")
    if tilt is not TiltDirection.STOP:
        names.append(f"Tilt {tilt.name} 속도 {effective_tilt_speed}")

    return _command(
        address,
        0,
        command2,
        effective_pan_speed,
        effective_tilt_speed,
        ", ".join(names),
        "motion",
    )


def lens_motion(address: int, action: str) -> OutgoingCommand:
    """Create a standard Pelco-D lens command.

    These functions only work when a mounted camera/controller implements the
    corresponding Pelco-D lens operation.
    """

    mapping = {
        "zoom_in": (0x00, 0x20, "Zoom In"),
        "zoom_out": (0x00, 0x40, "Zoom Out"),
        "focus_near": (0x00, 0x80, "Focus Near"),
        "focus_far": (0x01, 0x00, "Focus Far"),
        "iris_open": (0x02, 0x00, "Iris Open"),
        "iris_close": (0x04, 0x00, "Iris Close"),
    }
    if action not in mapping:
        raise ProtocolError(f"알 수 없는 렌즈 동작입니다: {action}")
    command1, command2, label = mapping[action]
    return _command(address, command1, command2, 0, 0, label, "lens")


def query_pan(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x51, 0, 0, "현재 Pan 절대각도 조회", "query")


def query_tilt(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x53, 0, 0, "현재 Tilt 절대각도 조회", "query")


def query_zoom(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x55, 0, 0, "현재 Zoom 위치 조회", "query")


def query_focus(address: int) -> OutgoingCommand:
    """Query focus using the PT503/PT510 manufacturer command sheet."""

    return _command(address, 0, 0x65, 0, 0, "현재 Focus 위치 조회(제조사 명령)", "query")


def query_device_type(address: int) -> OutgoingCommand:
    """Send the standard Pelco-D device-type query."""

    return _command(address, 0, 0x6B, 0, 0, "Pelco-D 장치 유형 조회", "query")


def encode_pan_degrees(degrees: float) -> int:
    if not 0.0 <= degrees <= 359.99:
        raise ProtocolError("Pan 목표각은 0.00~359.99° 범위여야 합니다.")
    return round(degrees * 100.0)


def encode_tilt_display_degrees(degrees: float) -> int:
    """Encode a human-friendly Pelco tilt angle.

    The UI convention follows common Pelco displays: positive is above the
    horizon and negative is below.  Pelco wire values wrap above-horizon
    angles around 360 degrees (for example +45° -> 31500).
    """

    if not -60.0 <= degrees <= 60.0:
        raise ProtocolError("Tilt 목표각은 -60.00~+60.00° 범위여야 합니다.")
    if degrees > 0:
        return round((360.0 - degrees) * 100.0)
    return round(abs(degrees) * 100.0)


def decode_tilt_display_degrees(raw: int) -> float:
    raw %= 36000
    if raw > 18000:
        return (36000 - raw) / 100.0
    return -(raw / 100.0)


def set_pan_position(address: int, degrees: float) -> OutgoingCommand:
    raw = encode_pan_degrees(degrees)
    return _command(
        address,
        0,
        0x4B,
        raw >> 8,
        raw & 0xFF,
        f"Pan 절대각도 {degrees:.2f}° 이동",
        "position",
    )


def set_tilt_position(address: int, degrees: float) -> OutgoingCommand:
    raw = encode_tilt_display_degrees(degrees)
    return _command(
        address,
        0,
        0x4D,
        raw >> 8,
        raw & 0xFF,
        f"Tilt 표시각 {degrees:+.2f}° 이동(+위/-아래)",
        "position",
    )


def set_position_speed(
    address: int, pan_speed: int, tilt_speed: int
) -> OutgoingCommand:
    return _command(
        address,
        0,
        0x5F,
        _byte(pan_speed, "Pan 위치이동 속도"),
        _byte(tilt_speed, "Tilt 위치이동 속도"),
        f"위치이동 속도 설정 Pan={pan_speed}, Tilt={tilt_speed}",
        "vendor",
    )


def set_preset(address: int, preset: int) -> OutgoingCommand:
    preset = _byte(preset, "Preset")
    return _command(address, 0, 0x03, 0, preset, f"프리셋 {preset} 저장", "preset")


def call_preset(address: int, preset: int) -> OutgoingCommand:
    preset = _byte(preset, "Preset")
    return _command(address, 0, 0x07, 0, preset, f"프리셋 {preset} 호출", "preset")


def clear_preset(address: int, preset: int) -> OutgoingCommand:
    preset = _byte(preset, "Preset")
    return _command(address, 0, 0x05, 0, preset, f"프리셋 {preset} 삭제", "preset")


def set_auxiliary(address: int, number: int, enabled: bool) -> OutgoingCommand:
    number = _byte(number, "Aux 번호")
    opcode = 0x09 if enabled else 0x0B
    state = "ON" if enabled else "OFF"
    return _command(address, 0, opcode, 0, number, f"Aux {number} {state}", "aux")


def zone_scan(address: int, enabled: bool) -> OutgoingCommand:
    opcode = 0x1B if enabled else 0x1D
    state = "시작" if enabled else "정지"
    return _command(address, 0, opcode, 0, 0, f"Pelco Zone/Line Scan {state}", "scan")


def set_line_scan_point(address: int, start: bool) -> OutgoingCommand:
    # Vendor mapping: magic preset 0x6E=start, 0x6F=end.
    point = 0x6E if start else 0x6F
    label = "시작점" if start else "종료점"
    return _command(address, 0, 0x03, 0, point, f"라인스캔 {label} 저장", "vendor")


def vendor_line_scan(address: int, enabled: bool) -> OutgoingCommand:
    # Vendor mapping: call magic preset 0x6E=start, 0x6F=stop.
    point = 0x6E if enabled else 0x6F
    label = "시작" if enabled else "정지"
    return _command(address, 0, 0x07, 0, point, f"제조사 라인스캔 {label}", "vendor")


def set_scan_speed(address: int, pan_speed: int, tilt_speed: int) -> OutgoingCommand:
    return _command(
        address,
        0,
        0x31,
        _byte(pan_speed, "Pan Scan 속도"),
        _byte(tilt_speed, "Tilt Scan 속도"),
        f"라인스캔 속도 설정 Pan={pan_speed}, Tilt={tilt_speed}",
        "vendor",
    )


def set_cruise_speed(address: int, pan_speed: int, tilt_speed: int) -> OutgoingCommand:
    return _command(
        address,
        0,
        0x67,
        _byte(pan_speed, "Pan Cruise 속도"),
        _byte(tilt_speed, "Tilt Cruise 속도"),
        f"크루징 속도 설정 Pan={pan_speed}, Tilt={tilt_speed}",
        "vendor",
    )


def start_cruise(address: int, track: int) -> OutgoingCommand:
    """Start a vendor cruise track using the supplied PT503/PT510 mapping."""

    if not 1 <= track <= 8:
        raise ProtocolError("크루징 경로는 1~8 범위여야 합니다.")
    return _command(
        address,
        0,
        0x07,
        0,
        track,
        f"크루징 경로 {track} 시작(제조사 매핑)",
        "vendor",
    )


def adjust_preset_speed(address: int, faster: bool) -> OutgoingCommand:
    opcode = 0x03 if faster else 0x05
    label = "증가" if faster else "감소"
    return _command(address, 0, opcode, 0, 0x61, f"프리셋 이동속도 {label}", "vendor")


def adjust_scan_speed(address: int, faster: bool) -> OutgoingCommand:
    opcode = 0x03 if faster else 0x05
    label = "증가" if faster else "감소"
    return _command(address, 0, opcode, 0, 0x62, f"스캔 속도 {label}", "vendor")


def auto_home(address: int, enabled: bool) -> OutgoingCommand:
    code = 0x70 if enabled else 0x71
    label = "활성화" if enabled else "비활성화"
    return _command(address, 0, 0x07, 0, code, f"Auto Home {label}", "vendor")


def power_on_self_check(address: int, enabled: bool) -> OutgoingCommand:
    """Configure the optional/customized power-on self-check mapping.

    The supplied manufacturer sheet writes ``03(05) 00 77`` and explicitly
    marks it as customized.  It is therefore exposed as an experimental
    vendor setting and must not be confused with inactivity Auto Home.
    """

    opcode = 0x03 if enabled else 0x05
    label = "활성화" if enabled else "비활성화"
    return _command(
        address,
        0,
        opcode,
        0,
        0x77,
        f"전원 인가 Self-check {label}(맞춤형 펌웨어 명령)",
        "maintenance",
    )


def home_then_preset1(address: int) -> OutgoingCommand:
    return _command(
        address, 0, 0x03, 0, 0x75, "Auto Home 후 프리셋 1 호출 설정", "vendor"
    )


def home_then_cruise1(address: int) -> OutgoingCommand:
    return _command(
        address, 0, 0x05, 0, 0x75, "Auto Home 후 크루징 경로 1 설정", "vendor"
    )


def self_check(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x07, 0, 0x77, "원격 Self-check 실행", "maintenance")


def remote_restart(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x07, 0, 0x78, "원격 재시작", "maintenance")


def factory_default(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x29, 0, 0, "공장 초기화 요청", "maintenance")


@dataclass(frozen=True)
class BuiltProtocolCommand:
    commands: tuple[OutgoingCommand, ...]
    result: dict[str, Any]
    requires_confirm: bool = False
    wait_ms: int = 80


_PROTOCOL_NAMED_COMMANDS = [
    "protocol.command",
    "motion.relative",
    "zoom.set",
    "focus.set",
    "pelco.flip",
    "pelco.zero_pan",
    "pelco.remote_reset",
    "zone.set_start",
    "zone.set_end",
    "screen.write_char",
    "screen.write_text",
    "screen.clear",
    "alarm.ack",
    "pattern.record_start",
    "pattern.record_stop",
    "pattern.run",
    "lens.zoom_speed",
    "lens.focus_speed",
    "camera.power",
    "camera.scan",
    "camera.auto_focus",
    "camera.auto_iris",
    "camera.agc",
    "camera.backlight",
    "camera.auto_white_balance",
    "camera.phase_delay",
    "camera.shutter",
    "camera.adjust",
    "preset.scan",
    "zero.set",
    "magnification.set",
    "magnification.query",
    "echo.activate",
    "device.remote_baud",
    "device.query_diagnostics",
    "cruise.interval",
    "home.time",
]


def protocol_named_command_names() -> list[str]:
    return list(_PROTOCOL_NAMED_COMMANDS)


def _u16(value: int, name: str) -> int:
    if not 0 <= int(value) <= 0xFFFF:
        raise ProtocolError(f"{name} 값은 0~65535 범위여야 합니다: {value}")
    return int(value)


def _i16(value: int, name: str) -> int:
    if not -0x8000 <= int(value) <= 0x7FFF:
        raise ProtocolError(f"{name} 값은 -32768~32767 범위여야 합니다: {value}")
    return int(value) & 0xFFFF


def _split_u16(raw: int) -> tuple[int, int]:
    raw = _u16(raw, "16-bit")
    return raw >> 8, raw & 0xFF


def _signed_hundredths_from_degrees(degrees: float, name: str) -> int:
    return _i16(round(float(degrees) * 100.0), name)


def set_relative_pan_position(address: int, degrees: float) -> OutgoingCommand:
    raw = _signed_hundredths_from_degrees(degrees, "Pan 상대각")
    msb, lsb = _split_u16(raw)
    return _command(
        address,
        0,
        0x47,
        msb,
        lsb,
        f"Pan 상대각 {degrees:+.2f}° 이동(제조사 확장)",
        "vendor",
    )


def set_relative_tilt_position(address: int, degrees: float) -> OutgoingCommand:
    raw = _signed_hundredths_from_degrees(degrees, "Tilt 상대각")
    msb, lsb = _split_u16(raw)
    return _command(
        address,
        0,
        0x49,
        msb,
        lsb,
        f"Tilt 상대각 {degrees:+.2f}° 이동(제조사 확장)",
        "vendor",
    )


def set_zoom_position(address: int, value: int) -> OutgoingCommand:
    msb, lsb = _split_u16(value)
    return _command(address, 0, 0x4F, msb, lsb, f"Zoom 위치 Raw={value} 설정", "lens")


def set_focus_position(address: int, value: int) -> OutgoingCommand:
    msb, lsb = _split_u16(value)
    return _command(address, 0, 0x6F, msb, lsb, f"Focus 위치 Raw={value} 설정(제조사 확장)", "lens")


def flip(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x07, 0, 0x21, "Pelco Flip 180°", "preset")


def goto_zero_pan(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x07, 0, 0x22, "Pelco Zero Pan 위치로 이동", "preset")


def remote_reset(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x0F, 0, 0, "Pelco 표준 Remote Reset", "maintenance")


def set_zone_point(address: int, zone: int, start: bool) -> OutgoingCommand:
    zone = _byte(zone, "Zone")
    opcode = 0x11 if start else 0x13
    label = "시작점" if start else "종료점"
    return _command(address, 0, opcode, 0, zone, f"Zone {zone} {label} 저장", "zone")


def write_screen_character(address: int, column: int, char: str | int) -> OutgoingCommand:
    column = _byte(column, "Screen column")
    if not 0 <= column <= 39:
        raise ProtocolError("Screen column은 0~39 범위여야 합니다.")
    if isinstance(char, str):
        if len(char) != 1:
            raise ProtocolError("char는 한 글자여야 합니다.")
        code = ord(char)
    else:
        code = int(char)
    code = _byte(code, "ASCII char")
    return _command(address, 0, 0x15, column, code, f"Screen column {column} 문자 0x{code:02X} 쓰기", "screen")


def clear_screen(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x17, 0, 0, "Screen clear", "screen")


def acknowledge_alarm(address: int, alarm: int, subopcode: int = 0) -> OutgoingCommand:
    return _command(address, _byte(subopcode, "Alarm subopcode"), 0x19, 0, _byte(alarm, "Alarm"), f"Alarm {alarm} acknowledge", "alarm")


def pattern_record_start(address: int, pattern: int) -> OutgoingCommand:
    return _command(address, 0, 0x1F, 0, _byte(pattern, "Pattern"), f"Pattern {pattern} 기록 시작", "pattern")


def pattern_record_stop(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x21, 0, 0, "Pattern 기록 종료", "pattern")


def pattern_run(address: int, pattern: int) -> OutgoingCommand:
    return _command(address, 0, 0x23, 0, _byte(pattern, "Pattern"), f"Pattern {pattern} 실행", "pattern")


def set_zoom_speed(address: int, speed: int) -> OutgoingCommand:
    return _command(address, 0, 0x25, 0, _byte(speed, "Zoom speed"), f"Zoom speed {speed} 설정", "lens")


def set_focus_speed(address: int, speed: int) -> OutgoingCommand:
    return _command(address, 0, 0x27, 0, _byte(speed, "Focus speed"), f"Focus speed {speed} 설정", "lens")


def camera_power(address: int, enabled: bool) -> OutgoingCommand:
    command1 = 0x88 if enabled else 0x08
    label = "ON" if enabled else "OFF"
    return _command(address, command1, 0, 0, 0, f"Camera power {label}", "camera")


def camera_scan(address: int, mode: str) -> OutgoingCommand:
    mapping = {
        "manual": (0x10, "Manual Scan On"),
        "auto": (0x90, "Auto Scan On"),
        "off": (0x00, "Scan Stop"),
    }
    if mode not in mapping:
        raise ProtocolError("camera.scan mode는 manual/auto/off 중 하나여야 합니다.")
    command1, label = mapping[mode]
    return _command(address, command1, 0, 0, 0, label, "camera")


def set_camera_control(address: int, control: str, value: int) -> OutgoingCommand:
    mapping = {
        "auto_focus": (0x2B, "Auto Focus"),
        "auto_iris": (0x2D, "Auto Iris"),
        "agc": (0x2F, "AGC"),
        "backlight": (0x31, "Backlight Compensation"),
        "auto_white_balance": (0x33, "Auto White Balance"),
    }
    if control not in mapping:
        raise ProtocolError(f"알 수 없는 camera control입니다: {control}")
    opcode, label = mapping[control]
    return _command(address, 0, opcode, 0, _byte(value, label), f"{label} 값 {value} 설정", "camera")


def enable_device_phase_delay(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x35, 0, 0, "Device phase delay mode 활성화", "camera")


def set_shutter_speed(address: int, value: int) -> OutgoingCommand:
    msb, lsb = _split_u16(value)
    return _command(address, 0, 0x37, msb, lsb, f"Shutter speed raw {value} 설정", "camera")


def set_camera_adjustment(address: int, kind: str, value: int, delta: bool = False) -> OutgoingCommand:
    mapping = {
        "line_lock_phase": (0x39, "Line lock phase"),
        "white_balance_rb": (0x3B, "White balance R-B"),
        "white_balance_mg": (0x3D, "White balance M-G"),
        "gain": (0x3F, "Gain"),
        "auto_iris_level": (0x41, "Auto iris level"),
        "auto_iris_peak": (0x43, "Auto iris peak"),
    }
    if kind not in mapping:
        raise ProtocolError(f"알 수 없는 adjustment kind입니다: {kind}")
    opcode, label = mapping[kind]
    raw = _i16(value, label) if delta else _u16(value, label)
    msb, lsb = _split_u16(raw)
    mode = "delta" if delta else "set"
    return _command(address, 1 if delta else 0, opcode, msb, lsb, f"{label} {mode} {value}", "camera")


def preset_scan(address: int, dwell: int) -> OutgoingCommand:
    return _command(address, 0, 0x47, 0, _byte(dwell, "Preset scan dwell"), f"Preset scan dwell={dwell}", "scan")


def set_zero_position(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x49, 0, 0, "현재 Pan 위치를 Zero 기준으로 설정", "position")


def set_magnification(address: int, value: int, subopcode: int = 0) -> OutgoingCommand:
    msb, lsb = _split_u16(value)
    return _command(address, _byte(subopcode, "Magnification subopcode"), 0x5F, msb, lsb, f"Magnification raw {value} 설정", "camera")


def query_magnification(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x61, 0, 0, "Magnification 조회", "query")


def activate_echo_mode(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x65, 0, 0, "Echo mode 활성화", "device")


def set_remote_baud_rate(address: int, code: int, subopcode: int = 0) -> OutgoingCommand:
    return _command(address, _byte(subopcode, "Remote baud subopcode"), 0x67, 0, _byte(code, "Baud code"), f"Remote baud code {code} 설정", "device")


def query_diagnostic_info(address: int) -> OutgoingCommand:
    return _command(address, 0, 0x6F, 0, 0, "Diagnostic information 조회", "query")


def set_cruise_interval(address: int, value: int) -> OutgoingCommand:
    return _command(address, 0, 0x03, 0, _byte(value, "Cruise interval"), f"Cruise interval/time 값 {value} 설정(제조사 매핑)", "vendor")


def set_auto_home_time(address: int, value: int) -> OutgoingCommand:
    return _command(address, 0, 0x03, 0, _byte(value, "Auto home time"), f"Auto home time 값 {value} 설정(제조사 매핑)", "vendor")


def _param_bool(params: dict[str, Any], name: str, default: bool | None = None) -> bool:
    if name not in params:
        if default is None:
            raise ProtocolError(f"{name} 값이 필요합니다.")
        return default
    value = params[name]
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "1", "yes", "on"}:
        return True
    if isinstance(value, str) and value.lower() in {"false", "0", "no", "off"}:
        return False
    raise ProtocolError(f"{name}은 boolean이어야 합니다.")


def _param_int(params: dict[str, Any], name: str, default: int | None = None, *, minimum: int = 0, maximum: int = 0xFFFF) -> int:
    if name not in params:
        if default is None:
            raise ProtocolError(f"{name} 값이 필요합니다.")
        value = default
    else:
        value = int(params[name])
    if not minimum <= value <= maximum:
        raise ProtocolError(f"{name} 값은 {minimum}~{maximum} 범위여야 합니다.")
    return value


def _param_float(params: dict[str, Any], name: str) -> float:
    if name not in params:
        raise ProtocolError(f"{name} 값이 필요합니다.")
    return float(params[name])


def _param_enum(params: dict[str, Any], name: str, allowed: set[str], default: str | None = None) -> str:
    if name not in params:
        if default is None:
            raise ProtocolError(f"{name} 값이 필요합니다.")
        value = default
    else:
        value = str(params[name])
    if value not in allowed:
        raise ProtocolError(f"{name}은 {sorted(allowed)} 중 하나여야 합니다.")
    return value


def build_named_protocol_command(address: int, api_command: str, params: dict[str, Any]) -> BuiltProtocolCommand:
    address = validate_address(address)
    params = dict(params)
    command: OutgoingCommand | None = None
    commands: tuple[OutgoingCommand, ...] | None = None
    result: dict[str, Any] = {"command": api_command}
    confirm = False

    if api_command == "protocol.command":
        command = _command(
            address,
            _param_int(params, "command1", minimum=0, maximum=255),
            _param_int(params, "command2", minimum=0, maximum=255),
            _param_int(params, "data1", minimum=0, maximum=255),
            _param_int(params, "data2", minimum=0, maximum=255),
            str(params.get("description") or "사용자 지정 Pelco-D 명령"),
            str(params.get("category") or "raw"),
        )
        confirm = True
    elif api_command == "motion.relative":
        built: list[OutgoingCommand] = []
        if "pan_delta" in params:
            built.append(set_relative_pan_position(address, float(params["pan_delta"])))
            result["pan_delta"] = float(params["pan_delta"])
        if "tilt_delta" in params:
            built.append(set_relative_tilt_position(address, float(params["tilt_delta"])))
            result["tilt_delta"] = float(params["tilt_delta"])
        if not built:
            raise ProtocolError("pan_delta 또는 tilt_delta 중 하나가 필요합니다.")
        commands = tuple(built)
    elif api_command == "zoom.set":
        value = _param_int(params, "value")
        command = set_zoom_position(address, value)
        result["value"] = value
    elif api_command == "focus.set":
        value = _param_int(params, "value")
        command = set_focus_position(address, value)
        result["value"] = value
    elif api_command == "pelco.flip":
        command = flip(address)
    elif api_command == "pelco.zero_pan":
        command = goto_zero_pan(address)
    elif api_command == "pelco.remote_reset":
        command = remote_reset(address)
        confirm = True
    elif api_command in {"zone.set_start", "zone.set_end"}:
        zone = _param_int(params, "zone", minimum=1, maximum=255)
        command = set_zone_point(address, zone, api_command.endswith("start"))
        result["zone"] = zone
    elif api_command == "screen.write_char":
        column = _param_int(params, "column", minimum=0, maximum=39)
        char = params.get("char")
        if char is None:
            char = _param_int(params, "ascii", minimum=0, maximum=255)
        command = write_screen_character(address, column, char)
        result.update({"column": column, "char": char})
    elif api_command == "screen.write_text":
        column = _param_int(params, "column", 0, minimum=0, maximum=39)
        text = str(params.get("text") or "")
        if not text:
            raise ProtocolError("text 값이 필요합니다.")
        if column + len(text) > 40:
            raise ProtocolError("screen.write_text는 column + text 길이가 40 이하여야 합니다.")
        commands = tuple(write_screen_character(address, column + idx, char) for idx, char in enumerate(text))
        result.update({"column": column, "text": text})
    elif api_command == "screen.clear":
        command = clear_screen(address)
    elif api_command == "alarm.ack":
        alarm = _param_int(params, "alarm", minimum=0, maximum=255)
        subopcode = _param_int(params, "subopcode", 0, minimum=0, maximum=255)
        command = acknowledge_alarm(address, alarm, subopcode)
        result.update({"alarm": alarm, "subopcode": subopcode})
    elif api_command == "pattern.record_start":
        pattern = _param_int(params, "pattern", minimum=1, maximum=255)
        command = pattern_record_start(address, pattern)
        result["pattern"] = pattern
    elif api_command == "pattern.record_stop":
        command = pattern_record_stop(address)
    elif api_command == "pattern.run":
        pattern = _param_int(params, "pattern", minimum=1, maximum=255)
        command = pattern_run(address, pattern)
        result["pattern"] = pattern
    elif api_command == "lens.zoom_speed":
        speed = _param_int(params, "speed", minimum=0, maximum=255)
        command = set_zoom_speed(address, speed)
        result["speed"] = speed
    elif api_command == "lens.focus_speed":
        speed = _param_int(params, "speed", minimum=0, maximum=255)
        command = set_focus_speed(address, speed)
        result["speed"] = speed
    elif api_command == "camera.power":
        enabled = _param_bool(params, "enabled")
        command = camera_power(address, enabled)
        result["enabled"] = enabled
    elif api_command == "camera.scan":
        mode = _param_enum(params, "mode", {"manual", "auto", "off"})
        command = camera_scan(address, mode)
        result["mode"] = mode
    elif api_command in {"camera.auto_focus", "camera.auto_iris", "camera.agc"}:
        mode = _param_enum(params, "mode", {"auto", "off"})
        value = 0 if mode == "auto" else 1
        command = set_camera_control(address, api_command.split(".", 1)[1], value)
        result["mode"] = mode
    elif api_command == "camera.backlight":
        enabled = _param_bool(params, "enabled")
        value = 1 if enabled else 0
        command = set_camera_control(address, "backlight", value)
        result["enabled"] = enabled
    elif api_command == "camera.auto_white_balance":
        enabled = _param_bool(params, "enabled")
        value = 0 if enabled else 1
        command = set_camera_control(address, "auto_white_balance", value)
        result["enabled"] = enabled
    elif api_command == "camera.phase_delay":
        command = enable_device_phase_delay(address)
    elif api_command == "camera.shutter":
        value = _param_int(params, "value")
        command = set_shutter_speed(address, value)
        result["value"] = value
    elif api_command == "camera.adjust":
        kind = _param_enum(
            params,
            "kind",
            {"line_lock_phase", "white_balance_rb", "white_balance_mg", "gain", "auto_iris_level", "auto_iris_peak"},
        )
        value = _param_int(params, "value", minimum=-32768, maximum=65535)
        delta = _param_bool(params, "delta", False)
        command = set_camera_adjustment(address, kind, value, delta)
        result.update({"kind": kind, "value": value, "delta": delta})
    elif api_command == "preset.scan":
        dwell = _param_int(params, "dwell", minimum=0, maximum=255)
        command = preset_scan(address, dwell)
        result["dwell"] = dwell
    elif api_command == "zero.set":
        command = set_zero_position(address)
        confirm = True
    elif api_command == "magnification.set":
        value = _param_int(params, "value")
        subopcode = _param_int(params, "subopcode", 0, minimum=0, maximum=255)
        command = set_magnification(address, value, subopcode)
        result.update({"value": value, "subopcode": subopcode})
    elif api_command == "magnification.query":
        command = query_magnification(address)
    elif api_command == "echo.activate":
        command = activate_echo_mode(address)
    elif api_command == "device.remote_baud":
        code = _param_int(params, "code", minimum=0, maximum=5)
        subopcode = _param_int(params, "subopcode", 0, minimum=0, maximum=255)
        command = set_remote_baud_rate(address, code, subopcode)
        result.update({"code": code, "subopcode": subopcode})
        confirm = True
    elif api_command == "device.query_diagnostics":
        command = query_diagnostic_info(address)
    elif api_command == "cruise.interval":
        value = _param_int(params, "value", minimum=1, maximum=255)
        command = set_cruise_interval(address, value)
        result["value"] = value
    elif api_command == "home.time":
        value = _param_int(params, "value", minimum=1, maximum=255)
        command = set_auto_home_time(address, value)
        result["value"] = value
    else:
        raise ProtocolError(f"지원하지 않는 protocol API command입니다: {api_command}")

    if command is not None:
        commands = (command,)
    if not commands:
        raise ProtocolError(f"명령 생성 실패: {api_command}")
    result["hex"] = [frame_to_hex(item.data) for item in commands]
    result["description"] = [item.description for item in commands]
    return BuiltProtocolCommand(commands, result, confirm)


def frame_to_hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def parse_hex_command(text: str, address: int | None = None) -> OutgoingCommand:
    """Parse either a complete 7-byte frame or six bytes without checksum.

    If six bytes are entered, they must start with FF and a checksum is added.
    A five-byte payload can also be entered when ``address`` is supplied.
    """

    cleaned = text.replace(",", " ").replace("0x", "").replace("0X", "")
    try:
        values = bytes(int(part, 16) for part in cleaned.split())
    except (ValueError, OverflowError) as exc:
        raise ProtocolError("HEX 바이트를 공백으로 구분해서 입력하세요.") from exc

    if len(values) == 5 and address is not None:
        values = bytes((SYNC, validate_address(address))) + values
    if len(values) == 6:
        if values[0] != SYNC:
            raise ProtocolError("첫 바이트는 FF여야 합니다.")
        values += bytes((pelco_checksum(values[1:6]),))
    if len(values) != 7:
        raise ProtocolError(
            "완성 프레임은 7바이트, 체크섬 제외 프레임은 6바이트여야 합니다."
        )
    if values[0] != SYNC:
        raise ProtocolError("첫 바이트는 FF여야 합니다.")
    if not verify_frame_checksum(values):
        raise ProtocolError(
            f"체크섬 불일치: 입력={values[6]:02X}, 계산={pelco_checksum(values[1:6]):02X}"
        )
    return OutgoingCommand(values, describe_command(values), "raw")


def describe_command(frame: bytes) -> str:
    if len(frame) != 7 or frame[0] != SYNC:
        return f"Pelco-D 형식 아님 ({len(frame)}바이트)"
    address, command1, command2, data1, data2 = frame[1:6]
    valid = "정상" if verify_frame_checksum(frame) else "오류"

    if command1 == 0 and command2 == 0:
        label = "동작 정지"
    elif command1 == 0 and command2 == 0x51:
        label = "Pan 위치 조회"
    elif command1 == 0 and command2 == 0x53:
        label = "Tilt 위치 조회"
    elif command1 == 0 and command2 == 0x55:
        label = "Zoom 위치 조회"
    elif command1 == 0 and command2 == 0x0F:
        label = "Pelco Remote Reset"
    elif command1 == 0 and command2 in (0x11, 0x13):
        label = f"Zone {data2} {'시작점' if command2 == 0x11 else '종료점'} 저장"
    elif command1 == 0 and command2 == 0x15:
        label = f"Screen column {data1} char 0x{data2:02X}"
    elif command1 == 0 and command2 == 0x17:
        label = "Screen clear"
    elif command2 == 0x19:
        label = f"Alarm {data2} acknowledge"
    elif command1 == 0 and command2 in (0x1F, 0x21, 0x23):
        label = {0x1F: f"Pattern {data2} 기록 시작", 0x21: "Pattern 기록 종료", 0x23: f"Pattern {data2} 실행"}[command2]
    elif command1 == 0 and command2 == 0x25:
        label = f"Zoom speed {data2}"
    elif command1 == 0 and command2 == 0x27:
        label = f"Focus speed {data2}"
    elif command1 == 0 and command2 in (0x2B, 0x2D, 0x2F, 0x31, 0x33, 0x35, 0x37, 0x39, 0x3B, 0x3D, 0x3F, 0x41, 0x43):
        label = f"Camera control CMD2={command2:02X}, DATA={data1:02X} {data2:02X}"
    elif command1 == 0 and command2 == 0x4B:
        label = f"Pan 절대위치 {(data1 << 8 | data2) / 100.0:.2f}°"
    elif command1 == 0 and command2 == 0x4D:
        raw = data1 << 8 | data2
        label = f"Tilt 표시위치 {decode_tilt_display_degrees(raw):+.2f}°"
    elif command1 == 0 and command2 == 0x4F:
        label = f"Zoom 위치 Raw={data1 << 8 | data2}"
    elif command1 == 0 and command2 == 0x49:
        label = "Zero position 설정" if data1 == 0 and data2 == 0 else f"Tilt 상대위치 Raw=0x{data1:02X}{data2:02X}"
    elif command1 == 0 and command2 == 0x47:
        label = f"Preset scan/Relative Pan Raw=0x{data1:02X}{data2:02X}"
    elif command1 == 0 and command2 in (0x03, 0x05, 0x07):
        label = {
            0x03: f"프리셋/제조사 설정 {data2}",
            0x05: f"프리셋/제조사 해제 {data2}",
            0x07: f"프리셋/제조사 호출 {data2}",
        }[command2]
    elif command1 == 0 and command2 in (0x1B, 0x1D):
        label = "Zone Scan 시작" if command2 == 0x1B else "Zone Scan 정지"
    elif command1 == 0 and command2 in (0x09, 0x0B):
        label = f"Aux {data2} {'ON' if command2 == 0x09 else 'OFF'}"
    elif command2 & 0x1E or command1 & 0x07:
        motions = []
        for mask, name in (
            (0x02, "Right"),
            (0x04, "Left"),
            (0x08, "Up"),
            (0x10, "Down"),
            (0x20, "Zoom In"),
            (0x40, "Zoom Out"),
            (0x80, "Focus Near"),
        ):
            if command2 & mask:
                motions.append(name)
        if command1 & 0x01:
            motions.append("Focus Far")
        if command1 & 0x02:
            motions.append("Iris Open")
        if command1 & 0x04:
            motions.append("Iris Close")
        label = "+".join(motions) or "표준 동작"
        label += f" (PanSpd={data1}, TiltSpd={data2})"
    else:
        label = (
            f"CMD1={command1:02X}, CMD2={command2:02X}, DATA={data1:02X} {data2:02X}"
        )
    return f"주소 {address}: {label} / 체크섬 {valid}"


def split_response_blob(blob: bytes) -> list[bytes]:
    """Split a quiet-gap-delimited serial blob into likely Pelco frames.

    Valid seven-byte frames are preferred.  Otherwise a four-byte general
    response is consumed.  Non-Pelco bytes are retained as raw chunks so the
    operator can still inspect everything that arrived on the wire.
    """

    frames: list[bytes] = []
    remaining = bytes(blob)
    while remaining:
        if remaining[0] != SYNC:
            next_sync = remaining.find(bytes((SYNC,)))
            if next_sync < 0:
                frames.append(remaining)
                break
            if next_sync:
                frames.append(remaining[:next_sync])
                remaining = remaining[next_sync:]
                continue

        if len(remaining) >= 7 and verify_frame_checksum(remaining[:7]):
            frames.append(remaining[:7])
            remaining = remaining[7:]
        elif len(remaining) >= 4:
            frames.append(remaining[:4])
            remaining = remaining[4:]
        else:
            frames.append(remaining)
            break
    return frames


def decode_response(
    data: bytes, last_tx_checksum: int | None = None
) -> DecodedResponse:
    """Decode a 4-byte general response or 7-byte extended response."""

    if not data:
        return DecodedResponse(data, "빈 응답", "invalid", checksum_ok=False)
    if data[0] != SYNC:
        return DecodedResponse(
            data, "FF Sync로 시작하지 않는 원시 데이터", "raw", checksum_ok=False
        )

    if len(data) == 4:
        address, alarms, received = data[1], data[2], data[3]
        expected = (
            None if last_tx_checksum is None else (last_tx_checksum + alarms) & 0xFF
        )
        checksum_ok = None if expected is None else received == expected
        state = (
            "검증 불가"
            if checksum_ok is None
            else ("정상" if checksum_ok else "불일치")
        )
        return DecodedResponse(
            data,
            f"일반 응답: 주소={address}, Alarm=0x{alarms:02X}, 체크섬={state}",
            "general",
            address,
            alarms,
            checksum_ok,
        )

    if len(data) == 7:
        checksum_ok = verify_frame_checksum(data)
        address, response1, response2, msb, lsb = data[1:6]
        raw = msb << 8 | lsb
        suffix = "정상" if checksum_ok else "불일치"
        if response2 == 0x59:
            value = raw / 100.0
            text = f"Pan 위치 응답: {value:.2f}°"
            kind = "pan"
        elif response2 == 0x5B:
            value = decode_tilt_display_degrees(raw)
            text = f"Tilt 위치 응답: {value:+.2f}° (+위/-아래)"
            kind = "tilt"
        elif response2 == 0x5D:
            value = raw
            text = f"Zoom 위치 응답: Raw={raw}"
            kind = "zoom"
        elif response2 == 0x6D:
            value = raw
            text = f"Focus/Device 응답: Raw={raw}"
            kind = "focus_or_device"
        elif response2 == 0x01:
            value = response1
            text = f"표준 확장 응답: Resp1=0x{response1:02X}, Data=0x{msb:02X}{lsb:02X}"
            kind = "extended"
        else:
            value = raw
            text = (
                f"확장 응답: Resp1=0x{response1:02X}, Resp2=0x{response2:02X}, "
                f"Data=0x{raw:04X}"
            )
            kind = "extended"
        return DecodedResponse(
            data,
            f"{text}, 주소={address}, 체크섬={suffix}",
            kind,
            address,
            value,
            checksum_ok,
        )

    return DecodedResponse(
        data,
        f"알 수 없는 응답 길이: {len(data)}바이트",
        "raw",
        checksum_ok=None,
    )
