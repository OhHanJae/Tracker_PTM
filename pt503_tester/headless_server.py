"""Headless HTTP/TCP API and web console.

This module avoids PySide/Qt so it can run on Windows and Ubuntu/RDK targets.
By default it binds to the machine's primary LAN IPv4 address for field use.
"""

from __future__ import annotations

import argparse
import contextlib
from copy import deepcopy
from datetime import datetime
import errno
import json
import re
import os
import socket
import socketserver
import queue
import select
import signal
import struct
import sys
import threading
import uuid
import time
import math
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import serial
from serial.tools import list_ports
from .gpio_laser import GpioLaser

from .protocol import (
    AUTO_PAN_SPEED,
    AUTO_TILT_SPEED,
    manual_speed_value,
    OutgoingCommand,
    PanDirection,
    ProtocolError,
    TiltDirection,
    auto_home,
    call_preset,
    clear_preset,
    decode_response,
    factory_default,
    frame_to_hex,
    home_then_cruise1,
    home_then_preset1,
    lens_motion,
    manual_motion,
    parse_hex_command,
    build_named_protocol_command,
    power_on_self_check,
    query_device_type,
    query_focus,
    query_pan,
    query_tilt,
    query_zoom,
    protocol_named_command_names,
    remote_restart,
    self_check,
    set_auxiliary,
    set_cruise_speed,
    set_line_scan_point,
    set_pan_position,
    set_position_speed,
    set_preset,
    set_scan_speed,
    set_tilt_position,
    start_cruise,
    stop,
    vendor_line_scan,
    verify_frame_checksum,
    zone_scan,
)
from .recipe_store import RecipePoint, RecipeStore, app_state_dir
from .drawing_store import DrawingStore
from .motion_tracker import MotionTracker
from .network_utils import managed_server_endpoints, resolve_bind_host
from .api_metadata import (
    command_templates,
    protocol_catalog as api_protocol_catalog,
    supported_command_names,
)

DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8080
DEFAULT_TCP_HOST = DEFAULT_HTTP_HOST
DEFAULT_TCP_PORT = 8765
MAX_TCP_LINE_BYTES = 65_536
TCP_SEND_TIMEOUT_S = 2.0
TCP_SEND_QUEUE_SIZE = 32
TCP_SEND_QUEUE_BYTES = 8 * 1024 * 1024
CORE_WAKE_TIMEOUT_S = 2.0

# Pelco-D manual motion is latched by direction bits. Generic Pelco guidance
# describes a longer runaway timeout, but PT503 field behavior can stop sooner.
# Refresh an active manual motion conservatively for Pelco runaway protection.
# There is intentionally no short application watchdog: manual motion ends only
# on explicit STOP/release,
# another motion mode, serial disconnect, or communication failure.
JOG_RESEND_INTERVAL_S = 3.5
PELCO_MIN_COMMAND_GAP_S = 0.32
PELCO_ABSOLUTE_AXIS_GAP_S = 0.05
PELCO_POSITION_QUERY_GAP_S = 0.05
MOTION_FEEDBACK_POLL_S = 0.2

PROTOCOL_NAME = 'PT503-Control'
PROTOCOL_VERSION = '1.0'
SUPPORTED_COMMANDS = supported_command_names()

# Arduino UNO laser controller (Modbus RTU)
LASER_DEFAULT_BAUD = 9600
LASER_DEFAULT_ADDRESS = 0x01
LASER_COIL_ADDRESS = 0x0000
LASER_RESPONSE_TIMEOUT_S = 0.35
LASER_API_COMMANDS = {
    'laser.state',
    'power_save.wake',
    'power_save.settings',
    'laser.power_save',
    'laser.settings',
    'laser.serial_connect',
    'laser.serial_disconnect',
    'laser.status',
}
PTM_SETTINGS_COMMANDS = {'ptm.settings'}
DRAWING_API_COMMANDS = {
    'drawing.list',
    'drawing.get',
    'drawing.delete',
    'drawing.update',
    'drawing.reorder',
    'drawing.select_point_set',
    'drawing.select_recipe',
    'drawing.point_upsert',
    'drawing.point_delete',
    'drawing.point_reorder',
    'drawing.calibrate',
    'drawing.reset_calibration',
    'drawing.estimate_pan_tilt',
    'drawing.estimate_xy',
    'drawing.estimate_recipe',
    'drawing.export_recipe',
    'drawing.import_recipe',
}
MAX_DRAWING_UPLOAD_BYTES = 512 * 1024 * 1024



