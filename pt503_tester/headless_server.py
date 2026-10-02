"""Headless HTTP/TCP API and web console.

This module avoids PySide/Qt so it can run on Windows and Ubuntu/RDK targets.
By default it binds to the machine's primary LAN IPv4 address for field use.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import errno
import json
import re
import socketserver
import sys
import threading
import time
import math
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import serial
from serial.tools import list_ports

from .protocol import (
    AUTO_PAN_SPEED,
    AUTO_TILT_SPEED,
    manual_speed_value,
    OutgoingCommand,
    PanDirection,
    ProtocolError,
    TiltDirection,
    adjust_preset_speed,
    adjust_scan_speed,
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
    split_response_blob,
    start_cruise,
    stop,
    vendor_line_scan,
    verify_frame_checksum,
    zone_scan,
)
from .recipe_store import RecipeStore, app_state_dir
from .drawing_store import DrawingStore
from .motion_tracker import MotionTracker
from .network_utils import resolve_bind_host
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
PROTOCOL_NAME = 'PT503-Control'
PROTOCOL_VERSION = '1.0'
SUPPORTED_COMMANDS = supported_command_names()

# Arduino UNO laser controller (Modbus RTU)
LASER_DEFAULT_BAUD = 9600
LASER_DEFAULT_ADDRESS = 0x01
LASER_COIL_ADDRESS = 0x0000
LASER_RESPONSE_TIMEOUT_S = 0.35
LASER_API_COMMANDS = {
    'laser.serial_connect',
    'laser.serial_disconnect',
    'laser.status',
}
DRAWING_API_COMMANDS = {
    'drawing.list',
    'drawing.get',
    'drawing.delete',
    'drawing.select_point_set',
    'drawing.point_upsert',
    'drawing.point_delete',
    'drawing.point_reorder',
    'drawing.calibrate',
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
        self._settings_path = (
            Path(settings_path)
            if settings_path is not None
            else app_state_dir() / "server_settings.json"
        )
        self._lock = threading.RLock()
        self._laser_lock = threading.RLock()
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
        self.last_tx: dict[str, Any] | None = None
        self.completion_config = {"tolerance_deg": 0.2, "stable_samples": 3, "timeout_s": 30}
        self.monitor_config = {"enabled": False, "interval_ms": 500}
        self.laser_armed = False
        self.laser_on = False
        self.aux_number = 1
        self._jog_deadline = 0.0
        # Active continuous JOG key: (pan, tilt, pan_level, tilt_level).
        # Identical client keepalives extend the watchdog without re-sending the
        # same Pelco-D motion frame to the PT unit.
        self._active_jog: tuple[Any, Any, int, int] | None = None
        self._laser_deadline = 0.0
        self._health_misses = 0
        self._tracker = None
        self.motion_state = "idle"
        self._event_callback: Callable[[str, dict[str, Any], str | None], None] | None = None
        self._pending_motion: dict[str, Any] | None = None
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
        self._auto_scan_active = False
        self._last_auto_scan = 0.0
        self._service = threading.Thread(target=self._service_loop, daemon=True)
        self._service.start()

    def set_event_callback(
        self,
        callback: Callable[[str, dict[str, Any], str | None], None] | None,
    ) -> None:
        self._event_callback = callback

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
            "request_id": pending.get("request_id"),
            "command": pending.get("command"),
            "recipe_id": pending.get("recipe_id"),
            "point_id": pending.get("point_id"),
        }
        if error is None:
            if pending.get("home"):
                self._home_valid = True
            self._emit_event("motion.completed", data, pending.get("client_id"))
        else:
            code, message = error
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

    def shutdown(self) -> None:
        self._shutdown.set()
        self._scan_cancel.set()
        self._service.join(timeout=2)
        self.close_laser()
        self.close()

    def _service_loop(self) -> None:
        next_poll = 0.0
        next_health = 0.0
        while not self._shutdown.wait(0.04):
            self._publish_state_changes()
            # Laser pulse watchdog must work even when PT-503 is disconnected.
            with self._lock:
                now = time.monotonic()
                if self._laser_deadline and now >= self._laser_deadline:
                    self._laser_deadline = 0.0
                    try:
                        self._laser_write_coil(False)
                    except HeadlessApiError:
                        # State is forced OFF locally; communication error is visible
                        # in laser_last_rx / the next explicit API command.
                        self.laser_on = False

            if self._should_auto_reconnect():
                self._run_auto_reconnect()
                continue

            with self._lock:
                if not self.connected:
                    continue
                try:
                    now = time.monotonic()
                    if self._jog_deadline and now >= self._jog_deadline:
                        self._jog_deadline = 0.0
                        self._active_jog = None
                        self.send(stop(self._address), wait_ms=0)
                        self.motion_state = "jog_timeout"
                    if self._tracker and self._tracker.check_timeout().status == "timeout":
                        self._tracker = None
                        self.motion_state = "timeout"
                        self.send(stop(self._address), wait_ms=0)
                        self._finish_pending_motion(
                            error=("MOTION_TIMEOUT", "target position was not reached in time")
                        )
                    # During continuous manual JOG, keep the RS-485 bus quiet.
                    # Some PT units visibly hesitate when position/health queries are
                    # interleaved with a continuous motion command. The client JOG
                    # keepalive still protects runaway motion via _jog_deadline.
                    jogging = self._active_jog is not None and self._jog_deadline > now
                    if not jogging:
                        if now >= next_poll and (self.monitor_config["enabled"] or self._tracker):
                            self.send(query_pan(self._address), wait_ms=80)
                            self.send(query_tilt(self._address), wait_ms=80)
                            next_poll = now + self.monitor_config["interval_ms"] / 1000
                        if now >= next_health:
                            health_rx = self.send(query_pan(self._address), wait_ms=80)
                            if health_rx:
                                self._health_misses = 0
                            else:
                                self._health_misses += 1
                                if self._health_misses >= 3:
                                    self.motion_state = "communication_timeout"
                                    self.close()
                                    continue
                            next_health = now + float(
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
            self.laser_armed = False
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
            self._laser_deadline = 0.0
            if self.laser_connected:
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
            self.laser_armed = False

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
            except serial.SerialException as exc:
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

    def _laser_write_coil(self, enabled: bool) -> dict[str, Any]:
        """Arduino Modbus Function 0x05, Coil 0 = relay/laser ON/OFF."""
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
            self._jog_deadline = 0.0
            self._active_jog = None
            self._tracker = None
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
            self.current_pan = self.current_tilt = None

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
                "motion": {"completion": dict(self.completion_config), "state": self.motion_state},
                "monitor": dict(self.monitor_config),
                "auto_reconnect": {
                    **dict(self.auto_reconnect_config),
                    "scanning": self._auto_scan_active,
                    "health_misses": self._health_misses,
                },
                "laser": {
                    "armed": self.laser_armed,
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
            if enable_auto_reconnect:
                self.auto_reconnect_config["enabled"] = True
                self.auto_reconnect_config["baudrates"] = [int(baudrate)]
                self.auto_reconnect_config["first_address"] = int(address)
                self.auto_reconnect_config["last_address"] = int(address)
            self._save_settings()
            return {"connected": True, "port": port, "baudrate": baudrate, "address": address}

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
        return api_protocol_catalog(tuple(sorted(set(SUPPORTED_COMMANDS) | LASER_API_COMMANDS | DRAWING_API_COMMANDS)))

    def send(self, command: OutgoingCommand, *, wait_ms: int = 80) -> list[dict[str, Any]]:
        with self._lock:
            if not self.connected or self._serial is None:
                raise HeadlessApiError("SERIAL_NOT_CONNECTED", "serial port is not connected")
            try:
                self._serial.write(command.data)
                self._serial.flush()
                self.last_tx = {
                    "hex": frame_to_hex(command.data),
                    "description": command.description,
                    "category": command.category,
                    "time": time.time(),
                }
                deadline = time.perf_counter() + wait_ms / 1000.0
                received = bytearray()
                while time.perf_counter() < deadline:
                    count = self._serial.in_waiting
                    if count:
                        received.extend(self._serial.read(count))
                    time.sleep(0.005)
            except serial.SerialException as exc:
                self.close()
                raise HeadlessApiError("SERIAL_IO_FAILED", str(exc)) from exc
            decoded = self._decode_blob(bytes(received), command.data[6])
            self.last_rx.extend(decoded)
            self.last_rx = self.last_rx[-50:]
            return decoded

    def _decode_blob(self, blob: bytes, last_tx_checksum: int | None = None) -> list[dict[str, Any]]:
        frames = []
        for frame in split_response_blob(blob):
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
            if self.current_pan is not None and self.current_tilt is not None:
                pan_error = abs(((self.current_pan + 180.0) % 360.0) - 180.0)
                tolerance = float(self.completion_config["tolerance_deg"])
                self._home_valid = pan_error <= tolerance and abs(self.current_tilt) <= tolerance
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

    def command(
        self,
        command: str,
        params: dict[str, Any],
        *,
        client_id: str | None = None,
        request_id: Any = None,
    ) -> dict[str, Any]:
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
        # Scan cancellation and status must stay available during discovery.
        if command == "serial.scan_cancel":
            self._scan_cancel.set()
            return {"accepted": True}
        if command == "serial.scan":
            with self._lock:
                if self.scanning or self.connected:
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
                self.scanning = False
        if command == "serial.auto_reconnect":
            with self._lock:
                if "enabled" in params:
                    self.auto_reconnect_config["enabled"] = self._bool(params, "enabled")
                if "baudrates" in params:
                    baudrates = params["baudrates"]
                    if not isinstance(baudrates, list) or not baudrates:
                        raise HeadlessApiError("INVALID_PARAMS", "baudrates must be non-empty array")
                    self.auto_reconnect_config["baudrates"] = [
                        self._int({"baud": item}, "baud", minimum=1200, maximum=115200)
                        for item in baudrates
                    ]
                if "first_address" in params:
                    self.auto_reconnect_config["first_address"] = self._int(
                        params, "first_address", minimum=1, maximum=255
                    )
                if "last_address" in params:
                    self.auto_reconnect_config["last_address"] = self._int(
                        params, "last_address", minimum=1, maximum=255
                    )
                if "timeout_ms" in params:
                    self.auto_reconnect_config["timeout_ms"] = self._int(
                        params, "timeout_ms", minimum=50, maximum=2000
                    )
                if "retry_interval_s" in params:
                    self.auto_reconnect_config["retry_interval_s"] = self._float(
                        params,
                        "retry_interval_s",
                        minimum=0.5,
                        maximum=3600.0,
                    )
                if "health_interval_s" in params:
                    self.auto_reconnect_config["health_interval_s"] = self._float(
                        params,
                        "health_interval_s",
                        minimum=0.5,
                        maximum=3600.0,
                    )
                if (
                    int(self.auto_reconnect_config["first_address"])
                    > int(self.auto_reconnect_config["last_address"])
                ):
                    raise HeadlessApiError(
                        "INVALID_PARAMS", "first_address must be <= last_address"
                    )
                self._save_settings()
                return dict(self.auto_reconnect_config)
        with self._lock:
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
                point = self.recipe_store.get_point(recipe_id, point_id)
                if not point.enabled:
                    raise HeadlessApiError("DISABLED", "point is disabled")
                self._selected_recipe_id = recipe_id
                self._selected_point_id = point_id
                result = self.command(
                    "motion.absolute",
                    {"pan": point.pan, "tilt": point.tilt},
                    client_id=client_id,
                    request_id=request_id,
                )
                if self._pending_motion is not None:
                    self._pending_motion.update(
                        command=command, recipe_id=recipe_id, point_id=point_id
                    )
                return {**result, "recipe_id": recipe_id, "point_id": point_id}
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
                    raise HeadlessApiError("INVALID_PARAMS", "Use pan_level/tilt_level (1..8)")
                pan = {"left": PanDirection.LEFT, "right": PanDirection.RIGHT, "stop": PanDirection.STOP}[self._enum(params, "pan", {"left", "right", "stop"}, "stop")]
                tilt = {"up": TiltDirection.UP, "down": TiltDirection.DOWN, "stop": TiltDirection.STOP}[self._enum(params, "tilt", {"up", "down", "stop"}, "stop")]
                pl = self._int(params, "pan_level", 5, minimum=1, maximum=8)
                tl = self._int(params, "tilt_level", 5, minimum=1, maximum=8)
                duration = self._int(params, "duration_ms", 1200, minimum=100, maximum=5000)

                now = time.monotonic()
                jog_key = (pan, tilt, pl, tl)
                refreshed = self._active_jog == jog_key and self._jog_deadline > now

                if refreshed:
                    # Same direction/speed: this is only a client watchdog keepalive.
                    # Do not send the same continuous-motion frame to the motor again.
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
                        wait_ms=0,
                    )
                    self._active_jog = jog_key

                self._tracker = None
                self._jog_deadline = now + duration / 1000
                self.motion_state = "jog"
                return {
                    "accepted": True,
                    "refreshed": refreshed,
                    "rx": rx,
                    "pan_level": pl,
                    "tilt_level": tl,
                    "watchdog_ms": duration,
                }
            if command in {"motion.stop", "cruise.stop", "scan.stop"}:
                self._jog_deadline = 0.0
                self._active_jog = None
                self._tracker = None
                self.motion_state = "stopped"
                self._finish_pending_motion(
                    error=("MOTION_STOPPED", "movement was stopped by request")
                )
            if command == "motion.absolute":
                # Absolute motion supersedes any continuous manual JOG watchdog.
                self._jog_deadline = 0.0
                self._active_jog = None
                # Legacy speed fields are ignored; every target move uses maximum.
                params = {**params, "pan_speed": AUTO_PAN_SPEED, "tilt_speed": AUTO_TILT_SPEED, "axis_delay_ms": 0}
            if command in {"scan.speed", "cruise.speed"}:
                params = {**params, "pan_speed": AUTO_PAN_SPEED, "tilt_speed": AUTO_TILT_SPEED}
            if command in {"preset.speed_adjust", "scan.speed_adjust"}:
                setter = set_position_speed if command.startswith("preset") else set_scan_speed
                return {"accepted": True, "fixed_maximum": True, "rx": self.send(setter(self._address, AUTO_PAN_SPEED, AUTO_TILT_SPEED))}
            if command in {"motion.relative", "preset.call", "preset.goto", "pelco.flip", "pelco.zero_pan", "pattern.run", "preset.scan", "scan.start", "cruise.start", "camera.scan", "home.auto", "home.after"}:
                self._jog_deadline = 0.0
                self._active_jog = None
                self._tracker = None
                for setter in (set_position_speed, set_scan_speed, set_cruise_speed):
                    self.send(setter(self._address, AUTO_PAN_SPEED, AUTO_TILT_SPEED))
            if command == "laser.arm" and not self._bool(params, "enabled"):
                if self.laser_connected:
                    self._laser_write_coil(False)
                self.laser_on = False
                self._laser_deadline = 0.0
            if command == "laser.off":
                self._laser_deadline = 0.0
            if command == "laser.on":
                self._laser_deadline = 0.0
            if command == "laser.pulse":
                if not self.laser_armed:
                    raise HeadlessApiError("LASER_NOT_ARMED", "Enable ARM first")
                duration = self._int(params, "duration_ms", 500, minimum=50, maximum=60000)
                result = self._laser_write_coil(True)
                self._laser_deadline = time.monotonic() + duration / 1000
                return {**result, "duration_ms": duration}
            if command == "motion.completion_config" and "tolerance_deg" in params:
                self._float(params, "tolerance_deg", minimum=0.01, maximum=10)
            if command == "point.upsert":
                self._float(params, "pan", minimum=0, maximum=359.99)
                self._float(params, "tilt", minimum=-60, maximum=60)
                self._int(params, "dwell_ms", 0, minimum=0, maximum=3600000)
                params = {**params, "pan_speed": AUTO_PAN_SPEED, "tilt_speed": AUTO_TILT_SPEED, "enabled": self._bool(params, "enabled", True)}
            result = self._command_impl(command, params)
            if command == "motion.absolute":
                self._jog_deadline = 0.0
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
                    "client_id": client_id,
                    "request_id": request_id,
                    "command": "motion.absolute",
                    "recipe_id": None,
                    "point_id": None,
                    "home": False,
                }
            if force_stop:
                result = {**result, "completed": True, "stopped": True}
            return result

    def _command_impl(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if command == "system.ping":
            return {"protocol": PROTOCOL_NAME, "version": PROTOCOL_VERSION, "time": time.time()}
        if command == "system.status":
            return self.status()
        if command == "system.commands":
            return {"commands": sorted(set(SUPPORTED_COMMANDS) | LASER_API_COMMANDS | DRAWING_API_COMMANDS)}
        if command == "protocol.catalog":
            return self.protocol_catalog()
        if command == "serial.ports":
            return {"ports": self.ports()}
        if command == "serial.settings":
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

        if command == "drawing.list":
            return {"drawings": self.drawing_store.list_drawings()}
        if command == "drawing.get":
            return {"drawing": self.drawing_store.get_drawing(str(params["drawing_id"]))}
        if command == "drawing.delete":
            drawing_id = str(params["drawing_id"])
            self.drawing_store.delete_drawing(drawing_id)
            return {"accepted": True, "drawing_id": drawing_id}
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
            return {"drawing": self.drawing_store.calibrate(str(params["drawing_id"]))}
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
            return {"rx": self.send(stop(self._address), wait_ms=80)}
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
            pan = self._float(params, "pan", required=False, minimum=0.0, maximum=359.99)
            tilt = self._float(params, "tilt", required=False, minimum=-60.0, maximum=60.0)
            if pan is None and tilt is None:
                raise HeadlessApiError("INVALID_PARAMS", "pan or tilt is required")
            rx = []
            if "pan_speed" in params or "tilt_speed" in params:
                rx.extend(
                    self.send(
                        set_position_speed(
                            self._address,
                            self._int(params, "pan_speed", 32, minimum=0, maximum=63),
                            self._int(params, "tilt_speed", 32, minimum=0, maximum=63),
                        ),
                        wait_ms=80,
                    )
                )
            if pan is not None:
                rx.extend(
                    self.send(
                        set_pan_position(self._address, pan),
                        wait_ms=0 if tilt is not None else 80,
                    )
                )
            if pan is not None and tilt is not None and params.get("axis_delay_ms", 0):
                time.sleep(float(params.get("axis_delay_ms", 350)) / 1000.0)
            if tilt is not None:
                rx.extend(self.send(set_tilt_position(self._address, tilt), wait_ms=80))
            return {"accepted": True, "target": {"pan": pan, "tilt": tilt}, "rx": rx}
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
            if refresh:
                rx.extend(self.send(query_pan(self._address), wait_ms=120))
                rx.extend(self.send(query_tilt(self._address), wait_ms=120))
            return {"pan": self.current_pan, "tilt": self.current_tilt, "fresh_query_requested": refresh, "rx": rx}
        if command == "monitor.set":
            self.monitor_config["enabled"] = self._bool(params, "enabled")
            if "interval_ms" in params:
                self.monitor_config["interval_ms"] = self._int(params, "interval_ms", minimum=350, maximum=5000)
            return dict(self.monitor_config)

        if command == "recipe.list":
            return {"recipes": self.recipe_store.list_recipes()}
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
        if command == "point.goto":
            point = self.recipe_store.get_point(str(params["recipe_id"]), str(params["point_id"]))
            if not point.enabled:
                raise HeadlessApiError("DISABLED", "point is disabled")
            return self.command(
                "motion.absolute",
                {
                    "pan": point.pan,
                    "tilt": point.tilt,
                    "pan_speed": point.pan_speed,
                    "tilt_speed": point.tilt_speed,
                },
            ) 

        if command == "lens.motion":
            action = self._enum(params, "action", {"zoom_in", "zoom_out", "focus_near", "focus_far", "iris_open", "iris_close"})
            return {"accepted": True, "action": action, "rx": self.send(lens_motion(self._address, action), wait_ms=80)}
        if command == "aux.set":
            number = self._int(params, "number", self.aux_number, minimum=1, maximum=255)
            enabled = self._bool(params, "enabled")
            rx = self.send(set_auxiliary(self._address, number, enabled), wait_ms=80)
            if number == self.aux_number:
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
        if command == "preset.speed_adjust":
            direction = self._enum(params, "direction", {"faster", "slower"})
            return {"accepted": True, "direction": direction, "rx": self.send(adjust_preset_speed(self._address, direction == "faster"), wait_ms=80)}
        if command == "raw.describe":
            raw_command = parse_hex_command(str(params["hex"]), self._address)
            return {"hex": frame_to_hex(raw_command.data), "description": raw_command.description}
        if command in protocol_named_command_names():
            built = build_named_protocol_command(self._address, command, params)
            if built.requires_confirm:
                self._require_confirm(params)
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
        if command == "scan.speed_adjust":
            direction = self._enum(params, "direction", {"faster", "slower"})
            return {"accepted": True, "direction": direction, "rx": self.send(adjust_scan_speed(self._address, direction == "faster"), wait_ms=80)}

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
            port = str(params.get("port", "")).strip()
            if not port:
                raise HeadlessApiError("INVALID_PARAMS", "laser.serial_connect requires port")
            baudrate = self._int(params, "baudrate", LASER_DEFAULT_BAUD, minimum=1200, maximum=115200)
            address = self._int(params, "address", LASER_DEFAULT_ADDRESS, minimum=1, maximum=247)
            return self.connect_laser(port, baudrate, address)
        if command == "laser.serial_disconnect":
            self.close_laser()
            return {"connected": False, "laser_on": False}
        if command == "laser.arm":
            self.laser_armed = self._bool(params, "enabled")
            return {
                "armed": self.laser_armed,
                "laser_on": self.laser_on,
                "connected": self.laser_connected,
            }
        if command == "laser.on":
            if not self.laser_armed:
                raise HeadlessApiError("LASER_NOT_ARMED", "laser.arm must be enabled first")
            return self._laser_write_coil(True)
        if command == "laser.off":
            return self._laser_write_coil(False)
        if command == "laser.status":
            return self._laser_read_coil()
        if command == "laser.pulse":
            # Normally handled in command() so OFF can be performed by the watchdog.
            if not self.laser_armed:
                raise HeadlessApiError("LASER_NOT_ARMED", "laser.arm must be enabled first")
            duration = self._int(params, "duration_ms", 500, minimum=50, maximum=60_000)
            result = self._laser_write_coil(True)
            self._laser_deadline = time.monotonic() + duration / 1000.0
            return {**result, "duration_ms": duration}

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
<button class="seg up" title="Tilt Up" onpointerdown="jog('stop','up')" onpointerup="stop()" onpointercancel="stop()" onpointerleave="stop()">▲</button>
<button class="seg left" title="Pan Left" onpointerdown="jog('left','stop')" onpointerup="stop()" onpointercancel="stop()" onpointerleave="stop()">◀</button>
<button class="seg right" title="Pan Right" onpointerdown="jog('right','stop')" onpointerup="stop()" onpointercancel="stop()" onpointerleave="stop()">▶</button>
<button class="seg down" title="Tilt Down" onpointerdown="jog('stop','down')" onpointerup="stop()" onpointercancel="stop()" onpointerleave="stop()">▼</button>
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
'motion.jog':{pan:'right',tilt:'stop',pan_level:5,tilt_level:5,duration_ms:500},'motion.absolute':{pan:90,tilt:0},
'lens.motion':{action:'zoom_in'},'aux.set':{number:1,enabled:true},'preset.set':{number:1,confirm:true},'preset.call':{number:1},'preset.clear':{number:1,confirm:true},
'scan.set_point':{point:'start'},'scan.start':{mode:'vendor'},'scan.stop':{mode:'vendor'},'scan.speed':{pan_speed:20,tilt_speed:8},'scan.speed_adjust':{direction:'faster'},
'cruise.start':{track:1},'cruise.speed':{pan_speed:20,tilt_speed:8},'home.auto':{enabled:false},'home.after':{action:'cruise1'},
'laser.serial_connect':{port:'/dev/ttyUSB1',baudrate:9600,address:1},'laser.serial_disconnect':{},'laser.status':{},'laser.arm':{enabled:true},'laser.pulse':{duration_ms:500},'device.query':{type:'pan'},'maintenance.self_check':{confirm:true},
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
async function jog(p,t){await cmd('motion.jog',{pan:p,tilt:t,pan_level:5,tilt_level:5,duration_ms:500})}async function stop(){await cmd('motion.stop',{})}async function gotoAbs(){await cmd('motion.absolute',{pan:+pan.value,tilt:+tilt.value})}
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
                catalog_commands = tuple(sorted(set(SUPPORTED_COMMANDS) | LASER_API_COMMANDS | DRAWING_API_COMMANDS))
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
        super().__init__(server_address, TcpJsonHandler)
        self.controller = controller
        self._client_lock = threading.Lock()
        self._next_client_number = 1
        self._clients: dict[str, TcpJsonHandler] = {}
        self.controller.set_event_callback(self.emit_event)

    def next_client_id(self) -> str:
        with self._client_lock:
            client_id = f"client-{self._next_client_number}"
            self._next_client_number += 1
            return client_id

    def register(self, handler: "TcpJsonHandler") -> None:
        with self._client_lock:
            self._clients[handler.client_id] = handler

    def unregister(self, client_id: str) -> None:
        with self._client_lock:
            self._clients.pop(client_id, None)

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

    def setup(self) -> None:
        super().setup()
        self._write_lock = threading.Lock()
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
        while True:
            raw_line = self.rfile.readline(MAX_TCP_LINE_BYTES + 1)
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
        self._disconnect_safety()
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
            if controller.motion_state == "jog":
                controller.command("motion.stop", {})
            if controller.laser_on:
                controller.command("laser.off", {})
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
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            self.wfile.write((text + "\n").encode("utf-8"))
            self.wfile.flush()

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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tcp_server is not None:
            tcp_server.shutdown()
            tcp_server.server_close()
        if tcp_thread is not None:
            tcp_thread.join(timeout=2)
        controller.shutdown()
        server.server_close()
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