class HeadlessApiError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HeadlessController:
    def __init__(
        self,
        recipe_store: RecipeStore | None = None,
        drawing_store: DrawingStore | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        self.recipe_store = recipe_store or RecipeStore()
        self.drawing_store = drawing_store or DrawingStore()
        self.drawing_store.recipe_store = self.recipe_store
        self._settings_path = (
            Path(settings_path)
            if settings_path is not None
            else app_state_dir() / "server_settings.json"
        )
        self._lock = threading.RLock()
        self._laser_lock = threading.RLock()
        self._laser_backend = "gpio"
        self._gpio_laser = GpioLaser()
        self._laser_error = ""
        self._laser_generation = 0
        self._laser_state_allowed = True
        self._laser_active_state: str | None = None
        self._laser_active_profile: dict[str, Any] = {}
        self._laser_blink_next = 0.0
        self.laser_state_profiles = {
            "point_irradiation": {"mode": "on", "interval_ms": 500},
            "position_check": {"mode": "on", "interval_ms": 500},
            "wrong_point": {"mode": "blink", "interval_ms": 250},
            "work_complete": {"mode": "off", "interval_ms": 500},
        }
        self._power_saving = False
        self._laser_plc_mode = 1
        self._laser_applied_mode = 1
        self.laser_control_priority = False
        self._laser_core_policy: dict[str, Any] = {"enabled": False}
        self._laser_manual_deadline = 0.0
        self._laser_manual_allowed = True
        self._laser_manual_block_reason = ""
        self._serial: serial.Serial | None = None
        self._laser_serial: serial.Serial | None = None
        self._laser_port = ''
        self._laser_baudrate = LASER_DEFAULT_BAUD
        self._laser_address = LASER_DEFAULT_ADDRESS
        self.laser_last_tx: dict[str, Any] | None = None
        self.laser_last_rx: dict[str, Any] | None = None
        self._port = ""
        self._baudrate = 9600
        self._address = 1
        self.current_pan: float | None = None
        self.current_tilt: float | None = None
        self.last_rx: list[dict[str, Any]] = []
        self._rx_buffer = bytearray()
        self.last_tx: dict[str, Any] | None = None
        self.completion_config = {"tolerance_deg": 0.2, "stable_samples": 3, "timeout_s": 30}
        self.monitor_config = {"enabled": False, "interval_ms": 500}
        self.device_settings = {"invert_pan": False, "invert_tilt": False, "power_on_home": None}
        self._power_on_home_sent = False
        self._power_on_home_error = ""
        self.laser_bypass = False
        self.laser_on = False
        self._laser_automatic_off = False
        self.aux_number = 1
        self._jog_next_resend = 0.0
        self._jog_lease_deadline = 0.0
        self._jog_token: str | None = None
        # Active continuous JOG key: (pan, tilt, pan_level, tilt_level).
        # The PT unit receives the frame at start/change and then about every
        # every few seconds while manual JOG remains active.  Re-sending too
        # frequently can make some PT heads visibly restart/hesitate.
        self._active_jog: tuple[Any, Any, int, int] | None = None
        # TCP jog ownership matters only for disconnect safety.  HTTP/Web JOG
        # has no persistent socket owner and therefore uses None.  A random TCP
        # status/health client disconnect must never stop somebody else's JOG.
        self._jog_owner_client_id: str | None = None
        self._last_serial_tx_at = 0.0
        self._position_speed_frame: bytes | None = None
        self._laser_deadline = 0.0
        self._health_misses = 0
        self._tracker = None
        self.motion_state = "idle"
        self._event_callback: Callable[[str, dict[str, Any], str | None], None] | None = None
        self._pending_motion: dict[str, Any] | None = None
        self._completed_motion_id: str | None = None
        self._failed_motion_id: str | None = None
        self._last_motion_error: str = ""
        self._selected_recipe_id: str | None = None
        self._selected_point_id: str | None = None
        self._home_valid = False
        self._last_protocol_states: dict[str, str] = {}
        self._shutdown = threading.Event()
        self._scan_cancel = threading.Event()
        self.scanning = False
        self.auto_reconnect_config = {
            "enabled": True,
            "baudrates": [9600],
            "first_address": 1,
            "last_address": 16,
            "timeout_ms": 200,
            "retry_interval_s": 3.0,
            "health_interval_s": 2.0,
        }
        self._load_settings()
        if self._laser_backend == "gpio":
            try:
                self._gpio_laser.open()
            except Exception as exc:
                self._laser_error = str(exc)
        self._auto_scan_active = False
        self._last_auto_scan = 0.0
        self._service = threading.Thread(target=self._service_loop, daemon=True)
        self._laser_service = threading.Thread(target=self._laser_service_loop, daemon=True)
        self._service.start()
        self._laser_service.start()

    def set_event_callback(
        self,
        callback: Callable[[str, dict[str, Any], str | None], None] | None,
    ) -> None:
        self._event_callback = callback

    def jog_owned_by(self, client_id: str) -> bool:
        """Return True only when this TCP client started the active manual JOG."""
        with self._lock:
            return (
                self._active_jog is not None
                and self._jog_owner_client_id is not None
                and self._jog_owner_client_id == client_id
            )

    def protocol_states(self) -> dict[str, str]:
        if self.connected:
            connection = "connected"
        elif self.scanning or self._auto_scan_active:
            connection = "connecting"
        else:
            connection = "disconnected"
        return {
            "pt503": connection,
            "motor": "moving" if self.motion_state in {"tracking", "jog"} else "stopped",
            "home": "valid" if self._home_valid else "invalid",
        }

    def _emit_event(
        self,
        event: str,
        data: dict[str, Any],
        client_id: str | None = None,
    ) -> None:
        callback = self._event_callback
        if callback is not None:
            callback(event, data, client_id)

    def _publish_state_changes(self) -> None:
        states = self.protocol_states()
        event_names = {
            "pt503": "pt503.connection_changed",
            "motor": "motor.state_changed",
            "home": "home.state_changed",
        }
        for key, state in states.items():
            if self._last_protocol_states.get(key) != state:
                self._last_protocol_states[key] = state
                self._emit_event(event_names[key], {"state": state})

    def _finish_pending_motion(self, *, error: tuple[str, str] | None = None) -> None:
        pending = self._pending_motion
        self._pending_motion = None
        if pending is None:
            return
        data = {
            "motion_id": pending.get("motion_id"),
            "request_id": pending.get("request_id"),
            "command": pending.get("command"),
            "recipe_id": pending.get("recipe_id"),
            "point_id": pending.get("point_id"),
        }
        if error is None:
            with self._laser_lock:
                if (pending.get("laser_after_move")
                        and not self.laser_bypass
                        and pending.get("laser_generation") == self._laser_generation):
                    try:
                        self._apply_laser_state("point_irradiation")
                    except HeadlessApiError as exc:
                        self._laser_deadline = time.monotonic() + 0.2
                        self._failed_motion_id = pending.get("motion_id")
                        self._last_motion_error = exc.code
                        self._emit_event("motion.error", {**data, "error": {
                            "code": exc.code, "message": exc.message}}, pending.get("client_id"))
                        return
            self._completed_motion_id = pending.get("motion_id")
            self._last_motion_error = ""
            if pending.get("home"):
                self._home_valid = True
            self._emit_event("motion.completed", data, pending.get("client_id"))
        else:
            code, message = error
            self._failed_motion_id = pending.get("motion_id")
            self._last_motion_error = code
            self._emit_event(
                "motion.error",
                {**data, "error": {"code": code, "message": message}},
                pending.get("client_id"),
            )

    def _load_settings(self) -> None:
        try:
            payload = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        laser_settings = payload.get("laser") if isinstance(payload, dict) else None
        if isinstance(laser_settings, dict) and laser_settings.get("backend") in {"gpio", "modbus"}:
            self._laser_backend = laser_settings["backend"]
        if isinstance(laser_settings, dict) and isinstance(laser_settings.get("bypass"), bool):
            self.laser_bypass = laser_settings["bypass"]
        if isinstance(laser_settings, dict) and isinstance(laser_settings.get("control_priority"), bool):
            self.laser_control_priority = laser_settings["control_priority"]
        if isinstance(laser_settings, dict) and "state_profiles" in laser_settings:
            try:
                self.laser_state_profiles = self._validated_laser_profiles(laser_settings["state_profiles"])
            except (ValueError, TypeError):
                pass
        device_settings = payload.get("device") if isinstance(payload, dict) else None
        if isinstance(device_settings, dict):
            for key in self.device_settings:
                if isinstance(device_settings.get(key), bool):
                    self.device_settings[key] = device_settings[key]
        serial_settings = payload.get("serial") if isinstance(payload, dict) else None
        if not isinstance(serial_settings, dict):
            return
        port = serial_settings.get("port")
        baudrate = serial_settings.get("baudrate")
        address = serial_settings.get("address")
        if isinstance(port, str):
            self._port = port
        if isinstance(baudrate, int) and 1200 <= baudrate <= 115200:
            self._baudrate = baudrate
        if isinstance(address, int) and 1 <= address <= 255:
            self._address = address
        reconnect = serial_settings.get("auto_reconnect")
        if not isinstance(reconnect, dict):
            return
        defaults = self.auto_reconnect_config
        if isinstance(reconnect.get("enabled"), bool):
            defaults["enabled"] = reconnect["enabled"]
        baudrates = reconnect.get("baudrates")
        if (
            isinstance(baudrates, list)
            and baudrates
            and all(isinstance(item, int) and 1200 <= item <= 115200 for item in baudrates)
        ):
            defaults["baudrates"] = list(baudrates)
        for key, minimum, maximum in (
            ("first_address", 1, 255),
            ("last_address", 1, 255),
            ("timeout_ms", 50, 2000),
        ):
            value = reconnect.get(key)
            if isinstance(value, int) and minimum <= value <= maximum:
                defaults[key] = value
        for key in ("retry_interval_s", "health_interval_s"):
            value = reconnect.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and 0.5 <= float(value) <= 3600.0
            ):
                defaults[key] = float(value)
        if int(defaults["first_address"]) > int(defaults["last_address"]):
            defaults["first_address"] = self._address
            defaults["last_address"] = self._address

    def _save_settings(self) -> None:
        payload = {
            "version": 1,
            "device": dict(self.device_settings),
            "laser": {"bypass": self.laser_bypass, "backend": self._laser_backend,
                      "control_priority": self.laser_control_priority,
                      "state_profiles": deepcopy(self.laser_state_profiles),
                      "numbering": "BOARD", "pin": 31, "active_high": True},
            "serial": {
                "port": self._port,
                "baudrate": self._baudrate,
                "address": self._address,
                "auto_reconnect": dict(self.auto_reconnect_config),
            },
        }
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._settings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._settings_path)

    def _device_settings_status(self) -> dict[str, Any]:
        return {**self.device_settings,
                "power_on_home_sent": self._power_on_home_sent,
                "power_on_home_error": self._power_on_home_error}

    def _apply_power_on_home(self) -> None:
        self._power_on_home_sent = False
        self._power_on_home_error = ""
        enabled = self.device_settings["power_on_home"]
        if enabled is None or not self.connected:
            return
        try:
            self.send(power_on_self_check(self._address, enabled), wait_ms=80)
        except HeadlessApiError as exc:
            self._power_on_home_error = exc.message
        else:
            # This vendor setting has no readback; a sent frame is not firmware confirmation.
            self._power_on_home_sent = True

    def _core_request(self, command: str, params: dict[str, Any], *,
                      host: str | None = None, port: int | None = None,
                      timeout_s: float = CORE_WAKE_TIMEOUT_S,
                      error_code: str = "CORE_SETTINGS_FAILED") -> dict[str, Any]:
        host = host or os.environ.get("PT503_CORE_HOST", "127.0.0.1")
        port = port if port is not None else int(os.environ.get("PT503_CORE_PORT", "8000"))
        try:
            request_id = uuid.uuid4().hex
            request = {"id": request_id, "command": command, "params": params}
            deadline = time.monotonic() + timeout_s
            with socket.create_connection((host, port), timeout=timeout_s) as connection:
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                connection.sendall((json.dumps(request) + "\n").encode("utf-8"))
                buffered = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Core response timed out")
                    if b"\n" in buffered:
                        line, _, rest = buffered.partition(b"\n")
                        buffered = bytearray(rest)
                        if len(line) > MAX_TCP_LINE_BYTES:
                            raise ValueError("Core response is too large")
                        response = json.loads(line)
                        if not isinstance(response, dict) or response.get("id") != request_id:
                            continue
                        if response.get("ok") is not True:
                            error = response.get("error")
                            raise ValueError(error.get("message", str(error)) if isinstance(error, dict) else str(error))
                        return response.get("result") or {}
                    if len(buffered) > MAX_TCP_LINE_BYTES:
                        raise ValueError("Core response is too large")
                    connection.settimeout(remaining)
                    chunk = connection.recv(min(4096, MAX_TCP_LINE_BYTES + 1 - len(buffered)))
                    if not chunk:
                        raise ValueError("Core closed before the response")
                    buffered.extend(chunk)
        except (OSError, ValueError) as exc:
            raise HeadlessApiError(error_code, str(exc)) from exc

    def shutdown(self) -> None:
        self._shutdown.set()
        self._scan_cancel.set()
        self._service.join(timeout=2)
        self._laser_service.join(timeout=2)
        self.close_laser()
        self.close()

    def _laser_service_loop(self) -> None:
        while not self._shutdown.wait(0.04):
            with self._laser_lock:
                if self._laser_manual_deadline and time.monotonic() >= self._laser_manual_deadline:
                    try:
                        self._laser_write_coil(False)
                    except HeadlessApiError:
                        self._laser_deadline = time.monotonic() + 0.2
                    else:
                        self._laser_deadline = 0.0

                if self._laser_deadline and time.monotonic() >= self._laser_deadline:
                    try:
                        self._laser_write_coil(False)
                    except HeadlessApiError:
                        self._laser_deadline = time.monotonic() + 0.2
                    else:
                        self._laser_deadline = 0.0
                if self._laser_blink_next and time.monotonic() >= self._laser_blink_next:
                    try:
                        self._laser_write_coil(not self.laser_on, pattern_step=True)
                    except HeadlessApiError:
                        self._laser_active_state = None
                        self._laser_blink_next = 0.0
                        self._laser_deadline = time.monotonic() + 0.2
                    else:
                        self._laser_blink_next = time.monotonic() + self._laser_active_profile["interval_ms"] / 1000

    def _validated_laser_profiles(self, profiles: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(profiles, dict) or set(profiles) != set(self.laser_state_profiles):
            raise ValueError("레이저 상태 설정에는 4개 상태가 모두 필요합니다.")
        result = {}
        for state, profile in profiles.items():
            if not isinstance(profile, dict) or profile.get("mode") not in {"on", "blink", "off"}:
                raise ValueError(f"잘못된 레이저 조사 방식: {state}")
            interval = profile.get("interval_ms", 500)
            if type(interval) is not int or not 100 <= interval <= 60000:
                raise ValueError("점멸 전환 간격은 100~60000 ms 정수로 입력하세요.")
            result[state] = {"mode": profile["mode"], "interval_ms": interval}
        return result

    def _effective_laser_mode(self) -> int:
        return 1 if self.laser_control_priority else self._laser_plc_mode

    def _apply_laser_state(self, state: str, *, manual: bool = False, profile: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._effective_laser_mode() != 1 and state != "plc_override":
            return {"accepted": False, "reason": "PLC laser mode has priority"}
        if state not in self.laser_state_profiles and (state != "plc_override" or profile is None):
            raise HeadlessApiError("INVALID_PARAMS", "unknown laser state")
        profile = deepcopy(profile if profile is not None else self.laser_state_profiles[state])
        if profile["mode"] != "off":
            if self._pending_motion is not None and state != "plc_override":
                raise HeadlessApiError("MOTION_ACTIVE", "Laser remains OFF until movement completes")
            allowed = self._laser_manual_allowed if manual else self._laser_state_allowed
            if not allowed or (not manual and self._laser_automatic_off and state != "work_complete"):
                raise HeadlessApiError("LASER_INTERLOCK", "Core safety interlock blocks laser state")
        if not manual and self._laser_manual_deadline:
            return {"accepted": False, "manual_override": True}
        if self._laser_active_state == state and self._laser_active_profile == profile and manual == bool(self._laser_manual_deadline):
            if manual:
                self._laser_manual_deadline = time.monotonic() + 1800
            return {"accepted": True, "state": state, **profile, "laser_on": self.laser_on}
        self._laser_write_coil(False)
        self._laser_active_state = state
        self._laser_active_profile = profile
        if manual and profile["mode"] != "off":
            self._laser_manual_deadline = time.monotonic() + 1800
        try:
            if profile["mode"] != "off":
                self._laser_write_coil(True, pattern_step=True)
            if profile["mode"] == "blink":
                self._laser_blink_next = time.monotonic() + profile["interval_ms"] / 1000
        except HeadlessApiError:
            self._laser_active_state = None
            self._laser_blink_next = 0.0
            self._laser_manual_deadline = 0.0
            self._laser_deadline = time.monotonic() + 0.2
            raise
        return {"accepted": True, "state": state, **profile, "laser_on": self.laser_on}

    def _service_loop(self) -> None:
        next_poll = 0.0
        next_health = 0.0
        while not self._shutdown.wait(0.04):
            self._publish_state_changes()
            if self._should_auto_reconnect():
                self._run_auto_reconnect()
                continue

            with self._lock:
                if not self.connected:
                    continue
                try:
                    now = time.monotonic()
                    jogging = self._active_jog is not None

                    if jogging and self._jog_lease_deadline and now >= self._jog_lease_deadline:
                        self._active_jog = None
                        self._jog_owner_client_id = None
                        self._jog_next_resend = 0.0
                        self._jog_lease_deadline = 0.0
                        self._jog_token = None
                        self._tracker = None
                        self.motion_state = "stopped"
                        self.send(stop(self._address), wait_ms=0)
                        jogging = False

                    # Pelco-D runaway protection: while the operator is still
                    # holding manual motion, repeat the same motion command only
                    # every few seconds. Do NOT send STOP on a software timer;
                    # release/STOP is authoritative.
                    if jogging and now >= self._jog_next_resend:
                        pan, tilt, pan_level, tilt_level = self._active_jog
                        self.send(
                            manual_motion(
                                self._address,
                                pan,
                                tilt,
                                manual_speed_value(pan_level),
                                manual_speed_value(tilt_level),
                            ),
                            wait_ms=80,
                        )
                        self._jog_next_resend = time.monotonic() + JOG_RESEND_INTERVAL_S

                    if self._tracker and self._tracker.check_timeout().status == "timeout":
                        self._tracker = None
                        self.motion_state = "timeout"
                        self.send(stop(self._address), wait_ms=80)
                        self._finish_pending_motion(
                            error=("MOTION_TIMEOUT", "target position was not reached in time")
                        )

                    # Never interleave position/health queries with continuous
                    # manual motion.  Extra commands can make PT heads hesitate.
                    if not jogging:
                        pan_feedback = None
                        if now >= next_poll and (self.monitor_config["enabled"] or self._tracker):
                            tracking = self._tracker is not None
                            # Only feedback queries use the shorter gap and finish
                            # early on a validated response; control commands keep 320ms.
                            if tracking:
                                pan_feedback = self.send(
                                    query_pan(self._address), wait_ms=80,
                                    min_gap_s=PELCO_POSITION_QUERY_GAP_S, response_kind="pan",
                                )
                                self.send(
                                    query_tilt(self._address), wait_ms=80,
                                    min_gap_s=PELCO_POSITION_QUERY_GAP_S, response_kind="tilt",
                                )
                            else:
                                self.send(query_pan(self._address), wait_ms=80)
                                self.send(query_tilt(self._address), wait_ms=80)
                            next_poll = (
                                now + MOTION_FEEDBACK_POLL_S if tracking
                                else time.monotonic() + self.monitor_config["interval_ms"] / 1000
                            )
                        if now >= next_health:
                            health_rx = pan_feedback if pan_feedback is not None else self.send(query_pan(self._address), wait_ms=80)
                            if health_rx:
                                self._health_misses = 0
                            else:
                                self._health_misses += 1
                                if self._health_misses >= 3:
                                    self.motion_state = "communication_timeout"
                                    self.close()
                                    continue
                            next_health = time.monotonic() + float(
                                self.auto_reconnect_config["health_interval_s"]
                            )
                except (HeadlessApiError, OSError):
                    self.motion_state = "communication_error"
                    self._finish_pending_motion(
                        error=("COMMUNICATION_ERROR", "PT503 communication failed during movement")
                    )
                    self.close()

    def _should_auto_reconnect(self) -> bool:
        with self._lock:
            if (
                not self.auto_reconnect_config["enabled"]
                or self.connected
                or self.scanning
                or self._auto_scan_active
            ):
                return False
            now = time.monotonic()
            return (
                now - self._last_auto_scan
                >= float(self.auto_reconnect_config["retry_interval_s"])
            )

    def _run_auto_reconnect(self) -> None:
        with self._lock:
            if self._shutdown.is_set() or self.connected or self.scanning or self._auto_scan_active:
                return
            self._auto_scan_active = True
            self._last_auto_scan = time.monotonic()
        self._publish_state_changes()
        try:
            config = dict(self.auto_reconnect_config)
            self._scan_cancel.clear()
            self.scan(
                baudrates=list(config["baudrates"]),
                first_address=int(config["first_address"]),
                last_address=int(config["last_address"]),
                timeout_ms=int(config["timeout_ms"]),
                enable_auto_reconnect=True,
            )
        except (HeadlessApiError, OSError, serial.SerialException):
            pass
        finally:
            with self._lock:
                self._auto_scan_active = False
            self._publish_state_changes()

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def laser_connected(self) -> bool:
        if self._laser_backend == "gpio":
            return self._gpio_laser.connected and not self._laser_error
        return self._laser_serial is not None and self._laser_serial.is_open

    @staticmethod
    def _modbus_crc16(data: bytes) -> int:
        """Modbus RTU CRC16 (polynomial 0xA001)."""
        crc = 0xFFFF
        for value in data:
            crc ^= value
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc & 0xFFFF

    @classmethod
    def _append_modbus_crc(cls, payload: bytes) -> bytes:
        crc = cls._modbus_crc16(payload)
        return payload + bytes((crc & 0xFF, (crc >> 8) & 0xFF))

    @classmethod
    def _modbus_crc_ok(cls, frame: bytes) -> bool:
        if len(frame) < 3:
            return False
        received = frame[-2] | (frame[-1] << 8)
        return received == cls._modbus_crc16(frame[:-2])

    def connect_laser(
        self,
        port: str,
        baudrate: int = LASER_DEFAULT_BAUD,
        address: int = LASER_DEFAULT_ADDRESS,
    ) -> dict[str, Any]:
        """Open the USB-RS485 port connected to the Arduino laser controller."""
        with self._laser_lock:
            if self._shutdown.is_set():
                raise HeadlessApiError("SERVICE_STOPPING", "server is shutting down")
            self.close_laser()
            try:
                self._laser_serial = serial.Serial(
                    port=port,
                    baudrate=int(baudrate),
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0,
                    write_timeout=0.5,
                    xonxoff=False,
                    rtscts=False,
                    dsrdtr=False,
                )
                self._laser_serial.reset_input_buffer()
                self._laser_serial.reset_output_buffer()
            except serial.SerialException as exc:
                self._laser_serial = None
                raise HeadlessApiError("LASER_SERIAL_OPEN_FAILED", str(exc)) from exc

            self._laser_port = str(port)
            self._laser_baudrate = int(baudrate)
            self._laser_address = int(address)
            self.laser_on = False
            self._laser_deadline = 0.0

            # Safety: force relay/laser OFF immediately after connection.
            self._laser_write_coil(False)
            return {
                "connected": True,
                "port": self._laser_port,
                "baudrate": self._laser_baudrate,
                "address": self._laser_address,
                "laser_on": self.laser_on,
            }

    def close_laser(self) -> None:
        """Turn the laser OFF if possible, then close only the Arduino RS485 port."""
        with self._laser_lock:
            self._laser_active_state = None
            self._laser_blink_next = 0.0
            self._laser_manual_deadline = 0.0
            self._laser_generation += 1
            if self._laser_backend == "gpio":
                try:
                    self._gpio_laser.close()
                except Exception as exc:
                    self._laser_error = str(exc)
            self._laser_deadline = 0.0
            if self.laser_connected or (self._laser_backend == "gpio" and self._gpio_laser.connected):
                try:
                    self._laser_write_coil(False)
                except (HeadlessApiError, serial.SerialException):
                    pass
            if self._laser_serial is not None:
                try:
                    self._laser_serial.close()
                except serial.SerialException:
                    pass
            self._laser_serial = None
            self.laser_on = False

    def _laser_transceive(
        self,
        request: bytes,
        *,
        minimum_response_bytes: int,
        timeout_s: float = LASER_RESPONSE_TIMEOUT_S,
    ) -> bytes:
        if not self.laser_connected or self._laser_serial is None:
            raise HeadlessApiError(
                "LASER_SERIAL_NOT_CONNECTED",
                "Connect Arduino laser RS485 port first with laser.serial_connect",
            )

        with self._laser_lock:
            try:
                self._laser_serial.reset_input_buffer()
                self._laser_serial.write(request)
                self._laser_serial.flush()
                self.laser_last_tx = {
                    "hex": request.hex(" ").upper(),
                    "time": time.time(),
                }

                deadline = time.perf_counter() + float(timeout_s)
                received = bytearray()
                while time.perf_counter() < deadline:
                    count = self._laser_serial.in_waiting
                    if count:
                        received.extend(self._laser_serial.read(count))
                        if len(received) >= minimum_response_bytes:
                            break
                    time.sleep(0.003)
            except (serial.SerialException, OSError) as exc:
                raise HeadlessApiError("LASER_SERIAL_IO_FAILED", str(exc)) from exc

        response = bytes(received)
        self.laser_last_rx = {
            "hex": response.hex(" ").upper(),
            "time": time.time(),
        }
        if len(response) < minimum_response_bytes:
            raise HeadlessApiError(
                "LASER_NO_RESPONSE",
                f"Arduino response timeout: RX={response.hex(' ').upper() or '<empty>'}",
            )
        return response

    def _laser_write_coil(self, enabled: bool, *, pattern_step: bool = False) -> dict[str, Any]:
        """Write the owned GPIO output, or the explicitly configured legacy relay."""
        if enabled and self._effective_laser_mode() not in {1, 2, 3, 4, 5}:
            raise HeadlessApiError("LASER_INTERLOCK", "PLC laser mode forces output OFF")
        state_permitted = pattern_step and self._laser_state_allowed and (
            not self._laser_automatic_off or self._laser_active_state == "work_complete")
        if enabled and not self._laser_manual_allowed and not state_permitted:
            raise HeadlessApiError("LASER_INTERLOCK", "Core safety interlock blocks laser output: "
                                   + self._laser_manual_block_reason)
        if enabled and self.laser_bypass:
            raise HeadlessApiError("LASER_BYPASSED", "Laser output is disabled by bypass mode")
        if enabled and self._power_saving and not self._laser_manual_deadline:
            raise HeadlessApiError("POWER_SAVING", "Wake Core before enabling laser output")
        if not enabled and not pattern_step:
            self._laser_active_state = None
            self._laser_blink_next = 0.0
            if self._laser_manual_deadline:
                self._laser_manual_deadline = 0.0
            self._laser_generation += 1
        if (not enabled and self.laser_bypass and not self.laser_connected
                and not (self._laser_backend == "gpio" and self._gpio_laser.connected)):
            self.laser_on = False
            return {"connected": False, "laser_on": False, "bypass": True}
        if self._laser_backend == "gpio":
            try:
                self.laser_on = self._gpio_laser.write(enabled)
                self._laser_error = ""
            except Exception as exc:
                self._laser_error = str(exc)
                self._laser_manual_deadline = 0.0
                self._laser_deadline = time.monotonic() + 0.2
                raise HeadlessApiError("LASER_GPIO_IO_FAILED", str(exc)) from exc
            return {"accepted": True, "connected": True, "laser_on": self.laser_on,
                    "backend": "gpio", "numbering": "BOARD", "pin": 31, "active_high": True}
        value = 0xFF00 if enabled else 0x0000
        payload = bytes((
            self._laser_address,
            0x05,
            (LASER_COIL_ADDRESS >> 8) & 0xFF,
            LASER_COIL_ADDRESS & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
        ))
        request = self._append_modbus_crc(payload)
        response = self._laser_transceive(request, minimum_response_bytes=8)
        response = response[:8]

        if not self._modbus_crc_ok(response):
            raise HeadlessApiError(
                "LASER_BAD_CRC",
                f"Bad Modbus CRC: {response.hex(' ').upper()}",
            )
        if response != request:
            raise HeadlessApiError(
                "LASER_BAD_RESPONSE",
                "Arduino Function 0x05 response did not echo the request",
            )

        self.laser_on = bool(enabled)
        return {
            "accepted": True,
            "laser_on": self.laser_on,
            "tx": request.hex(" ").upper(),
            "rx": response.hex(" ").upper(),
        }

    def _laser_read_coil(self) -> dict[str, Any]:
        """Read Arduino Coil 0 with Modbus Function 0x01."""
        if self._laser_backend == "gpio":
            try:
                self.laser_on = self._gpio_laser.read()
            except Exception as exc:
                self._laser_error = str(exc)
                self._laser_manual_deadline = 0.0
                self._laser_deadline = time.monotonic() + 0.2
                raise HeadlessApiError("LASER_GPIO_IO_FAILED", str(exc)) from exc
            return {"connected": True, "laser_on": self.laser_on, "backend": "gpio",
                    "numbering": "BOARD", "pin": 31, "active_high": True,
                    "feedback_available": False, "on_source": "commanded_gpio_level", "error": self._laser_error}
        payload = bytes((
            self._laser_address,
            0x01,
            (LASER_COIL_ADDRESS >> 8) & 0xFF,
            LASER_COIL_ADDRESS & 0xFF,
            0x00,
            0x01,
        ))
        request = self._append_modbus_crc(payload)
        response = self._laser_transceive(request, minimum_response_bytes=6)
        response = response[:6]

        if not self._modbus_crc_ok(response):
            raise HeadlessApiError(
                "LASER_BAD_CRC",
                f"Bad Modbus CRC: {response.hex(' ').upper()}",
            )
        if response[0] != self._laser_address or response[1] != 0x01 or response[2] != 0x01:
            raise HeadlessApiError(
                "LASER_BAD_RESPONSE",
                f"Unexpected Modbus response: {response.hex(' ').upper()}",
            )

        self.laser_on = bool(response[3] & 0x01)
        return {
            "connected": self.laser_connected,
            "laser_on": self.laser_on,
            "tx": request.hex(" ").upper(),
            "rx": response.hex(" ").upper(),
        }

    def close(self) -> None:
        """Close PT-503 serial only. Laser uses its own serial port."""
        with self._lock:
            self._power_on_home_sent = False
            self._jog_next_resend = 0.0
            self._active_jog = None
            self._jog_owner_client_id = None
            self._tracker = None
            self._finish_pending_motion(
                error=("COMMUNICATION_ERROR", "PT503 serial connection was closed")
            )
            if self.motion_state not in {"communication_error", "communication_timeout"}:
                self.motion_state = "disconnected"
            if self.connected:
                try:
                    self._serial.write(stop(self._address).data)
                    self._serial.flush()
                except serial.SerialException:
                    pass
            if self._serial is not None:
                try:
                    self._serial.close()
                except serial.SerialException:
                    pass
            self._serial = None
            self._health_misses = 0
            self._last_serial_tx_at = 0.0
            self._position_speed_frame = None
            self.current_pan = self.current_tilt = None
            self._home_valid = False
            self._rx_buffer.clear()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "protocol": PROTOCOL_NAME,
                "version": PROTOCOL_VERSION,
                "serial": {
                    "connected": self.connected,
                    "port": self._port if self.connected else "",
                    "configured_port": self._port,
                    "baudrate": self._baudrate,
                    "address": self._address,
                },
                "position": {"pan": self.current_pan, "tilt": self.current_tilt},
                "motion": {
                    "completion": dict(self.completion_config),
                    "manual_jog_lease_max_ms": 3000,
                    "manual_jog_token_supported": True,
                    "state": self.motion_state,
                    "id": self._pending_motion.get("motion_id") if self._pending_motion else None,
                    "completed_id": self._completed_motion_id,
                    "failed_id": self._failed_motion_id,
                    "last_error": self._last_motion_error,
                },
                "homed": self._home_valid,
                "monitor": dict(self.monitor_config),
                "settings": self._device_settings_status(),
                "auto_reconnect": {
                    **dict(self.auto_reconnect_config),
                    "scanning": self._auto_scan_active,
                    "health_misses": self._health_misses,
                },
                "laser": {
                    "plc_mode": self._laser_plc_mode,
                    "control_priority": self.laser_control_priority,
                    "state_profiles": deepcopy(self.laser_state_profiles),
                    "active_state": self._laser_active_state,
                    "active_profile": dict(self._laser_active_profile) if self._laser_active_state else None,
                    "state_allowed": self._laser_state_allowed,
                    "control_mode": "manual" if self._laser_manual_deadline else "auto",
                    "manual_override": bool(self._laser_manual_deadline),
                    "manual_remaining_s": max(0.0, self._laser_manual_deadline - time.monotonic()) if self._laser_manual_deadline else 0.0,
                    "manual_allowed": self._laser_manual_allowed,
                    "manual_block_reason": self._laser_manual_block_reason,
                    "power_saving": self._power_saving,
                    "backend": self._laser_backend,
                    "numbering": "BOARD" if self._laser_backend == "gpio" else None,
                    "pin": 31 if self._laser_backend == "gpio" else None,
                    "active_high": True,
                    "feedback_available": False,
                    "on_source": "commanded_gpio_level",
                    "error": self._laser_error,
                    "bypass": self.laser_bypass,
                    "on": self.laser_on,
                    "connected": self.laser_connected,
                    "port": self._laser_port if self.laser_connected else "",
                    "baudrate": self._laser_baudrate,
                    "address": self._laser_address,
                    "last_tx": self.laser_last_tx,
                    "last_rx": self.laser_last_rx,
                },
                "scanning": self.scanning,
                "last_tx": self.last_tx,
                "last_rx": self.last_rx[-10:],
            }

    @staticmethod
    def ports() -> list[dict[str, str]]:
        return [
            {
                "port": item.device,
                "description": item.description or "Serial Port",
                "hwid": item.hwid,
            }
            for item in list_ports.comports()
        ]

    def connect(
        self,
        port: str,
        baudrate: int = 9600,
        address: int = 1,
        *,
        enable_auto_reconnect: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if self._shutdown.is_set():
                raise HeadlessApiError("SERVICE_STOPPING", "server is shutting down")
            self.close()
            try:
                self._serial = serial.Serial(
                    port=port,
                    baudrate=int(baudrate),
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0,
                    write_timeout=0.5,
                    xonxoff=False,
                    rtscts=False,
                    dsrdtr=False,
                )
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
            except serial.SerialException as exc:
                self._serial = None
                raise HeadlessApiError("SERIAL_OPEN_FAILED", str(exc)) from exc
            self._port = port
            self._baudrate = int(baudrate)
            self._address = int(address)
            self._health_misses = 0
            self._last_serial_tx_at = 0.0
            if enable_auto_reconnect:
                self.auto_reconnect_config["enabled"] = True
                self.auto_reconnect_config["baudrates"] = [int(baudrate)]
                self.auto_reconnect_config["first_address"] = int(address)
                self.auto_reconnect_config["last_address"] = int(address)
            self._save_settings()
            self._apply_power_on_home()
            return {"connected": self.connected, "port": port, "baudrate": baudrate, "address": address}

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            self.auto_reconnect_config["enabled"] = False
            self._save_settings()
        self.close()
        return {"connected": False}

    def scan(
        self,
        *,
        baudrates: list[int] | None = None,
        ports: list[str] | None = None,
        first_address: int = 1,
        last_address: int = 1,
        timeout_ms: int = 200,
        enable_auto_reconnect: bool = True,
    ) -> dict[str, Any]:
        baudrates = baudrates or [9600]
        ports = ports or [item.device for item in list_ports.comports()]
        for port in ports:
            for baudrate in baudrates:
                for address in range(first_address, last_address + 1):
                    if self._scan_cancel.is_set():
                        return {"found": False, "cancelled": True}
                    try:
                        probe = serial.Serial(
                            port=port,
                            baudrate=int(baudrate),
                            bytesize=serial.EIGHTBITS,
                            parity=serial.PARITY_NONE,
                            stopbits=serial.STOPBITS_ONE,
                            timeout=0,
                            write_timeout=0.5,
                        )
                    except serial.SerialException:
                        continue
                    try:
                        command = query_pan(address)
                        probe.reset_input_buffer()
                        probe.write(command.data)
                        probe.flush()
                        deadline = time.perf_counter() + timeout_ms / 1000.0
                        received = bytearray()
                        while time.perf_counter() < deadline:
                            if self._scan_cancel.is_set() or self._shutdown.is_set():
                                return {"found": False, "cancelled": True}
                            count = probe.in_waiting
                            if count:
                                received.extend(probe.read(count))
                                if self._scan_has_match(received, address, command.data[6]):
                                    probe.close()
                                    self.connect(
                                        port,
                                        baudrate,
                                        address,
                                        enable_auto_reconnect=enable_auto_reconnect,
                                    )
                                    return {
                                        "found": True,
                                        "port": port,
                                        "baudrate": baudrate,
                                        "address": address,
                                    }
                            time.sleep(0.005)
                    finally:
                        probe.close()
        return {"found": False, "ports_checked": ports, "baudrates": baudrates}

    @staticmethod
    def _scan_has_match(data: bytes | bytearray, address: int, tx_checksum: int) -> bool:
        blob = bytes(data)
        for start in range(max(0, len(blob) - 20), max(0, len(blob) - 6)):
            candidate = blob[start : start + 7]
            if (
                len(candidate) == 7
                and candidate[0] == 0xFF
                and candidate[1] == address
                and candidate[3] in (0x59, 0x5B)
                and verify_frame_checksum(candidate)
            ):
                return True
        for start in range(max(0, len(blob) - 12), max(0, len(blob) - 3)):
            candidate = blob[start : start + 4]
            if (
                len(candidate) == 4
                and candidate[0] == 0xFF
                and candidate[1] == address
                and candidate[3] == ((tx_checksum + candidate[2]) & 0xFF)
            ):
                return True
        return False


    @staticmethod
    def _require_confirm(params: dict[str, Any]) -> None:
        if params.get("confirm") is not True:
            raise HeadlessApiError("CONFIRM_REQUIRED", "confirm=true is required")

    @staticmethod
    def _bool(params: dict[str, Any], name: str, default: bool | None = None) -> bool:
        value = params.get(name, default)
        if not isinstance(value, bool):
            raise HeadlessApiError("INVALID_PARAMS", f"{name} must be boolean")
        return value

    @staticmethod
    def _int(
        params: dict[str, Any],
        name: str,
        default: int | None = None,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = params.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise HeadlessApiError("INVALID_PARAMS", f"{name} must be integer")
        if not minimum <= value <= maximum:
            raise HeadlessApiError("OUT_OF_RANGE", f"{name} must be {minimum}..{maximum}")
        return value

    @staticmethod
    def _float(
        params: dict[str, Any],
        name: str,
        default: float | None = None,
        *,
        minimum: float,
        maximum: float,
        required: bool = True,
    ) -> float | None:
        if name not in params or params[name] is None:
            if required:
                raise HeadlessApiError("INVALID_PARAMS", f"{name} is required")
            return default
        value = params[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HeadlessApiError("INVALID_PARAMS", f"{name} must be number")
        result = float(value)
        if not minimum <= result <= maximum:
            raise HeadlessApiError("OUT_OF_RANGE", f"{name} must be {minimum}..{maximum}")
        return result

    @staticmethod
    def _enum(params: dict[str, Any], name: str, allowed: set[str], default: str | None = None) -> str:
        value = params.get(name, default)
        if not isinstance(value, str) or value not in allowed:
            raise HeadlessApiError("INVALID_PARAMS", f"{name} must be one of {sorted(allowed)}")
        return value

    @staticmethod
    def protocol_catalog() -> dict[str, Any]:
        return api_protocol_catalog(tuple(sorted(set(SUPPORTED_COMMANDS) | LASER_API_COMMANDS | DRAWING_API_COMMANDS | PTM_SETTINGS_COMMANDS)))

    def send(
        self,
        command: OutgoingCommand,
        *,
        wait_ms: int = 80,
        min_gap_s: float | None = None,
        response_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if not self.connected or self._serial is None:
                raise HeadlessApiError(
                    "SERIAL_NOT_CONNECTED",
                    "serial port is not connected",
                )

            try:
                # 일반 명령은 기존 320ms 간격을 유지한다.
                #
                # 단, Pan/Tilt 절대위치 명령처럼 연속 송신이 필요한 경우에는
                # 호출 측에서 min_gap_s를 별도로 지정할 수 있다.
                gap_s = (
                    PELCO_MIN_COMMAND_GAP_S
                    if min_gap_s is None
                    else max(0.0, float(min_gap_s))
                )

                elapsed = time.monotonic() - self._last_serial_tx_at
                remaining = gap_s - elapsed

                if self._last_serial_tx_at and remaining > 0:
                    time.sleep(remaining)

                position_speed = command.data[2:4] == b"\x00\x5f"
                if (position_speed or command.category in {"raw", "device", "maintenance"}
                        or command.data[2:6] in {b"\x00\x03\x00\x61", b"\x00\x05\x00\x61"}):
                    self._position_speed_frame = None
                self._serial.write(command.data)
                self._serial.flush()

                self._last_serial_tx_at = time.monotonic()
                if position_speed:
                    self._position_speed_frame = bytes(command.data)

                self.last_tx = {
                    "hex": frame_to_hex(command.data),
                    "description": command.description,
                    "category": command.category,
                    "time": time.time(),
                }

                deadline = time.perf_counter() + wait_ms / 1000.0
                received = bytearray()
                decoded = []

                while time.perf_counter() < deadline:
                    count = self._serial.in_waiting

                    if count:
                        chunk = self._serial.read(count)
                        if response_kind is not None:
                            decoded.extend(self._decode_blob(chunk, command.data[6]))
                            if any(frame["kind"] == response_kind for frame in decoded):
                                break
                        else:
                            received.extend(chunk)

                    time.sleep(0.005)

            except serial.SerialException as exc:
                self.close()
                raise HeadlessApiError(
                    "SERIAL_IO_FAILED",
                    str(exc),
                ) from exc

            if response_kind is None:
                decoded = self._decode_blob(bytes(received), command.data[6])

            self.last_rx.extend(decoded)
            self.last_rx = self.last_rx[-50:]

            return decoded

    def _decode_blob(self, blob: bytes, last_tx_checksum: int | None = None) -> list[dict[str, Any]]:
        frames = []
        self._rx_buffer.extend(blob)
        while self._rx_buffer:
            start = self._rx_buffer.find(0xFF)
            if start < 0:
                self._rx_buffer.clear()
                break
            del self._rx_buffer[:start]
            if len(self._rx_buffer) < 4:
                break
            if len(self._rx_buffer) >= 7 and verify_frame_checksum(self._rx_buffer[:7]):
                size = 7
            elif decode_response(bytes(self._rx_buffer[:4]), last_tx_checksum).checksum_ok is True:
                size = 4
            elif self._rx_buffer[2] == 0 and self._rx_buffer[3] in {0x01, 0x59, 0x5B, 0x5D, 0x6D}:
                # A position response can arrive across separate read windows.
                if len(self._rx_buffer) < 7:
                    break
                del self._rx_buffer[0]
                continue
            else:
                size = 4
            frame = bytes(self._rx_buffer[:size])
            del self._rx_buffer[:size]
            response = decode_response(frame, last_tx_checksum)
            if response.address != self._address or response.checksum_ok is not True:
                continue
            if self._tracker and response.kind in {"pan", "tilt"} and isinstance(response.value, (int, float)):
                result = self._tracker.update(response.kind, float(response.value))
                self.motion_state = result.status
                if result.status != "tracking":
                    self._tracker = None
                    if result.status == "completed":
                        self._finish_pending_motion()
                    else:
                        self._finish_pending_motion(
                            error=("MOTION_FAILED", result.message or "movement failed")
                        )
            if response.kind == "pan" and isinstance(response.value, (int, float)):
                self.current_pan = float(response.value)
            elif response.kind == "tilt" and isinstance(response.value, (int, float)):
                self.current_tilt = float(response.value)
            frames.append(
                {
                    "hex": frame.hex(" ").upper(),
                    "kind": response.kind,
                    "address": response.address,
                    "value": response.value,
                    "checksum_ok": response.checksum_ok,
                    "description": response.description,
                }
            )
        return frames

    def _recipe_motion_target(
        self, recipe_id: str, point: RecipePoint
    ) -> tuple[dict[str, float], dict[str, Any] | None]:
        # 제품 자세 보정은 도면 캘리브레이션 단계에서 Pan/Tilt에 이미 반영된다.
        # 레시피의 product_rotation_deg / product_tilt_deg는 레거시 확인용 읽기전용이다.
        self.recipe_store.get_recipe(recipe_id)  # recipe_id 유효성 검증 유지
        return {"pan": point.pan, "tilt": point.tilt}, None

    def command(
        self,
        command: str,
        params: dict[str, Any],
        *,
        client_id: str | None = None,
        request_id: Any = None,
    ) -> dict[str, Any]:
        if self._shutdown.is_set():
            raise HeadlessApiError("SERVICE_STOPPING", "server is shutting down")
        if command in {"motor.status", "ptm.status"}:
            command = "system.status"
        if command == "protocol.hello":
            requested_version = str(params.get("version", PROTOCOL_VERSION))
            if requested_version != PROTOCOL_VERSION:
                raise HeadlessApiError(
                    "UNSUPPORTED_VERSION",
                    f"supported protocol version is {PROTOCOL_VERSION}",
                )
            return {
                "protocol": PROTOCOL_NAME,
                "version": PROTOCOL_VERSION,
                "client_id": client_id,
                "states": self.protocol_states(),
            }
        if command == "system.heartbeat":
            return {"alive": True, "echo": dict(params), "server_time": time.time()}
        if command == "laser.power_save":
            with self._laser_lock:
                plc_mode = self._int(params, "plc_mode", self._laser_plc_mode, minimum=0, maximum=65535)
                enabled = self._bool(params, "enabled")
                automatic_off = self._bool(params, "automatic_off", False)
                allowed = self._bool(params, "manual_allowed", self._laser_manual_allowed)
                state_allowed = self._bool(params, "state_allowed", allowed)
                local_allowed = self._bool(params, "local_allowed", True)
                self._laser_core_policy = {**params, "plc_mode": plc_mode, "enabled": enabled,
                                           "automatic_off": automatic_off, "manual_allowed": allowed,
                                           "state_allowed": state_allowed}
                mode = 1 if self.laser_control_priority else plc_mode
                if self.laser_control_priority:
                    enabled = False
                    automatic_off = not local_allowed
                    allowed = state_allowed = local_allowed
                if mode != self._laser_applied_mode:
                    self._laser_write_coil(False)
                    self._laser_deadline = 0.0
                    self._laser_applied_mode = mode
                self._laser_plc_mode = plc_mode
                if mode not in {1, 2, 3, 4, 5}:
                    allowed = False
                    automatic_off = True
                self._power_saving = enabled
                self._laser_automatic_off = automatic_off
                self._laser_manual_allowed = allowed
                self._laser_manual_block_reason = (
                    "" if allowed else str(params.get("local_block_reason" if self.laser_control_priority else "manual_block_reason")
                                           or "Core가 레이저 출력을 허용하지 않았습니다.")
                )
                self._laser_state_allowed = state_allowed
                if mode not in {1, 2, 3, 4, 5}:
                    self._laser_state_allowed = False
                preserve_manual = bool(self._laser_manual_deadline and allowed)
                preserve_state = bool(self._laser_active_state and self._laser_state_allowed
                                      and not enabled and (not automatic_off or self._laser_active_state == "work_complete"))
                if (enabled or automatic_off or not allowed or not self._laser_state_allowed) and not (preserve_manual or preserve_state):
                    self._laser_generation += 1
                    try:
                        self._laser_write_coil(False)
                    except HeadlessApiError:
                        self._laser_deadline = time.monotonic() + 0.2
                        raise
                if mode in {2, 3, 4, 5} and self._laser_state_allowed and not enabled and not automatic_off:
                    self._apply_laser_state("plc_override", profile={
                        "mode": "on" if mode == 2 else "blink",
                        "interval_ms": {2: 500, 3: 500, 4: 250, 5: 100}[mode],
                    })
                return {"sleeping": enabled, "laser_on": self.laser_on, "plc_mode": plc_mode,
                        "manual_override": preserve_manual, "manual_allowed": allowed}
        if command == "power_save.settings":
            # Core may reconnect PTM while applying configuration. Do not hold _lock here.
            if "timeout_minutes" in params:
                minutes = self._float(params, "timeout_minutes", minimum=1 / 60, maximum=1440)
                expected = self._float(params, "expected_timeout_s", minimum=1, maximum=86400)
                config = self._core_request("monitor.config.apply", {
                    "patch": {"runtime": {"power_save_timeout_s": minutes * 60}},
                    "expected": {"runtime.power_save_timeout_s": expected},
                }, timeout_s=10.0)
            else:
                config = self._core_request("get_config", {})
            try:
                seconds = float(config["runtime"]["power_save_timeout_s"])
                if not math.isfinite(seconds) or not 1 <= seconds <= 86400:
                    raise ValueError("invalid Core power-save timeout")
            except (KeyError, TypeError, ValueError) as exc:
                raise HeadlessApiError("CORE_SETTINGS_FAILED", "Core power-save settings are unavailable") from exc
            return {"timeout_s": seconds, "timeout_minutes": seconds / 60, "managed_by": "Core"}
        if command == "power_save.wake":
            port = self._int(params, "port", int(os.environ.get("PT503_CORE_PORT", "8000")),
                             minimum=1, maximum=65535)
            return self._core_request("power_save.wake", {}, host=str(params.get("host") or "") or None,
                                      port=port, error_code="CORE_WAKE_FAILED")
        # Scan cancellation and status must stay available during discovery.
        if command == "serial.scan_cancel":
            self._scan_cancel.set()
            return {"accepted": True}
        if command == "serial.scan":
            with self._lock:
                if self.scanning or self._auto_scan_active or self.connected:
                    raise HeadlessApiError("BUSY", "Disconnect before scanning")
                self.scanning = True
                self._scan_cancel.clear()
            try:
                scan_ports = params.get("ports")
                if scan_ports is not None and (
                    not isinstance(scan_ports, list)
                    or not all(isinstance(item, str) for item in scan_ports)
                ):
                    raise HeadlessApiError("INVALID_PARAMS", "ports must be string array")
                return self.scan(
                    baudrates=[self._int({"baud": b}, "baud", minimum=1200, maximum=115200) for b in params.get("baudrates", [9600])],
                    ports=scan_ports,
                    first_address=self._int(params, "first_address", 1, minimum=1, maximum=255),
                    last_address=self._int(params, "last_address", 1, minimum=1, maximum=255),
                    timeout_ms=self._int(params, "timeout_ms", 200, minimum=50, maximum=2000),
                )
            finally:
                with self._lock:
                    self.scanning = False
        if command == "serial.auto_reconnect":
            with self._lock:
                updated = dict(self.auto_reconnect_config)
                if "enabled" in params:
                    updated["enabled"] = self._bool(params, "enabled")
                if "baudrates" in params:
                    baudrates = params["baudrates"]
                    if not isinstance(baudrates, list) or not baudrates:
                        raise HeadlessApiError("INVALID_PARAMS", "baudrates must be non-empty array")
                    updated["baudrates"] = [
                        self._int({"baud": item}, "baud", minimum=1200, maximum=115200)
                        for item in baudrates
                    ]
                if "first_address" in params:
                    updated["first_address"] = self._int(
                        params, "first_address", minimum=1, maximum=255
                    )
                if "last_address" in params:
                    updated["last_address"] = self._int(
                        params, "last_address", minimum=1, maximum=255
                    )
                if "timeout_ms" in params:
                    updated["timeout_ms"] = self._int(
                        params, "timeout_ms", minimum=50, maximum=2000
                    )
                if "retry_interval_s" in params:
                    updated["retry_interval_s"] = self._float(
                        params,
                        "retry_interval_s",
                        minimum=0.5,
                        maximum=3600.0,
                    )
                if "health_interval_s" in params:
                    updated["health_interval_s"] = self._float(
                        params,
                        "health_interval_s",
                        minimum=0.5,
                        maximum=3600.0,
                    )
                if (
                    int(updated["first_address"])
                    > int(updated["last_address"])
                ):
                    raise HeadlessApiError(
                        "INVALID_PARAMS", "first_address must be <= last_address"
                    )
                previous = self.auto_reconnect_config
                self.auto_reconnect_config = updated
                try:
                    self._save_settings()
                except OSError:
                    self.auto_reconnect_config = previous
                    raise
                return dict(self.auto_reconnect_config)
        with self._lock:
            if self._shutdown.is_set():
                raise HeadlessApiError("SERVICE_STOPPING", "server is shutting down")
            manual_off_error = None
            if (command.startswith(("motion.", "home.", "preset.", "scan.", "cruise.", "pattern.",
                                    "pelco.", "lens.", "aux.", "camera.", "maintenance.", "zero."))
                    or command in {"point.goto", "serial.connect", "serial.disconnect", "raw.send"}
                    or command in set(protocol_named_command_names()) - {"magnification.query", "device.query_diagnostics"}):
                preserve_manual = (command in {"motion.jog", "motion.absolute", "motion.relative",
                                               "motion.goto", "point.goto", "home.start"}
                                   or (command == "motion.stop" and params.get("preserve_laser_manual") is True))
                preserve_manual = preserve_manual and bool(self._laser_manual_deadline)
                preserve_manual |= (command == "motion.stop" and params.get("preserve_laser") is True)
                preserve_manual |= (self._effective_laser_mode() in {2, 3, 4, 5} and self._laser_state_allowed
                                    and command in {"motion.jog", "motion.absolute", "motion.relative",
                                                    "motion.goto", "point.goto", "home.start", "motion.stop"})
                preserve_manual |= (command == "motion.stop" and "jog_token" in params
                                    and (self._active_jog is None or params["jog_token"] != self._jog_token))
                preserve_manual |= command in {"motion.completion_config", "motion.tolerance"} and not params
                with self._laser_lock:
                    if (self._laser_manual_deadline or self._laser_active_state) and not preserve_manual:
                        try:
                            self._laser_write_coil(False)
                        except HeadlessApiError as exc:
                            if command not in {"motion.stop", "motion.force_stop"}:
                                raise
                            self._laser_manual_deadline = 0.0
                            self._laser_deadline = time.monotonic() + 0.2
                            manual_off_error = exc
            if (
                self.scanning
                and command not in {"system.status", "system.ping", "system.commands", "serial.ports", "serial.scan_cancel"}
                and not command.startswith("laser.")
                and not command.startswith("drawing.")
            ):
                raise HeadlessApiError("BUSY", "Scanning")
            if command == "motion.tolerance":
                command = "motion.completion_config"
            if command == "recipe.select":
                recipe_id = str(params.get("recipe_id", "")).strip()
                recipe = self.recipe_store.get_recipe(recipe_id)
                self._selected_recipe_id = recipe.id
                self._selected_point_id = None
                return {"recipe_id": recipe.id, "name": recipe.name}
            if command == "point.select":
                recipe_id = str(params.get("recipe_id") or self._selected_recipe_id or "").strip()
                point_id = str(params.get("point_id", "")).strip()
                recipe_id = self.recipe_store.get_recipe(recipe_id).id
                point = self.recipe_store.get_point(recipe_id, point_id)
                self._selected_recipe_id = recipe_id
                self._selected_point_id = point.id
                return {"recipe_id": recipe_id, "point_id": point.id, "name": point.name}
            if command in {"motion.goto", "point.goto"}:
                recipe_id = str(params.get("recipe_id") or self._selected_recipe_id or "").strip()
                point_id = str(params.get("point_id") or self._selected_point_id or "").strip()
                if not recipe_id or not point_id:
                    raise HeadlessApiError(
                        "SELECTION_REQUIRED", "recipe_id and point_id must be selected first"
                    )
                recipe_id = self.recipe_store.get_recipe(recipe_id).id
                point = self.recipe_store.get_point(recipe_id, point_id)
                if not point.enabled:
                    raise HeadlessApiError("DISABLED", "point is disabled")
                self._selected_recipe_id = recipe_id
                self._selected_point_id = point_id
                target, correction = self._recipe_motion_target(recipe_id, point)
                with self._laser_lock:
                    auto_laser = (self._bool(params, "laser_after_move", True)
                                  and not self._laser_manual_deadline
                                  and not self._power_saving and not self._laser_automatic_off
                                  and self._laser_manual_allowed
                                  and (self._laser_backend == "gpio" or self.laser_connected))
                    if "laser_permit_token" in params:
                        auto_laser = auto_laser and params["laser_permit_token"] == self._laser_generation
                    if (not self._laser_manual_deadline and self._effective_laser_mode() not in {2, 3, 4, 5} and not self.laser_bypass
                            and (self._laser_backend == "gpio" or self.laser_connected)):
                        self._laser_write_coil(False)
                    laser_generation = self._laser_generation
                result = self.command(
                    "motion.absolute",
                    target,
                    client_id=client_id,
                    request_id=request_id,
                )
                if self._pending_motion is not None:
                    self._pending_motion.update(
                        command=command, recipe_id=recipe_id, point_id=point_id,
                        laser_after_move=auto_laser, laser_generation=laser_generation,
                    )
                return {
                    **result,
                    "recipe_id": recipe_id,
                    "point_id": point_id,
                    "pose_correction": correction,
                }
            if command == "home.start":
                self._home_valid = False
                result = self.command(
                    "motion.absolute",
                    {"pan": 0.0, "tilt": 0.0},
                    client_id=client_id,
                    request_id=request_id,
                )
                if self._pending_motion is not None:
                    self._pending_motion.update(command=command, home=True)
                return {**result, "homing": True}
            force_stop = command == "motion.force_stop"
            if force_stop:
                command = "motion.stop"
            if command == "motion.stop" and "jog_token" in params and not force_stop:
                token = str(params.get("jog_token") or "")
                if not token or self._active_jog is None or token != self._jog_token:
                    return {"accepted": False, "stopped": False, "reason": "manual jog was ended or superseded"}
            for key in ("pan", "tilt", "pan_delta", "tilt_delta", "tolerance_deg"):
                if (
                    command != "motion.jog"
                    and key in params
                    and params[key] not in (None, "")
                    and not math.isfinite(float(params[key]))
                ):
                    raise HeadlessApiError("INVALID_PARAMS", key + " must be finite")
            if command == "serial.connect":
                self._int(params, "address", 1, minimum=1, maximum=255)
                self._int(params, "baudrate", 9600, minimum=1200, maximum=115200)
            if command == "motion.jog":
                if "pan_speed" in params or "tilt_speed" in params:
                    raise HeadlessApiError("INVALID_PARAMS", "Use speed_level (1..8)")
                # Opt-in lease for clients relayed through a persistent Core socket.
                # Legacy duration_ms and unleased continuous JOG remain unchanged.
                lease_ms = (
                    self._int(params, "lease_ms", minimum=100, maximum=3000)
                    if "lease_ms" in params else None
                )
                token = params.get("jog_token")
                if token is not None and (not isinstance(token, str) or not token or len(token) > 128 or lease_ms is None):
                    raise HeadlessApiError("INVALID_PARAMS", "jog_token requires a lease and 1..128 characters")
                pan = {"left": PanDirection.LEFT, "right": PanDirection.RIGHT, "stop": PanDirection.STOP}[self._enum(params, "pan", {"left", "right", "stop"}, "stop")]
                tilt = {"up": TiltDirection.UP, "down": TiltDirection.DOWN, "stop": TiltDirection.STOP}[self._enum(params, "tilt", {"up", "down", "stop"}, "stop")]
                if self.device_settings["invert_pan"]:
                    pan = {PanDirection.LEFT: PanDirection.RIGHT, PanDirection.RIGHT: PanDirection.LEFT}.get(pan, pan)
                if self.device_settings["invert_tilt"]:
                    tilt = {TiltDirection.UP: TiltDirection.DOWN, TiltDirection.DOWN: TiltDirection.UP}.get(tilt, tilt)
                if "speed_level" in params:
                    level = self._int(params, "speed_level", 5, minimum=1, maximum=8)
                    pl = tl = level
                else:
                    pl = self._int(params, "pan_level", 5, minimum=1, maximum=8)
                    tl = self._int(params, "tilt_level", pl, minimum=1, maximum=8)

                with self._lock:
                    self._finish_pending_motion(
                        error=("MOTION_STOPPED", "movement was superseded by manual motion")
                    )
                    if pan is PanDirection.STOP and tilt is TiltDirection.STOP:
                        self._active_jog = None
                        self._jog_owner_client_id = None
                        self._jog_next_resend = 0.0
                        self._tracker = None
                        self.motion_state = "stopped"
                        rx = self.send(stop(self._address), wait_ms=80)
                        return {"accepted": True, "stopped": True, "rx": rx}

                    jog_key = (pan, tilt, pl, tl)
                    unchanged = self._active_jog == jog_key
                    if unchanged:
                        # Old clients may still send periodic motion.jog requests.
                        # Ignore duplicates WITHOUT moving the server's own next
                        # physical refresh time; otherwise duplicate client keepalives
                        # could postpone the server-side Pelco refresh forever.
                        rx: list[dict[str, Any]] = []
                    else:
                        rx = self.send(
                            manual_motion(
                                self._address,
                                pan,
                                tilt,
                                manual_speed_value(pl),
                                manual_speed_value(tl),
                            ),
                            wait_ms=80,
                        )
                        self._active_jog = jog_key
                        self._jog_next_resend = time.monotonic() + JOG_RESEND_INTERVAL_S

                    # Ownership follows the client that most recently asserted
                    # the JOG.  HTTP/Web requests have client_id=None and are not
                    # tied to unrelated TCP socket disconnects.
                    self._jog_owner_client_id = client_id
                    self._jog_token = token
                    self._jog_lease_deadline = (
                        time.monotonic() + lease_ms / 1000.0 if lease_ms is not None else 0.0
                    )
                    self._tracker = None
                    self.motion_state = "jog"

                return {
                    "accepted": True,
                    "continuous": True,
                    "unchanged": unchanged,
                    "rx": rx,
                    "pan_level": pl,
                    "tilt_level": tl,
                    "speed_level": pl if pl == tl else None,
                    "resend_interval_ms": int(JOG_RESEND_INTERVAL_S * 1000),
                    "lease_ms": lease_ms,
                    "jog_token": token,
                }
            if command in {"motion.stop", "cruise.stop", "scan.stop"}:
                self._jog_next_resend = 0.0
                self._active_jog = None
                self._jog_owner_client_id = None
                self._tracker = None
                self.motion_state = "stopped"
                self._finish_pending_motion(
                    error=("MOTION_STOPPED", "movement was stopped by request")
                )
            if command == "motion.absolute":
                pan = self._float(params, "pan", required=False, minimum=0, maximum=359.99)
                tilt = self._float(params, "tilt", required=False, minimum=-60, maximum=60)
                if pan is None and tilt is None:
                    raise HeadlessApiError("INVALID_PARAMS", "pan or tilt is required")
                self._tracker = None
                self._finish_pending_motion(
                    error=("MOTION_STOPPED", "movement was superseded by a new target")
                )
                # Absolute motion supersedes continuous manual JOG.
                self._jog_next_resend = 0.0
                self._active_jog = None
                self._jog_owner_client_id = None
                # Legacy speed fields are ignored; every target move uses maximum.
                params = {**params, "pan_speed": AUTO_PAN_SPEED, "tilt_speed": AUTO_TILT_SPEED, "axis_delay_ms": 0}
            if command in {"scan.speed", "cruise.speed"}:
                params = {**params, "pan_speed": AUTO_PAN_SPEED, "tilt_speed": AUTO_TILT_SPEED}
            if command in {"preset.speed_adjust", "scan.speed_adjust"}:
                setter = set_position_speed if command.startswith("preset") else set_scan_speed
                return {"accepted": True, "fixed_maximum": True, "rx": self.send(setter(self._address, AUTO_PAN_SPEED, AUTO_TILT_SPEED))}
            if command in {"motion.relative", "preset.call", "preset.goto", "pelco.flip", "pelco.zero_pan", "pattern.run", "preset.scan", "scan.start", "cruise.start", "camera.scan", "home.auto", "home.after"}:
                self._jog_next_resend = 0.0
                self._active_jog = None
                self._jog_owner_client_id = None
                self._tracker = None
                self._finish_pending_motion(
                    error=("MOTION_STOPPED", "movement was superseded by another motion mode")
                )
                for setter in (set_position_speed, set_scan_speed, set_cruise_speed):
                    self.send(setter(self._address, AUTO_PAN_SPEED, AUTO_TILT_SPEED))
            if command in {"maintenance.restart", "maintenance.factory_default", "maintenance.self_check", "pelco.remote_reset", "zero.set"}:
                self._require_confirm(params)
                if self._tracker is not None or self._active_jog is not None:
                    self.send(stop(self._address), wait_ms=80)
                self._home_valid = False
                self._tracker = None
                self._jog_next_resend = 0.0
                self._active_jog = None
                self._jog_owner_client_id = None
                self._finish_pending_motion(
                    error=("MOTION_STOPPED", "device reference was reset by request")
                )
                self.motion_state = "stopped"
            if command in {"laser.off", "laser.on"}:
                with self._laser_lock:
                    self._laser_active_state = None
                    self._laser_blink_next = 0.0
                    try:
                        result = self._command_impl(command, params)
                    except HeadlessApiError:
                        if self.laser_connected or (self._laser_backend == "gpio" and self._gpio_laser.connected):
                            self._laser_deadline = time.monotonic() + 0.2
                        raise
                    self._laser_deadline = 0.0
                    return {**result, "permit_token": self._laser_generation}
            if command == "laser.pulse":
                if self._pending_motion is not None:
                    raise HeadlessApiError("MOTION_ACTIVE", "Laser remains OFF until movement completes")
                duration = self._int(params, "duration_ms", 500, minimum=50, maximum=60000)
                with self._laser_lock:
                    self._laser_active_state = None
                    self._laser_blink_next = 0.0
                    self._laser_deadline = time.monotonic() + duration / 1000
                    self._laser_manual_deadline = self._laser_deadline
                    try:
                        result = self._laser_write_coil(True)
                    except HeadlessApiError:
                        # ON can reach the relay even when its reply is lost.
                        self._laser_deadline = time.monotonic() + 0.2
                        raise
                return {**result, "duration_ms": duration}
            if command == "laser.state":
                with self._laser_lock:
                    if params.get("recipe_id") is not None:
                        recipe_id = self.recipe_store.get_recipe(str(params["recipe_id"])).id
                        point = self.recipe_store.get_point(recipe_id, str(params["point_id"]))
                        selected = self.recipe_store.get_point(recipe_id, str(self._selected_point_id or ""))
                        if recipe_id != self._selected_recipe_id or point.id != selected.id:
                            raise HeadlessApiError("STALE_TARGET", "laser state does not match selected target")
                    try:
                        return self._apply_laser_state(str(params["state"]), manual=self._bool(params, "manual", False))
                    except HeadlessApiError:
                        self._laser_deadline = time.monotonic() + 0.2
                        raise
            if command == "motion.completion_config" and "tolerance_deg" in params:
                self._float(params, "tolerance_deg", minimum=0.01, maximum=10)
            if command == "point.upsert":
                self._float(params, "pan", minimum=0, maximum=359.99)
                self._float(params, "tilt", minimum=-60, maximum=60)
                self._int(params, "dwell_ms", 0, minimum=0, maximum=3600000)
                params = {**params, "pan_speed": AUTO_PAN_SPEED, "tilt_speed": AUTO_TILT_SPEED, "enabled": self._bool(params, "enabled", True)}
            result = self._command_impl(command, params)
            if command == "motion.absolute":
                self._jog_next_resend = 0.0
                self._tracker = MotionTracker(
                    "API move",
                    target_pan=float(params["pan"]) if params.get("pan") is not None else None,
                    target_tilt=float(params["tilt"]) if params.get("tilt") is not None else None,
                    tolerance=self.completion_config["tolerance_deg"],
                    stable_samples=self.completion_config["stable_samples"],
                    timeout_seconds=self.completion_config["timeout_s"],
                )
                self.motion_state = "tracking"
                self._pending_motion = {
                    "motion_id": uuid.uuid4().hex,
                    "client_id": client_id,
                    "request_id": request_id,
                    "command": "motion.absolute",
                    "recipe_id": None,
                    "point_id": None,
                    "home": False,
                }
                result = {**result, "motion_id": self._pending_motion["motion_id"]}
            if force_stop:
                with self._laser_lock:
                    if self.laser_connected or (self._laser_backend == "gpio" and self._gpio_laser.connected):
                        try:
                            self._laser_write_coil(False)
                        except HeadlessApiError:
                            self._laser_deadline = time.monotonic() + 0.2
                            raise
                    else:
                        self.laser_on = False
                    self._laser_deadline = 0.0
                result = {**result, "completed": True, "stopped": True}
                manual_off_error = None
            if manual_off_error is not None:
                raise manual_off_error
            return result

    def _command_impl(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if command == "ptm.settings":
            updated = dict(self.device_settings)
            for key in updated:
                if key in params:
                    updated[key] = None if key == "power_on_home" and params[key] is None else self._bool(params, key)
            if updated == self.device_settings:
                return self._device_settings_status()
            if self._active_jog is not None or self._pending_motion is not None:
                raise HeadlessApiError("BUSY", "Stop motion before saving PTM settings")
            home_changed = updated["power_on_home"] != self.device_settings["power_on_home"]
            if home_changed:
                self._require_confirm(params)
            previous = self.device_settings
            self.device_settings = updated
            try:
                self._save_settings()
            except OSError:
                self.device_settings = previous
                raise
            if home_changed:
                self._apply_power_on_home()
            return self._device_settings_status()
        if command == "system.ping":
            return {"protocol": PROTOCOL_NAME, "version": PROTOCOL_VERSION, "time": time.time()}
        if command == "laser.settings":
            enabled = self._bool(params, "bypass", self.laser_bypass)
            priority = self._bool(params, "control_priority", self.laser_control_priority)
            profiles = self._validated_laser_profiles(params["state_profiles"]) if "state_profiles" in params else self.laser_state_profiles
            with self._laser_lock:
                if enabled:
                    self._laser_manual_deadline = 0.0
                    if self.laser_connected or (self._laser_backend == "gpio" and self._gpio_laser.connected):
                        try:
                            self._laser_write_coil(False)
                        except HeadlessApiError:
                            self._laser_deadline = time.monotonic() + 0.2
                            raise
                    self.laser_on = False
                    self._laser_deadline = 0.0
                previous = self.laser_bypass
                previous_priority = self.laser_control_priority
                previous_profiles = self.laser_state_profiles
                self.laser_bypass = enabled
                self.laser_control_priority = priority
                self.laser_state_profiles = profiles
                try:
                    self._save_settings()
                except OSError:
                    self.laser_bypass = previous
                    self.laser_control_priority = previous_priority
                    self.laser_state_profiles = previous_profiles
                    raise
                if profiles != previous_profiles and self._laser_active_state:
                    self._laser_write_coil(False)
                if priority != previous_priority:
                    self.command("laser.power_save", self._laser_core_policy)
                return {"bypass": self.laser_bypass, "laser_on": self.laser_on,
                        "control_priority": self.laser_control_priority,
                        "state_profiles": deepcopy(self.laser_state_profiles)}
        if command == "system.status":
            return self.status()
        if command == "system.commands":
            return {"commands": sorted(set(SUPPORTED_COMMANDS) | LASER_API_COMMANDS | DRAWING_API_COMMANDS | PTM_SETTINGS_COMMANDS)}
        if command == "protocol.catalog":
            return self.protocol_catalog()
        if command == "serial.ports":
            return {"ports": self.ports()}
        if command == "serial.settings":
            self._position_speed_frame = None
            self._port = str(params.get("port", self._port))
            self._baudrate = self._int(
                params, "baudrate", self._baudrate, minimum=1200, maximum=115200
            )
            self._address = self._int(
                params, "address", self._address, minimum=1, maximum=255
            )
            self.auto_reconnect_config["baudrates"] = [self._baudrate]
            self.auto_reconnect_config["first_address"] = self._address
            self.auto_reconnect_config["last_address"] = self._address
            self._save_settings()
            return {
                "port": self._port,
                "baudrate": self._baudrate,
                "address": self._address,
            }

        if command in {"drawing.point_upsert", "drawing.calibrate", "drawing.reset_calibration",
                       "drawing.estimate_pan_tilt", "drawing.estimate_xy"} and params.get("recipe_id"):
            expected_recipe = self.recipe_store.get_recipe(str(params["recipe_id"])).id
            drawing = self.drawing_store.get_drawing(str(params["drawing_id"]))
            if drawing.get("selected_recipe_id") != expected_recipe:
                raise ValueError("활성 그룹/레시피가 변경되었습니다. 도면을 다시 불러오세요.")

        if command == "drawing.list":
            return {"drawings": self.drawing_store.list_drawings()}
        if command == "drawing.get":
            return {"drawing": self.drawing_store.get_drawing(str(params["drawing_id"]))}
        if command == "drawing.delete":
            drawing_id = str(params["drawing_id"])
            self.drawing_store.delete_drawing(drawing_id)
            return {"accepted": True, "drawing_id": drawing_id}
        if command == "drawing.update":
            return {"drawing": self.drawing_store.update_drawing(str(params["drawing_id"]), str(params["name"]))}
        if command == "drawing.reorder":
            return {"drawings": self.drawing_store.reorder_drawings([str(item) for item in params["ordered_ids"]])}
        if command == "drawing.select_point_set":
            return {
                "drawing": self.drawing_store.select_point_set(
                    str(params["drawing_id"]),
                    str(params["point_file"]),
                )
            }
        if command == "drawing.point_upsert":
            drawing_id = str(params["drawing_id"])
            point = self.drawing_store.upsert_point(drawing_id, params)
            return {"point": point, "drawing": self.drawing_store.get_drawing(drawing_id)}
        if command == "drawing.select_recipe":
            return {"drawing": self.drawing_store.select_recipe(str(params["drawing_id"]), str(params["recipe_id"]))}
        if command == "drawing.point_delete":
            self.drawing_store.delete_point(str(params["drawing_id"]), str(params["point_id"]))
            return {"accepted": True, "drawing": self.drawing_store.get_drawing(str(params["drawing_id"]))}
        if command == "drawing.point_reorder":
            drawing = self.drawing_store.reorder_points(
                str(params["drawing_id"]),
                [str(item) for item in params.get("ordered_ids", [])],
            )
            return {"accepted": True, "drawing": drawing}
        if command == "drawing.calibrate":
            return {
                "drawing": self.drawing_store.calibrate(
                    str(params["drawing_id"]),
                    product_rotation_deg=self._float(
                        params,
                        "product_rotation_deg",
                        0.0,
                        minimum=-360.0,
                        maximum=360.0,
                        required=False,
                    ) or 0.0,
                    product_tilt_deg=self._float(
                        params,
                        "product_tilt_deg",
                        0.0,
                        minimum=-360.0,
                        maximum=360.0,
                        required=False,
                    ) or 0.0,
                )
            }
        if command == "drawing.reset_calibration":
            return {"drawing": self.drawing_store.reset_calibration(str(params["drawing_id"]))}
        if command == "drawing.estimate_pan_tilt":
            return self.drawing_store.estimate_pan_tilt(
                str(params["drawing_id"]),
                self._float(params, "x", minimum=-1e9, maximum=1e9),
                self._float(params, "y", minimum=-1e9, maximum=1e9),
                self._float(
                    params,
                    "z",
                    None,
                    minimum=-1e9,
                    maximum=1e9,
                    required=False,
                ),
            )
        if command == "drawing.estimate_xy":
            return self.drawing_store.estimate_xy(
                str(params["drawing_id"]),
                self._float(params, "pan", minimum=0.0, maximum=359.99),
                self._float(params, "tilt", minimum=-60.0, maximum=60.0),
            )
        if command == "drawing.estimate_recipe":
            return self.drawing_store.estimate_recipe_coordinates(
                str(params["drawing_id"]),
                self.recipe_store,
                str(params["recipe_id"]),
            )
        if command == "drawing.export_recipe":
            return self.drawing_store.export_to_recipe(
                str(params["drawing_id"]),
                self.recipe_store,
                str(params["recipe_id"]),
            )
        if command == "drawing.import_recipe":
            return self.drawing_store.import_from_recipe(
                str(params["drawing_id"]),
                self.recipe_store,
                str(params["recipe_id"]),
            )
        if command == "serial.connect":
            return self.connect(
                str(params["port"]),
                int(params.get("baudrate", 9600)),
                int(params.get("address", 1)),
                enable_auto_reconnect=bool(params.get("auto_reconnect", True)),
            )
        if command == "serial.disconnect":
            return self.disconnect()
        if command == "serial.scan":
            return self.scan(
                baudrates=[int(item) for item in params.get("baudrates", [9600])],
                ports=(
                    [str(item) for item in params["ports"]]
                    if "ports" in params
                    else None
                ),
                first_address=int(params.get("first_address", 1)),
                last_address=int(params.get("last_address", params.get("first_address", 1))),
                timeout_ms=int(params.get("timeout_ms", 200)),
            )
        if command == "serial.scan_cancel":
            return {"accepted": True, "scanning": False}

        if command == "motion.stop":
            result = {"rx": self.send(stop(self._address), wait_ms=0)}
            if "jog_token" in params:
                result.update(accepted=True, stopped=True)
            return result
        if command == "motion.jog":
            pan = {"left": PanDirection.LEFT, "right": PanDirection.RIGHT, "stop": PanDirection.STOP}[
                self._enum(params, "pan", {"left", "right", "stop"}, "stop")
            ]
            tilt = {"up": TiltDirection.UP, "down": TiltDirection.DOWN, "stop": TiltDirection.STOP}[
                self._enum(params, "tilt", {"up", "down", "stop"}, "stop")
            ]
            return {
                "rx": self.send(
                    manual_motion(
                        self._address,
                        pan,
                        tilt,
                        self._int(params, "pan_speed", 32, minimum=0, maximum=63),
                        self._int(params, "tilt_speed", 32, minimum=0, maximum=63),
                    ),
                    wait_ms=int(params.get("wait_ms", 80)),
                )
            }
        if command == "motion.absolute":
            pan = self._float(
                params,
                "pan",
                required=False,
                minimum=0.0,
                maximum=359.99,
            )

            tilt = self._float(
                params,
                "tilt",
                required=False,
                minimum=-60.0,
                maximum=60.0,
            )

            if pan is None and tilt is None:
                raise HeadlessApiError(
                    "INVALID_PARAMS",
                    "pan or tilt is required",
                )

            rx = []

            # --------------------------------------------------------
            # 자동 위치이동 속도 설정
            # --------------------------------------------------------

            if "pan_speed" in params or "tilt_speed" in params:
                speed_command = set_position_speed(
                    self._address,
                    self._int(params, "pan_speed", 32, minimum=0, maximum=63),
                    self._int(params, "tilt_speed", 32, minimum=0, maximum=63),
                )
                if self._position_speed_frame != speed_command.data:
                    rx.extend(self.send(speed_command, wait_ms=80))

            # --------------------------------------------------------
            # Pan + Tilt 동시 이동
            #
            # Pelco-D 절대위치 명령은
            # Pan  = 0x4B
            # Tilt = 0x4D
            #
            # 하나의 프레임으로 동시에 지정할 수 없으므로
            # 두 프레임을 최대한 짧은 간격으로 연속 송신한다.
            # --------------------------------------------------------

            if pan is not None and tilt is not None:

                # Pan 목표 먼저 전송.
                # 응답 대기는 하지 않고 바로 Tilt 명령 준비.
                rx.extend(
                    self.send(
                        set_pan_position(
                            self._address,
                            pan,
                        ),
                        wait_ms=0,
                    )
                )

                # Tilt는 일반 320ms 간격을 적용하지 않고
                # 전용 50ms 간격으로 바로 전송.
                rx.extend(
                    self.send(
                        set_tilt_position(
                            self._address,
                            tilt,
                        ),
                        wait_ms=80,
                        min_gap_s=PELCO_ABSOLUTE_AXIS_GAP_S,
                    )
                )

            # --------------------------------------------------------
            # Pan 단독 이동
            # --------------------------------------------------------

            elif pan is not None:
                rx.extend(
                    self.send(
                        set_pan_position(
                            self._address,
                            pan,
                        ),
                        wait_ms=80,
                    )
                )

            # --------------------------------------------------------
            # Tilt 단독 이동
            # --------------------------------------------------------

            elif tilt is not None:
                rx.extend(
                    self.send(
                        set_tilt_position(
                            self._address,
                            tilt,
                        ),
                        wait_ms=80,
                    )
                )

            return {
                "accepted": True,
                "target": {
                    "pan": pan,
                    "tilt": tilt,
                },
                "rx": rx,
            }
        if command == "motion.completion_config":
            if "tolerance_deg" in params:
                self.completion_config["tolerance_deg"] = float(params["tolerance_deg"])
            if "stable_samples" in params:
                self.completion_config["stable_samples"] = self._int(params, "stable_samples", minimum=1, maximum=10)
            if "timeout_s" in params:
                self.completion_config["timeout_s"] = self._int(params, "timeout_s", minimum=2, maximum=120)
            return dict(self.completion_config)
        if command == "position.get":
            refresh = self._bool(params, "refresh", True)
            rx = []
            jogging = self._active_jog is not None
            # Preserve continuous manual motion.  Some PT heads visibly hesitate
            # when extended query frames are interleaved with a latched JOG.
            # During JOG return the latest cached position and defer the physical
            # query until manual motion is released/stopped.
            if refresh and not jogging:
                rx.extend(self.send(query_pan(self._address), wait_ms=120))
                rx.extend(self.send(query_tilt(self._address), wait_ms=120))
            return {
                "pan": self.current_pan,
                "tilt": self.current_tilt,
                "fresh_query_requested": refresh,
                "query_deferred_for_jog": bool(refresh and jogging),
                "rx": rx,
            }
        if command == "monitor.set":
            self.monitor_config["enabled"] = self._bool(params, "enabled")
            if "interval_ms" in params:
                self.monitor_config["interval_ms"] = self._int(params, "interval_ms", minimum=350, maximum=5000)
            return dict(self.monitor_config)

        if command == "recipe.list":
            return {"recipes": self.recipe_store.list_recipes()}
        if command == "recipe.pose_preview":
            recipe_id = self.recipe_store.get_recipe(str(params["recipe_id"])).id
            if params.get("point_id"):
                point = self.recipe_store.get_point(recipe_id, str(params["point_id"]))
                target, correction = self._recipe_motion_target(recipe_id, point)
                return {"recipe_id": recipe_id, "point_id": point.id, **target, "pose_correction": correction}
            targets = []
            for point in self.recipe_store.get_recipe(recipe_id).points:
                if point.enabled:
                    target, correction = self._recipe_motion_target(recipe_id, point)
                    targets.append({"point_id": point.id, **target, "pose_correction": correction})
            return {"recipe_id": recipe_id, "targets": targets}
        if command == "recipe.export":
            return {
                "document": self.recipe_store.export_document(
                    str(params["recipe_id"]) if params.get("recipe_id") else None
                )
            }
        if command == "recipe.import":
            return {"recipes": self.recipe_store.import_document(params.get("document"))}
        if command == "recipe.upsert":
            recipe = self.recipe_store.upsert_recipe(
                recipe_id=params.get("recipe_id"),
                name=str(params.get("name") or "Recipe"),
                description=str(params.get("description", "")),
                index=(
                    self._int(params, "index", minimum=1, maximum=2_147_483_647)
                    if "index" in params else None
                ),
                # 제품 각도는 레시피에서 더 이상 수정하지 않는다.
                # 기존 값은 호환성을 위해 RecipeStore에 그대로 보존된다.
            )
            return {"recipe": recipe.to_dict()}
        if command == "recipe.delete":
            self.recipe_store.delete_recipe(str(params["recipe_id"]))
            return {"accepted": True}
        if command == "point.upsert":
            point = self.recipe_store.upsert_point(
                str(params["recipe_id"]),
                point_id=params.get("point_id"),
                name=str(params.get("name") or "Point"),
                pan=float(params["pan"]),
                tilt=float(params["tilt"]),
                pan_speed=int(params.get("pan_speed", 32)),
                tilt_speed=int(params.get("tilt_speed", 32)),
                dwell_ms=int(params.get("dwell_ms", 0)),
                note=str(params.get("note", "")),
                enabled=bool(params.get("enabled", True)),
            )
            return {"point": point.to_dict()}
        if command == "point.delete":
            self.recipe_store.delete_point(str(params["recipe_id"]), str(params["point_id"]))
            return {"accepted": True}
        if command == "point.reorder":
            recipe = self.recipe_store.reorder_points(
                str(params["recipe_id"]),
                [str(item) for item in params.get("ordered_ids", [])],
            )
            return {"recipe": recipe.to_dict()}
        if command == "lens.motion":
            action = self._enum(params, "action", {"zoom_in", "zoom_out", "focus_near", "focus_far", "iris_open", "iris_close"})
            return {"accepted": True, "action": action, "rx": self.send(lens_motion(self._address, action), wait_ms=80)}
        if command == "aux.set":
            number = self._int(params, "number", self.aux_number, minimum=1, maximum=255)
            enabled = self._bool(params, "enabled")
            rx = self.send(set_auxiliary(self._address, number, enabled), wait_ms=80)
            if number == self.aux_number and self._laser_backend != "gpio":
                self.laser_on = enabled
            return {"accepted": True, "number": number, "enabled": enabled, "rx": rx}
        if command == "preset.set":
            self._require_confirm(params)
            number = self._int(params, "number", minimum=1, maximum=255)
            return {"accepted": True, "number": number, "rx": self.send(set_preset(self._address, number), wait_ms=80)}
        if command in {"preset.call", "preset.goto"}:
            number = self._int(params, "number", minimum=1, maximum=255)
            return {"accepted": True, "number": number, "rx": self.send(call_preset(self._address, number), wait_ms=80)}
        if command in {"preset.clear", "preset.delete"}:
            self._require_confirm(params)
            number = self._int(params, "number", minimum=1, maximum=255)
            return {"accepted": True, "number": number, "rx": self.send(clear_preset(self._address, number), wait_ms=80)}
        if command == "raw.describe":
            raw_command = parse_hex_command(str(params["hex"]), self._address)
            return {"hex": frame_to_hex(raw_command.data), "description": raw_command.description}
        if command in protocol_named_command_names():
            built = build_named_protocol_command(self._address, command, params)
            if built.requires_confirm:
                self._require_confirm(params)
            if command == "protocol.command":
                self._position_speed_frame = None
            rx = []
            for item in built.commands:
                rx.extend(self.send(item, wait_ms=built.wait_ms))
            return {"accepted": True, **built.result, "rx": rx}

        if command == "scan.set_point":
            point = self._enum(params, "point", {"start", "end"})
            return {"accepted": True, "point": point, "rx": self.send(set_line_scan_point(self._address, point == "start"), wait_ms=80)}
        if command == "scan.start":
            mode = self._enum(params, "mode", {"vendor", "zone"}, "vendor")
            api_command = vendor_line_scan(self._address, True) if mode == "vendor" else zone_scan(self._address, True)
            return {"accepted": True, "mode": mode, "rx": self.send(api_command, wait_ms=80)}
        if command == "scan.stop":
            mode = self._enum(params, "mode", {"vendor", "zone"}, "vendor")
            api_command = vendor_line_scan(self._address, False) if mode == "vendor" else zone_scan(self._address, False)
            return {"accepted": True, "mode": mode, "rx": self.send(api_command, wait_ms=80)}
        if command == "scan.speed":
            pan_speed = self._int(params, "pan_speed", minimum=0, maximum=63)
            tilt_speed = self._int(params, "tilt_speed", minimum=0, maximum=63)
            return {"accepted": True, "pan_speed": pan_speed, "tilt_speed": tilt_speed, "rx": self.send(set_scan_speed(self._address, pan_speed, tilt_speed), wait_ms=80)}

        if command == "cruise.start":
            track = self._int(params, "track", minimum=1, maximum=8)
            return {"accepted": True, "track": track, "rx": self.send(start_cruise(self._address, track), wait_ms=80)}
        if command == "cruise.stop":
            return {"accepted": True, "rx": self.send(stop(self._address), wait_ms=80)}
        if command == "cruise.speed":
            pan_speed = self._int(params, "pan_speed", minimum=0, maximum=63)
            tilt_speed = self._int(params, "tilt_speed", minimum=0, maximum=63)
            return {"accepted": True, "pan_speed": pan_speed, "tilt_speed": tilt_speed, "rx": self.send(set_cruise_speed(self._address, pan_speed, tilt_speed), wait_ms=80)}

        if command == "home.auto":
            enabled = self._bool(params, "enabled")
            return {"accepted": True, "enabled": enabled, "rx": self.send(auto_home(self._address, enabled), wait_ms=80)}
        if command == "home.after":
            action = self._enum(params, "action", {"preset1", "cruise1"})
            api_command = home_then_preset1(self._address) if action == "preset1" else home_then_cruise1(self._address)
            return {"accepted": True, "action": action, "rx": self.send(api_command, wait_ms=80)}

        if command == "laser.serial_connect":
            if self._laser_backend == "gpio":
                raise HeadlessApiError("LASER_GPIO_MODE", "Laser uses GPIO BOARD 31; serial connection is disabled")
            port = str(params.get("port", "")).strip()
            if not port:
                raise HeadlessApiError("INVALID_PARAMS", "laser.serial_connect requires port")
            baudrate = self._int(params, "baudrate", LASER_DEFAULT_BAUD, minimum=1200, maximum=115200)
            address = self._int(params, "address", LASER_DEFAULT_ADDRESS, minimum=1, maximum=247)
            return self.connect_laser(port, baudrate, address)
        if command == "laser.serial_disconnect":
            if self._laser_backend == "gpio":
                raise HeadlessApiError("LASER_GPIO_MODE", "Laser uses GPIO BOARD 31; serial disconnection is disabled")
            self.close_laser()
            return {"connected": False, "laser_on": False}
        if command == "laser.on":
            if self._pending_motion is not None:
                raise HeadlessApiError("MOTION_ACTIVE", "Laser remains OFF until movement completes")
            if not self._laser_manual_allowed:
                raise HeadlessApiError("LASER_INTERLOCK", "Core safety interlock blocks manual laser output: "
                                       + self._laser_manual_block_reason)
            if self.laser_bypass:
                raise HeadlessApiError("LASER_BYPASSED", "Laser output is disabled by bypass mode")
            self._laser_manual_deadline = time.monotonic() + 1800.0
            return self._laser_write_coil(True)
        if command == "laser.off":
            return self._laser_write_coil(False)
        if command == "laser.status":
            if self.laser_bypass and not self.laser_connected:
                return {"bypass": True, "connected": False, "laser_on": False}
            return self._laser_read_coil()
        if command == "device.query":
            query = self._enum(params, "type", {"pan", "tilt", "zoom", "focus", "device"})
            api_command = {
                "pan": query_pan,
                "tilt": query_tilt,
                "zoom": query_zoom,
                "focus": query_focus,
                "device": query_device_type,
            }[query](self._address)
            return {"accepted": True, "type": query, "rx": self.send(api_command, wait_ms=120)}

        if command == "maintenance.self_check":
            self._require_confirm(params)
            return {"accepted": True, "rx": self.send(self_check(self._address), wait_ms=80)}
        if command == "maintenance.restart":
            self._require_confirm(params)
            return {
                "accepted": True,
                "restarting": True,
                "rx": self.send(remote_restart(self._address), wait_ms=80),
            }
        if command == "maintenance.factory_default":
            self._require_confirm(params)
            return {"accepted": True, "rx": self.send(factory_default(self._address), wait_ms=80)}
        if command == "maintenance.power_on_self_check":
            self._require_confirm(params)
            enabled = self._bool(params, "enabled")
            return {"accepted": True, "enabled": enabled, "rx": self.send(power_on_self_check(self._address, enabled), wait_ms=80)}
        if command == "raw.send":
            self._require_confirm(params)
            raw_command = parse_hex_command(str(params["hex"]), self._address)
            return {"accepted": True, "hex": frame_to_hex(raw_command.data), "rx": self.send(raw_command, wait_ms=80)}
        raise HeadlessApiError("UNKNOWN_COMMAND", command)


INDEX_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PT503 Web Console</title>
<style>
body{margin:0;background:#111722;color:#e8eef7;font-family:Segoe UI,Arial,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:12px 16px;background:#1d2634;border-bottom:1px solid #334055}
h1{font-size:18px;margin:0}main{display:grid;grid-template-columns:360px 1fr;gap:12px;padding:12px}
section{border:1px solid #334055;background:#171f2b;border-radius:6px;padding:12px}h2{font-size:15px;margin:0 0 10px}
label{display:block;margin:8px 0 4px;color:#aebbd0}input,select,button,textarea{box-sizing:border-box;width:100%;padding:8px;border-radius:4px;border:1px solid #44546b;background:#222c3c;color:#f6f8fb}
button{cursor:pointer;background:#1769aa;border-color:#2b88d8;font-weight:700}.secondary{background:#2c3748;border-color:#4b5a70}
.dpad{position:relative;width:236px;height:236px;margin:10px auto 14px;border-radius:50%;background:radial-gradient(circle at 44% 38%,#f8f9f8 0,#b7bcc0 31%,#676e77 62%,#05070d 78%,#020308 100%);box-shadow:0 18px 34px rgba(0,0,0,.32),inset 0 0 0 5px #070a0f;touch-action:none}
.dpad:before{content:"";position:absolute;inset:13px;border-radius:50%;border:5px solid #090d13;box-shadow:inset 0 0 18px rgba(255,255,255,.2),0 0 0 1px rgba(255,255,255,.1);pointer-events:none}
.dpad button{width:auto;position:absolute;display:flex;align-items:center;justify-content:center;padding:0;border:1px solid rgba(255,255,255,.45);background:linear-gradient(135deg,#fbfbf8,#a5abb0 55%,#626a72);color:#111820;text-shadow:0 1px rgba(255,255,255,.65);font-size:30px;font-weight:900;box-shadow:inset 0 2px 6px rgba(255,255,255,.65),inset 0 -5px 10px rgba(0,0,0,.28)}
.dpad .seg{width:78px;height:78px}.dpad .up{left:79px;top:19px;border-radius:62px 62px 12px 12px}.dpad .down{left:79px;bottom:19px;border-radius:12px 12px 62px 62px}.dpad .left{left:19px;top:79px;border-radius:62px 12px 12px 62px}.dpad .right{right:19px;top:79px;border-radius:12px 62px 62px 12px}
.dpad .center{left:78px;top:78px;width:80px;height:80px;border-radius:50%;font-size:14px;letter-spacing:0;background:radial-gradient(circle at 40% 35%,#ffffff,#b7bdc2 48%,#6f767e 100%)}.dpad button:hover{filter:brightness(1.08)}.dpad button:active{transform:translateY(1px);filter:brightness(.92)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
table{width:100%;border-collapse:collapse}th,td{padding:7px;border-bottom:1px solid #303b4d;text-align:left}th{background:#243044}
pre{white-space:pre-wrap;background:#0c1119;border:1px solid #303b4d;border-radius:4px;padding:10px;max-height:260px;overflow:auto}textarea{min-height:92px;font-family:Consolas,monospace}
</style></head>
<body><header><h1>PT503 Web Console</h1><span id="state">loading...</span></header>
<main><section><h2>Serial</h2><button onclick="scan()">9600 자동탐색</button>
<label>Port</label><select id="port"></select><div class="row"><div><label>Baud</label><input id="baud" value="9600"></div><div><label>Address</label><input id="addr" value="1"></div></div>
<div class="row"><button onclick="connectSerial()">Connect</button><button class="secondary" onclick="cmd('serial.disconnect',{})">Disconnect</button></div>
<h2>Move</h2><div class="dpad" aria-label="manual move dial">
<button class="seg up" title="Tilt Up" onpointerdown="jog(event,'stop','up')" onpointerup="stop()">▲</button>
<button class="seg left" title="Pan Left" onpointerdown="jog(event,'left','stop')" onpointerup="stop()">◀</button>
<button class="seg right" title="Pan Right" onpointerdown="jog(event,'right','stop')" onpointerup="stop()">▶</button>
<button class="seg down" title="Tilt Down" onpointerdown="jog(event,'stop','down')" onpointerup="stop()">▼</button>
<button class="center" title="STOP" onclick="stop()">STOP</button></div>
<div class="row"><div><label>Pan</label><input id="pan" value="0"></div><div><label>Tilt</label><input id="tilt" value="0"></div></div>
<button onclick="gotoAbs()">지령위치 이동</button><button class="secondary" onclick="cmd('position.get',{})">위치 조회</button></section>
<section><h2>Recipe / Points</h2><div class="row"><select id="recipe"></select><button onclick="newRecipe()">레시피 추가</button></div>
<div class="three"><input id="pname" placeholder="point name"><input id="ppan" placeholder="pan"><input id="ptilt" placeholder="tilt"></div><button onclick="savePoint()">포인트 저장</button>
<table><thead><tr><th>#</th><th>Name</th><th>Pan</th><th>Tilt</th><th></th></tr></thead><tbody id="points"></tbody></table>
<h2>Protocol / API</h2><div class="row"><select id="apiCommand"></select><button onclick="loadTemplate()">템플릿</button></div>
<textarea id="apiParams">{}</textarea><button onclick="runApiCommand()">명령 실행</button>
<h2>Log</h2><pre id="log"></pre></section></main>
<script>
let recipes=[];let commands=[];const templates={
'serial.auto_reconnect':{enabled:true,baudrates:[9600],first_address:1,last_address:16,timeout_ms:200,retry_interval_s:3,health_interval_s:2},
'motion.jog':{pan:'right',tilt:'stop',speed_level:5},'motion.absolute':{pan:90,tilt:0},
'lens.motion':{action:'zoom_in'},'aux.set':{number:1,enabled:true},'preset.set':{number:1,confirm:true},'preset.call':{number:1},'preset.clear':{number:1,confirm:true},
'scan.set_point':{point:'start'},'scan.start':{mode:'vendor'},'scan.stop':{mode:'vendor'},'scan.speed':{pan_speed:20,tilt_speed:8},'scan.speed_adjust':{direction:'faster'},
'cruise.start':{track:1},'cruise.speed':{pan_speed:20,tilt_speed:8},'home.auto':{enabled:false},'home.after':{action:'cruise1'},
'laser.serial_connect':{port:'/dev/ttyUSB1',baudrate:9600,address:1},'laser.serial_disconnect':{},'laser.status':{},'laser.pulse':{duration_ms:500},'device.query':{type:'pan'},'maintenance.self_check':{confirm:true},
'maintenance.restart':{confirm:true},'maintenance.factory_default':{confirm:true},'maintenance.power_on_self_check':{enabled:false,confirm:true},
'raw.describe':{hex:'FF 01 00 51 00 00 52'},'motion.relative':{pan_delta:1.0},'zoom.set':{value:1000},'focus.set':{value:1000},'pelco.flip':{},'pelco.zero_pan':{},'pelco.remote_reset':{confirm:true},'zone.set_start':{zone:1},'zone.set_end':{zone:1},'screen.write_text':{column:0,text:'TEST'},'screen.clear':{},'alarm.ack':{alarm:1},'pattern.record_start':{pattern:1},'pattern.record_stop':{},'pattern.run':{pattern:1},'lens.zoom_speed':{speed:2},'lens.focus_speed':{speed:2},'camera.power':{enabled:true},'camera.scan':{mode:'auto'},'camera.auto_focus':{mode:'auto'},'camera.auto_iris':{mode:'auto'},'camera.agc':{mode:'auto'},'camera.backlight':{enabled:false},'camera.auto_white_balance':{enabled:true},'camera.phase_delay':{},'camera.shutter':{value:0},'camera.adjust':{kind:'gain',value:0,delta:false},'preset.scan':{dwell:5},'zero.set':{confirm:true},'magnification.set':{value:1000},'magnification.query':{},'echo.activate':{},'device.remote_baud':{code:2,confirm:true},'device.query_diagnostics':{},'cruise.interval':{value:1},'home.time':{value:1},'protocol.command':{command1:0,command2:15,data1:0,data2:0,confirm:true},'raw.send':{hex:'FF 01 00 51 00 00 52',confirm:true}};
async function api(path,opt={}){const r=await fetch(path,opt);const j=await r.json();if(!r.ok||j.ok===false)throw new Error(j.error?.message||j.message||r.statusText);return j.result??j}
async function cmd(command,params){try{const r=await api('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command,params})});log(command+' -> '+JSON.stringify(r));await refresh(false);return r}catch(e){log('ERR '+command+' -> '+e.message)}}
async function cmdSilent(command,params){return await api('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command,params})})}
function log(t){const el=document.getElementById('log');el.textContent=new Date().toLocaleTimeString()+' '+t+'\\n'+el.textContent}
async function refresh(load=true){const s=await api('/api/status');state.textContent=s.serial.connected?`connected ${s.serial.port} ${s.serial.baudrate}bps addr ${s.serial.address}`:'disconnected';if(s.position.pan!==null)pan.value=s.position.pan;if(s.position.tilt!==null)tilt.value=s.position.tilt;const ports=await api('/api/ports');port.innerHTML=ports.ports.map(p=>`<option>${p.port}</option>`).join('');if(load)await loadRecipes()}
async function loadCommands(){const r=await cmdSilent('system.commands',{});commands=r.commands;apiCommand.innerHTML=commands.map(c=>`<option>${c}</option>`).join('');loadTemplate()}
async function loadRecipes(){const r=await cmdSilent('recipe.list',{});recipes=r.recipes;const cur=recipe.value;recipe.innerHTML=recipes.map(x=>`<option value="${x.id}">${x.name}</option>`).join('');if(cur)recipe.value=cur;renderPoints()}
function selectedRecipe(){return recipes.find(r=>r.id===recipe.value)||recipes[0]}function renderPoints(){const r=selectedRecipe();points.innerHTML=(r?.points||[]).map(p=>`<tr><td>${p.order}</td><td>${p.name}</td><td>${p.pan}</td><td>${p.tilt}</td><td><button onclick="gotoPoint('${p.id}')">이동</button></td></tr>`).join('')}
async function scan(){await cmd('serial.scan',{baudrates:[9600],first_address:1,last_address:1,timeout_ms:200})}async function connectSerial(){await cmd('serial.connect',{port:port.value,baudrate:+baud.value,address:+addr.value})}
async function jog(e,p,t){try{e?.currentTarget?.setPointerCapture?.(e.pointerId)}catch(_e){}await cmd('motion.jog',{pan:p,tilt:t,speed_level:5})}async function stop(){await cmd('motion.stop',{})}async function gotoAbs(){await cmd('motion.absolute',{pan:+pan.value,tilt:+tilt.value})}
async function newRecipe(){const name=prompt('Recipe name','Recipe');if(name)await cmd('recipe.upsert',{name})}async function savePoint(){const r=selectedRecipe();if(!r)return;await cmd('point.upsert',{recipe_id:r.id,name:pname.value||'Point',pan:+ppan.value,tilt:+ptilt.value})}
function loadTemplate(){apiParams.value=JSON.stringify(templates[apiCommand.value]||{},null,2)}
async function runApiCommand(){let params={};try{params=JSON.parse(apiParams.value||'{}')}catch(e){log('ERR JSON params -> '+e.message);return}await cmd(apiCommand.value,params)}
async function gotoPoint(id){const r=selectedRecipe();await cmd('point.goto',{recipe_id:r.id,point_id:id})}recipe.addEventListener('change',renderPoints);apiCommand.addEventListener('change',loadTemplate);refresh();loadCommands();setInterval(()=>refresh(false),3000);
</script></body></html>
"""


class WebHandler(BaseHTTPRequestHandler):
    controller: HeadlessController
    _CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send_text((Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            elif path in {"/app.js", "/style.css"}:
                self._send_text((Path(__file__).parent / "web" / path[1:]).read_text(encoding="utf-8"), "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
            elif path.startswith("/cad/"):
                root = (Path(__file__).parent / "web" / "cad").resolve()
                asset = (root / unquote(path.removeprefix("/cad/"))).resolve()
                try:
                    asset.relative_to(root)
                except ValueError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_types = {
                    ".js": "text/javascript; charset=utf-8",
                }
                content_type = content_types.get(asset.suffix.lower())
                if content_type is None or not asset.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_bytes(asset.read_bytes(), content_type)
            elif path == "/templates.js":
                catalog_commands = tuple(sorted(set(SUPPORTED_COMMANDS) | LASER_API_COMMANDS | DRAWING_API_COMMANDS | PTM_SETTINGS_COMMANDS))
                catalog = self.controller.protocol_catalog()
                payload = (
                    "window.protocolTemplates="
                    + json.dumps(command_templates(catalog_commands), ensure_ascii=False)
                    + ";\nwindow.protocolEnums="
                    + json.dumps(catalog["enums"], ensure_ascii=False)
                    + ";\nwindow.protocolGroupGuide="
                    + json.dumps(catalog.get("group_guide", {}), ensure_ascii=False)
                    + ";\nwindow.protocolCommandGuide="
                    + json.dumps(catalog.get("command_guide", {}), ensure_ascii=False)
                    + ";"
                )
                self._send_text(payload, "text/javascript; charset=utf-8")
            elif path == "/api/status":
                self._send_json(self.controller.status())
            elif path == "/api/ports":
                self._send_json({"ports": self.controller.ports()})
            elif path == "/api/recipes":
                self._send_json(self.controller.command("recipe.list", {}))
            elif path.startswith("/api/drawings/") and path.endswith("/mesh"):
                parts = [unquote(item) for item in path.strip("/").split("/")]
                if len(parts) != 4 or parts[0] != "api" or parts[1] != "drawings" or parts[3] != "mesh":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_json(self.controller.drawing_store.mesh(parts[2]))
            elif path.startswith("/api/drawings/") and path.endswith("/model"):
                parts = [unquote(item) for item in path.strip("/").split("/")]
                if len(parts) != 4 or parts[0] != "api" or parts[1] != "drawings" or parts[3] != "model":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_bytes(
                    self.controller.drawing_store.model_path(parts[2]).read_bytes(),
                    "model/gltf-binary",
                )
            elif path.startswith("/api/drawings/") and path.endswith("/archive"):
                parts = [unquote(item) for item in path.strip("/").split("/")]
                if len(parts) != 4 or parts[0] != "api" or parts[1] != "drawings" or parts[3] != "archive":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                with self.controller._lock:
                    payload = self.controller.drawing_store.archive(parts[2])
                self._send_bytes(payload, "application/zip")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except self._CLIENT_DISCONNECT_ERRORS:
            return
        except Exception as exc:  # noqa: BLE001
            self._send_error("INTERNAL_ERROR", str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/drawings/upload":
            self._handle_drawing_upload()
            return
        if path != "/api/command":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1048576:
                raise HeadlessApiError("INVALID_REQUEST", "Body size must be 1..1048576 bytes")
            request = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(request, dict):
                raise HeadlessApiError("INVALID_REQUEST", "JSON object required")
            command = str(request.get("command", ""))
            params = request.get("params", {})
            if not command or not isinstance(params, dict):
                raise HeadlessApiError("INVALID_REQUEST", "command and params object are required")
            self._send_json({"ok": True, "result": self.controller.command(command, params)})
        except self._CLIENT_DISCONNECT_ERRORS:
            return
        except HeadlessApiError as exc:
            self._send_json({"ok": False, "error": {"code": exc.code, "message": exc.message}}, HTTPStatus.BAD_REQUEST)
        except (KeyError, ValueError, ProtocolError) as exc:
            self._send_json({"ok": False, "error": {"code": "INVALID_PARAMS", "message": str(exc)}}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._send_error("INTERNAL_ERROR", str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_drawing_upload(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_DRAWING_UPLOAD_BYTES:
                raise HeadlessApiError("INVALID_REQUEST", "Drawing upload size must be 1..512MB")
            content_type = self.headers.get("Content-Type", "")
            payload = self.rfile.read(length)
            files = self._multipart_files(payload, content_type)
            with self.controller._lock:
                drawing = self.controller.drawing_store.create_from_uploads(files)
            self._send_json({"ok": True, "result": {"drawing": drawing}})
        except self._CLIENT_DISCONNECT_ERRORS:
            return
        except HeadlessApiError as exc:
            self._send_json({"ok": False, "error": {"code": exc.code, "message": exc.message}}, HTTPStatus.BAD_REQUEST)
        except (KeyError, ValueError, ProtocolError) as exc:
            self._send_json({"ok": False, "error": {"code": "INVALID_PARAMS", "message": str(exc)}}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._send_error("INTERNAL_ERROR", str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    @staticmethod
    def _multipart_file(payload: bytes, content_type: str) -> tuple[str, bytes]:
        files = WebHandler._multipart_files(payload, content_type)
        return files[0]

    @staticmethod
    def _multipart_files(payload: bytes, content_type: str) -> list[tuple[str, bytes]]:
        boundary_token = "boundary="
        if boundary_token not in content_type:
            raise HeadlessApiError("INVALID_REQUEST", "multipart/form-data boundary is required")
        boundary = content_type.split(boundary_token, 1)[1].split(";", 1)[0].strip().strip('"')
        if not boundary:
            raise HeadlessApiError("INVALID_REQUEST", "multipart boundary is empty")
        delimiter = ("--" + boundary).encode("utf-8")
        files: list[tuple[str, bytes]] = []
        for part in payload.split(delimiter):
            if not part or part in {b"--\r\n", b"--"}:
                continue
            if part.startswith(b"\r\n"):
                part = part[2:]
            header_blob, separator, body = part.partition(b"\r\n\r\n")
            if not separator:
                continue
            if body.endswith(b"\r\n"):
                body = body[:-2]
            if body.endswith(b"--"):
                body = body[:-2]
            headers = header_blob.decode("latin-1", errors="ignore").split("\r\n")
            disposition = next((item for item in headers if item.lower().startswith("content-disposition:")), "")
            if 'name="file"' not in disposition:
                continue
            filename = WebHandler._multipart_param(disposition, "filename") or "drawing.stl"
            if not body:
                raise HeadlessApiError("INVALID_REQUEST", "uploaded drawing file is empty")
            files.append((filename, body))
        if not files:
            raise HeadlessApiError("INVALID_REQUEST", "drawing file field named 'file' is required")
        return files

    @staticmethod
    def _multipart_param(header: str, name: str) -> str:
        match = re.search(rf'{re.escape(name)}="([^"]*)"', header)
        if match:
            return match.group(1)
        return ""

    def _send_text(self, text: str, content_type: str) -> None:
        self._send_bytes(text.encode("utf-8"), content_type)

    def _send_bytes(self, payload: bytes, content_type: str) -> None:
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(payload)
        except self._CLIENT_DISCONNECT_ERRORS:
            return

    def _send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(payload)
        except self._CLIENT_DISCONNECT_ERRORS:
            return

    def _send_error(self, code: str, message: str, status: HTTPStatus) -> None:
        self._send_json({"ok": False, "error": {"code": code, "message": message}}, status)


class HeadlessHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class HeadlessTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        controller: HeadlessController,
    ) -> None:
        self.controller = controller
        self._client_lock = threading.Lock()
        self._next_client_number = 1
        self._clients: dict[str, TcpJsonHandler] = {}
        self._closing = False
        super().__init__(server_address, TcpJsonHandler)
        self.controller.set_event_callback(self.emit_event)

    def next_client_id(self) -> str:
        with self._client_lock:
            client_id = f"client-{self._next_client_number}"
            self._next_client_number += 1
            return client_id

    def register(self, handler: "TcpJsonHandler") -> None:
        with self._client_lock:
            closing = self._closing
            if not closing:
                self._clients[handler.client_id] = handler
        if closing:
            handler._close_connection()

    def unregister(self, client_id: str) -> None:
        with self._client_lock:
            self._clients.pop(client_id, None)

    def server_close(self) -> None:
        self.controller.set_event_callback(None)
        with self._client_lock:
            self._closing = True
            handlers = list(self._clients.values())
        for handler in handlers:
            handler._close_connection()
        super().server_close()
        deadline = time.monotonic() + TCP_SEND_TIMEOUT_S + 0.5
        for handler in handlers:
            handler._sender_thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def emit_event(
        self,
        event: str,
        data: dict[str, Any],
        client_id: str | None = None,
    ) -> None:
        with self._client_lock:
            if client_id is None:
                handlers = list(self._clients.values())
            else:
                handler = self._clients.get(client_id)
                handlers = [handler] if handler is not None else []
        for handler in handlers:
            handler.send_event(event, data)


class TcpJsonHandler(socketserver.StreamRequestHandler):
    server: HeadlessTcpServer
    _CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    def setup(self) -> None:
        super().setup()
        self._send_state_lock = threading.Lock()
        self._send_queue: queue.Queue[bytes] = queue.Queue(maxsize=TCP_SEND_QUEUE_SIZE)
        self._queued_bytes = 0
        self._disconnected = False
        self._drain_deadline = 0.0
        self._sender_thread = threading.Thread(target=self._send_loop, name="pt503-client-writer", daemon=True)
        self._sender_thread.start()
        self.client_id = self.server.next_client_id()
        self.server.register(self)
        self._send_json(
            {
                "event": "service.ready",
                "data": {
                    "protocol": PROTOCOL_NAME,
                    "version": PROTOCOL_VERSION,
                    "client_id": self.client_id,
                    "mode": "headless",
                },
                "timestamp": self._timestamp(),
            }
        )
        states = self.server.controller.protocol_states()
        self.send_event("pt503.connection_changed", {"state": states["pt503"]})
        self.send_event("motor.state_changed", {"state": states["motor"]})
        self.send_event("home.state_changed", {"state": states["home"]})

    def handle(self) -> None:
        while not self._disconnected:
            try:
                raw_line = self.rfile.readline(MAX_TCP_LINE_BYTES + 1)
            except self._CLIENT_DISCONNECT_ERRORS:
                self._disconnected = True
                return
            if not raw_line:
                return
            if len(raw_line) > MAX_TCP_LINE_BYTES:
                self._send_error(None, "FRAME_TOO_LARGE", "request line is too large")
                return
            raw_line = raw_line.rstrip(b"\r\n")
            if not raw_line:
                continue
            self._handle_line(raw_line)

    def finish(self) -> None:
        try:
            self._disconnect_safety()
        finally:
            with self._send_state_lock:
                self._drain_deadline = time.monotonic() + TCP_SEND_TIMEOUT_S
            self._sender_thread.join(timeout=TCP_SEND_TIMEOUT_S)
            self._close_connection()
            self._sender_thread.join(timeout=0.5)
            self.server.unregister(self.client_id)
            super().finish()

    def _handle_line(self, raw_line: bytes) -> None:
        request_id: Any = None
        try:
            request = json.loads(raw_line.decode("utf-8"))
            if not isinstance(request, dict):
                raise HeadlessApiError("INVALID_REQUEST", "JSON object required")
            request_id = request.get("id")
            if request_id is None or isinstance(request_id, (dict, list, bool)):
                raise HeadlessApiError("INVALID_ID", "id must be string or number")
            command = request.get("command")
            if not isinstance(command, str) or not command.strip():
                raise HeadlessApiError("INVALID_COMMAND", "command string is required")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise HeadlessApiError("INVALID_PARAMS", "params must be JSON object")
            result = self.server.controller.command(
                command.strip(),
                params,
                client_id=self.client_id,
                request_id=request_id,
            )
            self._send_json({"id": request_id, "ok": True, "result": result})
        except UnicodeDecodeError:
            self._send_error(request_id, "INVALID_ENCODING", "UTF-8 request required")
        except json.JSONDecodeError as exc:
            self._send_error(request_id, "INVALID_JSON", f"JSON parse error: {exc.msg}")
        except HeadlessApiError as exc:
            self._send_error(request_id, exc.code, exc.message)
        except (KeyError, ValueError, ProtocolError) as exc:
            self._send_error(request_id, "INVALID_PARAMS", str(exc))
        except Exception as exc:  # noqa: BLE001 - keep malformed clients isolated.
            self._send_error(request_id, "INTERNAL_ERROR", f"request failed: {exc}")

    def _disconnect_safety(self) -> None:
        controller = self.server.controller
        try:
            # Critical: a status/health TCP client may connect and disconnect
            # without owning manual motion.  Stopping on every disconnect caused
            # unrelated clients to inject a physical STOP during Web/gamepad JOG.
            # Only the TCP client that most recently asserted the active JOG owns
            # disconnect-stop safety.
            if controller.jog_owned_by(self.client_id):
                controller.command(
                    "motion.stop", {}, client_id=self.client_id, request_id=None
                )
            # Laser ownership is not tracked here; do not let an unrelated TCP
            # disconnect turn off a laser controlled by another interface.
        except (HeadlessApiError, OSError, serial.SerialException):
            pass

    def _send_error(self, request_id: Any, code: str, message: str) -> None:
        self._send_json(
            {
                "id": request_id,
                "ok": False,
                "error": {"code": code, "message": message},
            }
        )

    def send_event(self, event: str, data: dict[str, Any]) -> None:
        try:
            self._send_json(
                {"event": event, "data": data, "timestamp": self._timestamp()}
            )
        except (OSError, ValueError):
            pass

    def _send_json(self, message: dict[str, Any]) -> None:
        payload = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        close = False
        with self._send_state_lock:
            if self._disconnected or self._drain_deadline:
                return
            if self._queued_bytes + len(payload) > TCP_SEND_QUEUE_BYTES:
                close = True
            else:
                try:
                    self._send_queue.put_nowait(payload)
                    self._queued_bytes += len(payload)
                except queue.Full:
                    close = True
        if close:
            self._close_connection()

    def _close_connection(self) -> None:
        with self._send_state_lock:
            self._disconnected = True
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def _send_loop(self) -> None:
        try:
            while not self._disconnected:
                if self._drain_deadline and self._send_queue.empty():
                    break
                try:
                    payload = self._send_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                with self._send_state_lock:
                    self._queued_bytes -= len(payload)
                self._write_payload(payload)
        except (OSError, ValueError):
            pass
        finally:
            self._close_connection()

    def _write_payload(self, payload: bytes) -> None:
        deadline = time.monotonic() + TCP_SEND_TIMEOUT_S
        if self._drain_deadline:
            deadline = min(deadline, self._drain_deadline)
        remaining_payload = memoryview(payload)
        while remaining_payload:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("TCP client did not receive the response in time")
            # SO_SNDTIMEO limits writes only; an idle client's blocking reader is unchanged.
            timeout = (struct.pack("I", max(1, math.ceil(remaining * 1000))) if os.name == "nt"
                       else struct.pack("ll", int(remaining), int(remaining % 1 * 1_000_000)))
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, timeout)
            if not select.select([], [self.connection], [], remaining)[1]:
                raise TimeoutError("TCP client did not receive the response in time")
            sent = self.connection.send(remaining_payload[:65536])
            if not sent:
                raise ConnectionError("TCP client disconnected during a response")
            remaining_payload = remaining_payload[sent:]

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")


def run_server(
    host: str = DEFAULT_HTTP_HOST,
    port: int = DEFAULT_HTTP_PORT,
    *,
    tcp_host: str | None = DEFAULT_TCP_HOST,
    tcp_port: int = DEFAULT_TCP_PORT,
) -> int:
    host, port, tcp_host, tcp_port = managed_server_endpoints(host, port, tcp_host, tcp_port)
    host = resolve_bind_host(host)
    tcp_host = resolve_bind_host(tcp_host) if tcp_host else None
    controller = HeadlessController()
    WebHandler.controller = controller
    try:
        server = HeadlessHttpServer((host, int(port)), WebHandler)
    except OSError as exc:
        controller.shutdown()
        if exc.errno == errno.EADDRINUSE:
            print(
                f"[ERROR] Web/API port is already in use: {host}:{port}\n"
                f"        Existing server may already be running. Open http://{host}:{port}\n"
                f"        Find owner: ss -ltnp 'sport = :{port}'\n"
                f"        Use another port: ./run_linux.sh --port 9000 --tcp-port 9001",
                file=sys.stderr,
            )
            return 98
        raise
    tcp_server: HeadlessTcpServer | None = None
    tcp_thread: threading.Thread | None = None
    if tcp_host:
        try:
            tcp_server = HeadlessTcpServer((tcp_host, int(tcp_port)), controller)
        except OSError as exc:
            controller.shutdown()
            server.server_close()
            if exc.errno == errno.EADDRINUSE:
                print(
                    f"[ERROR] TCP API port is already in use: {tcp_host}:{tcp_port}\n"
                    f"        Existing server may already be running.\n"
                    f"        Find owner: ss -ltnp 'sport = :{tcp_port}'\n"
                    f"        Use another port: ./run_linux.sh --port 9000 --tcp-port 9001",
                    file=sys.stderr,
                )
                return 98
            raise
        tcp_thread = threading.Thread(
            target=tcp_server.serve_forever,
            name="pt503-headless-tcp",
            daemon=True,
        )
        tcp_thread.start()
        print(f"PT503 headless TCP/JSON server listening on {tcp_host}:{tcp_port}")
    print(f"PT503 headless web/API server listening on http://{host}:{port}")
    stopping = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stopping
        if not stopping:
            stopping = True
            raise KeyboardInterrupt

    previous_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopping = True
        try:
            try:
                controller.shutdown()
            finally:
                if tcp_server is not None:
                    tcp_server.shutdown()
                    tcp_server.server_close()
                if tcp_thread is not None:
                    tcp_thread.join(timeout=2)
                server.server_close()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PT503 headless HTTP/TCP server")
    parser.add_argument("--host", default=DEFAULT_HTTP_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--tcp-host", default=DEFAULT_TCP_HOST)
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT)
    parser.add_argument("--no-tcp", action="store_true")
    args = parser.parse_args(argv)
    return run_server(
        args.host,
        args.port,
        tcp_host=None if args.no_tcp else args.tcp_host,
        tcp_port=args.tcp_port,
    )


if __name__ == "__main__":
    raise SystemExit(main())
