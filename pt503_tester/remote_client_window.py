"""PySide6 TCP client for a headless PT503 API server."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import gamepad_mapping as gamepad_map
from .api_metadata import command_templates, supported_command_names
from .gamepad_control import axes_to_motion
from .gamepad_manager import GamepadManager, GamepadState
from .protocol import PanDirection, TiltDirection


ResponseCallback = Callable[[dict[str, Any]], None]


class RemoteClientWindow(QMainWindow):
    """Small operator client that talks only to the TCP API server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        super().__init__()
        self.setWindowTitle("PT503 Remote Client")
        self.resize(920, 720)
        self.socket = QTcpSocket(self)
        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.readyRead.connect(self._on_ready_read)
        self.socket.errorOccurred.connect(self._on_socket_error)
        self._buffer = bytearray()
        self._pending: dict[str, ResponseCallback | None] = {}
        self._current_jog: tuple[str, str] | None = None
        self._gamepad_motion: tuple[str, str] | None = None
        self._previous_buttons: tuple[bool, ...] = ()
        self._previous_hat = (0, 0)
        self._laser_on = False
        self._api_commands = supported_command_names()
        self._api_templates = command_templates(self._api_commands)

        self.status_timer = QTimer(self)
        self.status_timer.setInterval(1000)
        self.status_timer.timeout.connect(lambda: self._request_status(quiet=True))
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setInterval(2000)
        self.reconnect_timer.timeout.connect(self._retry_server_connection)
        self.jog_timer = QTimer(self)
        # JOG lease refresh only. The server suppresses an identical JOG frame and
        # extends its watchdog, so the physical motor command is not restarted.
        # A 1 s refresh / 5 s lease also tolerates short Qt/network stalls.
        self.jog_timer.setInterval(1000)
        self.jog_timer.timeout.connect(self._send_current_jog)

        self.gamepad_manager = GamepadManager(self)
        self.gamepad_manager.devices_changed.connect(self._on_gamepad_devices)
        self.gamepad_manager.connected.connect(self._on_gamepad_connected)
        self.gamepad_manager.disconnected.connect(self._on_gamepad_disconnected)
        self.gamepad_manager.state_changed.connect(self._on_gamepad_state)
        self.gamepad_manager.error.connect(lambda text: self._log(f"GAMEPAD ERR {text}"))

        self._build_ui(host, port)
        self._refresh_gamepads()

    def _build_ui(self, host: str, port: int) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        server_group = QGroupBox("API 서버 연결")
        server_layout = QGridLayout(server_group)
        self.host_edit = QLineEdit(host)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(port)
        self.auto_server_reconnect = QCheckBox("서버 자동 재연결")
        self.auto_server_reconnect.setChecked(True)
        self.connect_button = QPushButton("TCP 연결")
        self.connect_button.clicked.connect(self._toggle_connection)
        self.connection_label = QLabel("연결 안 됨")
        server_layout.addWidget(QLabel("Host"), 0, 0)
        server_layout.addWidget(self.host_edit, 0, 1)
        server_layout.addWidget(QLabel("TCP Port"), 0, 2)
        server_layout.addWidget(self.port_spin, 0, 3)
        server_layout.addWidget(self.connect_button, 0, 4)
        server_layout.addWidget(self.auto_server_reconnect, 1, 0, 1, 2)
        server_layout.addWidget(self.connection_label, 1, 2, 1, 3)
        layout.addWidget(server_group)

        serial_group = QGroupBox("서버 측 USB-RS485")
        serial_layout = QGridLayout(serial_group)
        self.port_combo = QComboBox()
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(1200, 115200)
        self.baud_spin.setValue(9600)
        self.address_spin = QSpinBox()
        self.address_spin.setRange(1, 255)
        self.address_spin.setValue(1)
        refresh_ports = QPushButton("포트 조회")
        refresh_ports.clicked.connect(self._refresh_ports)
        connect_serial = QPushButton("시리얼 연결")
        connect_serial.clicked.connect(self._connect_serial)
        scan_serial = QPushButton("자동탐색 시작")
        scan_serial.clicked.connect(self._scan_serial)
        serial_layout.addWidget(QLabel("Port"), 0, 0)
        serial_layout.addWidget(self.port_combo, 0, 1, 1, 2)
        serial_layout.addWidget(refresh_ports, 0, 3)
        serial_layout.addWidget(QLabel("Baud"), 1, 0)
        serial_layout.addWidget(self.baud_spin, 1, 1)
        serial_layout.addWidget(QLabel("Address"), 1, 2)
        serial_layout.addWidget(self.address_spin, 1, 3)
        serial_layout.addWidget(connect_serial, 2, 0, 1, 2)
        serial_layout.addWidget(scan_serial, 2, 2, 1, 2)
        layout.addWidget(serial_group)

        move_group = QGroupBox("수동 조작")
        move_layout = QGridLayout(move_group)
        self.pan_level = self._level_combo()
        self.tilt_level = self._level_combo()
        move_layout.addWidget(QLabel("Pan 단계"), 0, 0)
        move_layout.addWidget(self.pan_level, 0, 1)
        move_layout.addWidget(QLabel("Tilt 단계"), 0, 2)
        move_layout.addWidget(self.tilt_level, 0, 3)
        for text, row, column, pan, tilt in (
            ("▲", 1, 1, "stop", "up"),
            ("◀", 2, 0, "left", "stop"),
            ("STOP", 2, 1, "stop", "stop"),
            ("▶", 2, 2, "right", "stop"),
            ("▼", 3, 1, "stop", "down"),
        ):
            button = QPushButton(text)
            if text == "STOP":
                button.clicked.connect(self._stop_motion)
            else:
                button.pressed.connect(lambda p=pan, t=tilt: self._start_jog(p, t))
                button.released.connect(self._stop_motion)
            move_layout.addWidget(button, row, column)
        self.target_pan = QDoubleSpinBox()
        self.target_pan.setRange(0.0, 359.99)
        self.target_pan.setDecimals(2)
        self.target_tilt = QDoubleSpinBox()
        self.target_tilt.setRange(-60.0, 60.0)
        self.target_tilt.setDecimals(2)
        goto_button = QPushButton("지령위치 이동")
        goto_button.clicked.connect(self._goto_absolute)
        pos_button = QPushButton("위치 조회")
        pos_button.clicked.connect(lambda: self._send_command("position.get", {}))
        move_layout.addWidget(QLabel("Pan"), 4, 0)
        move_layout.addWidget(self.target_pan, 4, 1)
        move_layout.addWidget(QLabel("Tilt"), 4, 2)
        move_layout.addWidget(self.target_tilt, 4, 3)
        move_layout.addWidget(goto_button, 5, 0, 1, 2)
        move_layout.addWidget(pos_button, 5, 2, 1, 2)
        layout.addWidget(move_group)

        gamepad_group = QGroupBox("게임패드")
        gamepad_layout = QFormLayout(gamepad_group)
        self.gamepad_combo = QComboBox()
        refresh_gamepads = QPushButton("게임패드 새로고침")
        refresh_gamepads.clicked.connect(self._refresh_gamepads)
        self.gamepad_connect = QPushButton("게임패드 연결")
        self.gamepad_connect.clicked.connect(self._toggle_gamepad)
        self.gamepad_enable = QCheckBox("게임패드로 서버 제어")
        self.gamepad_status = QLabel("-")
        gamepad_row = QWidget()
        row_layout = QHBoxLayout(gamepad_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self.gamepad_combo)
        row_layout.addWidget(refresh_gamepads)
        row_layout.addWidget(self.gamepad_connect)
        gamepad_layout.addRow("Device", gamepad_row)
        gamepad_layout.addRow("", self.gamepad_enable)
        gamepad_layout.addRow("State", self.gamepad_status)
        layout.addWidget(gamepad_group)

        api_group = QGroupBox("Protocol / API")
        api_layout = QGridLayout(api_group)
        self.api_group_combo = QComboBox()
        self.api_command_combo = QComboBox()
        self.api_params = QTextEdit()
        self.api_params.setFixedHeight(86)
        self.api_result = QTextEdit()
        self.api_result.setReadOnly(True)
        self.api_result.setFixedHeight(92)
        api_run = QPushButton("Run")
        api_run.clicked.connect(self._run_api_command)
        self.api_group_combo.currentTextChanged.connect(self._populate_api_commands)
        self.api_command_combo.currentTextChanged.connect(self._load_api_template)
        api_layout.addWidget(QLabel("Group"), 0, 0)
        api_layout.addWidget(self.api_group_combo, 0, 1)
        api_layout.addWidget(QLabel("Command"), 0, 2)
        api_layout.addWidget(self.api_command_combo, 0, 3)
        api_layout.addWidget(QLabel("Params JSON"), 1, 0)
        api_layout.addWidget(self.api_params, 1, 1, 1, 3)
        api_layout.addWidget(api_run, 1, 4)
        api_layout.addWidget(QLabel("Result"), 2, 0)
        api_layout.addWidget(self.api_result, 2, 1, 1, 4)
        layout.addWidget(api_group)

        self.status_label = QLabel("상태 대기")
        layout.addWidget(self.status_label)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        self.setCentralWidget(root)
        self._populate_api_groups()

    @staticmethod
    def _level_combo() -> QComboBox:
        combo = QComboBox()
        for index, percent in enumerate((1, 15, 29, 43, 58, 72, 86, 100), start=1):
            combo.addItem(f"{index}단계 ({percent}%)", index)
        combo.setCurrentIndex(4)
        return combo

    def _populate_api_groups(self) -> None:
        current = self.api_group_combo.currentData()
        groups = sorted({name.split(".", 1)[0] for name in self._api_commands})
        self.api_group_combo.blockSignals(True)
        self.api_group_combo.clear()
        self.api_group_combo.addItem("All", "")
        for group in groups:
            self.api_group_combo.addItem(group, group)
        if current is not None:
            index = self.api_group_combo.findData(current)
            if index >= 0:
                self.api_group_combo.setCurrentIndex(index)
        self.api_group_combo.blockSignals(False)
        self._populate_api_commands()

    def _populate_api_commands(self, _text: str = "") -> None:
        group = self.api_group_combo.currentData() or ""
        previous = self.api_command_combo.currentText()
        commands = [
            command
            for command in self._api_commands
            if not group or command.startswith(group + ".")
        ]
        self.api_command_combo.blockSignals(True)
        self.api_command_combo.clear()
        self.api_command_combo.addItems(commands)
        if previous:
            index = self.api_command_combo.findText(previous)
            if index >= 0:
                self.api_command_combo.setCurrentIndex(index)
        self.api_command_combo.blockSignals(False)
        self._load_api_template()

    def _load_api_template(self, _text: str = "") -> None:
        command = self.api_command_combo.currentText()
        params = self._api_templates.get(command, {})
        self.api_params.setPlainText(json.dumps(params, ensure_ascii=False, indent=2))

    def _update_api_catalog(self, result: dict[str, Any]) -> None:
        commands = result.get("commands")
        templates = result.get("templates")
        if isinstance(commands, list) and all(isinstance(item, str) for item in commands):
            self._api_commands = list(commands)
        base_templates = command_templates(self._api_commands)
        if isinstance(templates, dict):
            base_templates.update(
                {
                    str(key): value
                    for key, value in templates.items()
                    if isinstance(value, dict)
                }
            )
        self._api_templates = base_templates
        self._populate_api_groups()

    def _run_api_command(self) -> None:
        command = self.api_command_combo.currentText()
        if not command:
            return
        try:
            params = json.loads(self.api_params.toPlainText() or "{}")
            if not isinstance(params, dict):
                raise ValueError("params JSON must be an object")
        except (json.JSONDecodeError, ValueError) as exc:
            self.api_result.setPlainText(f"JSON error: {exc}")
            return
        if params.get("confirm") is True:
            answer = QMessageBox.question(
                self,
                "Confirm command",
                f"Run {command}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._send_command(command, params, self._show_api_result)

    def _show_api_result(self, result: dict[str, Any]) -> None:
        self.api_result.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))

    def _toggle_connection(self) -> None:
        if self.socket.state() == QAbstractSocket.SocketState.ConnectedState:
            self.auto_server_reconnect.setChecked(False)
            self.socket.disconnectFromHost()
            return
        self._connect_to_server()

    def _connect_to_server(self) -> None:
        if self.socket.state() in {
            QAbstractSocket.SocketState.ConnectedState,
            QAbstractSocket.SocketState.ConnectingState,
        }:
            return
        self.connection_label.setText("연결 중...")
        self.socket.connectToHost(self.host_edit.text().strip(), self.port_spin.value())

    def _retry_server_connection(self) -> None:
        if self.auto_server_reconnect.isChecked():
            self._connect_to_server()

    def _on_connected(self) -> None:
        self.reconnect_timer.stop()
        self.connect_button.setText("TCP 연결 해제")
        self.connection_label.setText("TCP 연결됨")
        self._log("SERVER connected")
        self._refresh_ports()
        self._send_command(
            "serial.auto_reconnect",
            {"enabled": True, "baudrates": [self.baud_spin.value()]},
            quiet=True,
        )
        self._send_command("protocol.catalog", {}, self._update_api_catalog, quiet=True)
        self._request_status()
        self.status_timer.start()

    def _on_disconnected(self) -> None:
        self.status_timer.stop()
        self.jog_timer.stop()
        self._current_jog = None
        self.connect_button.setText("TCP 연결")
        self.connection_label.setText("연결 끊김")
        self._log("SERVER disconnected")
        if self.auto_server_reconnect.isChecked():
            self.reconnect_timer.start()

    def _on_socket_error(self, _error: QAbstractSocket.SocketError) -> None:
        self._log(f"SERVER ERR {self.socket.errorString()}")

    def _on_ready_read(self) -> None:
        self._buffer.extend(bytes(self.socket.readAll()))
        while b"\n" in self._buffer:
            raw_line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            raw_line = raw_line.rstrip(b"\r")
            if raw_line:
                self._handle_message(raw_line)

    def _handle_message(self, raw_line: bytes) -> None:
        try:
            message = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._log(f"RX parse error: {exc}")
            return
        if "event" in message:
            self._log(f"EVENT {message['event']} {message.get('data', {})}")
            return
        request_id = str(message.get("id"))
        callback = self._pending.pop(request_id, None)
        if not message.get("ok"):
            self._log(f"ERR {message.get('error')}")
            return
        result = message.get("result", {})
        if callback is not None:
            callback(result)

    def _send_command(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        callback: ResponseCallback | None = None,
        *,
        quiet: bool = False,
    ) -> None:
        if self.socket.state() != QAbstractSocket.SocketState.ConnectedState:
            if not quiet:
                self._log("SERVER not connected")
            return
        request_id = uuid.uuid4().hex
        self._pending[request_id] = callback
        packet = {"id": request_id, "command": command, "params": params or {}}
        self.socket.write((json.dumps(packet, ensure_ascii=False) + "\n").encode("utf-8"))
        if not quiet:
            self._log(f"TX {command} {params or {}}")

    def _request_status(self, *, quiet: bool = False) -> None:
        self._send_command("system.status", {}, self._update_status, quiet=quiet)

    def _update_status(self, status: dict[str, Any]) -> None:
        serial = status.get("serial", {})
        position = status.get("position", {})
        motion = status.get("motion", {})
        laser = status.get("laser", {})
        self._laser_on = bool(laser.get("on"))
        self.status_label.setText(
            "Serial: {port} / {baudrate} / addr {address} / {connected} | "
            "Pan {pan} Tilt {tilt} | Motion {motion} | Laser {laser}".format(
                port=serial.get("port") or "-",
                baudrate=serial.get("baudrate") or "-",
                address=serial.get("address") or "-",
                connected="ON" if serial.get("connected") else "OFF",
                pan=position.get("pan"),
                tilt=position.get("tilt"),
                motion=motion.get("state"),
                laser="ON" if laser.get("on") else "OFF",
            )
        )

    def _refresh_ports(self) -> None:
        self._send_command("serial.ports", {}, self._update_ports, quiet=True)

    def _update_ports(self, result: dict[str, Any]) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        for item in result.get("ports", []):
            self.port_combo.addItem(
                f"{item.get('port')} · {item.get('description', '')}",
                item.get("port"),
            )
        if current:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def _connect_serial(self) -> None:
        port = self.port_combo.currentData()
        if not port:
            self._log("Serial port not selected")
            return
        self._send_command(
            "serial.connect",
            {
                "port": port,
                "baudrate": self.baud_spin.value(),
                "address": self.address_spin.value(),
                "auto_reconnect": True,
            },
        )

    def _scan_serial(self) -> None:
        self._send_command(
            "serial.scan",
            {
                "baudrates": [self.baud_spin.value()],
                "first_address": self.address_spin.value(),
                "last_address": self.address_spin.value(),
                "timeout_ms": 200,
            },
        )

    def _start_jog(self, pan: str, tilt: str) -> None:
        self._current_jog = (pan, tilt)
        self._send_current_jog()
        self.jog_timer.start()

    def _send_current_jog(self) -> None:
        if self._current_jog is None:
            return
        pan, tilt = self._current_jog
        self._send_command(
            "motion.jog",
            {
                "pan": pan,
                "tilt": tilt,
                "pan_level": int(self.pan_level.currentData()),
                "tilt_level": int(self.tilt_level.currentData()),
                "duration_ms": 5000,
            },
            quiet=True,
        )

    def _stop_motion(self) -> None:
        self._current_jog = None
        self._gamepad_motion = None
        self.jog_timer.stop()
        self._send_command("motion.stop", {}, quiet=True)

    def _goto_absolute(self) -> None:
        self._send_command(
            "motion.absolute",
            {"pan": self.target_pan.value(), "tilt": self.target_tilt.value()},
        )

    def _refresh_gamepads(self) -> None:
        self.gamepad_manager.refresh_devices()

    def _on_gamepad_devices(self, devices: list[object]) -> None:
        previous = self.gamepad_combo.currentData()
        self.gamepad_combo.clear()
        for device in devices:
            mode = "SDL" if device.standard_mapping else "raw"
            self.gamepad_combo.addItem(f"{device.index}: {device.name} · {mode}", device.index)
        if previous is not None:
            index = self.gamepad_combo.findData(previous)
            if index >= 0:
                self.gamepad_combo.setCurrentIndex(index)

    def _toggle_gamepad(self) -> None:
        if self.gamepad_manager.is_connected:
            self.gamepad_manager.set_auto_reconnect(False)
            self.gamepad_manager.close(reconnect=False)
            return
        index = self.gamepad_combo.currentData()
        if index is not None:
            self.gamepad_manager.connect_device(int(index), auto_reconnect=True)

    def _on_gamepad_connected(self, name: str) -> None:
        self.gamepad_connect.setText("게임패드 연결 해제")
        self.gamepad_status.setText(f"연결됨: {name}")

    def _on_gamepad_disconnected(self, reason: str) -> None:
        self.gamepad_connect.setText("게임패드 연결")
        self.gamepad_status.setText(reason)
        self._stop_motion()

    def _on_gamepad_state(self, state: GamepadState) -> None:
        if not self.gamepad_enable.isChecked():
            return
        motion = axes_to_motion(state.axes, 1, 1)
        if motion is None:
            if self._gamepad_motion is not None:
                self._stop_motion()
            self._gamepad_motion = None
        else:
            pan, tilt, _pan_speed, _tilt_speed = motion
            pan_text = {
                PanDirection.LEFT: "left",
                PanDirection.RIGHT: "right",
                PanDirection.STOP: "stop",
            }[pan]
            tilt_text = {
                TiltDirection.UP: "up",
                TiltDirection.DOWN: "down",
                TiltDirection.STOP: "stop",
            }[tilt]

            next_motion = (pan_text, tilt_text)
            changed = next_motion != self._gamepad_motion
            self._gamepad_motion = next_motion
            self._current_jog = next_motion

            # pygame polling is 40 ms, but sending a motor command on every poll
            # floods the API/serial path. Send immediately only when direction
            # changes; the timer below refreshes the server watchdog afterwards.
            if changed:
                self._send_current_jog()
            if not self.jog_timer.isActive():
                self.jog_timer.start()

        hat_index = gamepad_map.HAT_INDEX
        current_hat = (
            state.hats[hat_index]
            if hat_index is not None and 0 <= hat_index < len(state.hats)
            else (0, 0)
        )
        if current_hat != self._previous_hat:
            self._step_combo(self.pan_level, current_hat[0])
            self._step_combo(self.tilt_level, current_hat[1])
            self._previous_hat = current_hat

        buttons = state.buttons
        if self._button_edge(gamepad_map.BUTTON_STOP, buttons):
            self._stop_motion()
        if self._button_edge(gamepad_map.BUTTON_HOME, buttons):
            self._stop_motion()
            self._send_command("motion.absolute", {"pan": 0, "tilt": 0})
        if self._button_edge(gamepad_map.BUTTON_LASER_TOGGLE, buttons):
            self._send_command("laser.off" if self._laser_on else "laser.on", {})
        self._previous_buttons = buttons

    def _button_edge(self, index: int | None, current: tuple[bool, ...]) -> bool:
        if index is None or index < 0 or index >= len(current):
            return False
        previous = self._previous_buttons[index] if index < len(self._previous_buttons) else False
        return current[index] and not previous

    @staticmethod
    def _step_combo(combo: QComboBox, delta: int) -> None:
        if delta == 0:
            return
        combo.setCurrentIndex(max(0, min(combo.count() - 1, combo.currentIndex() + delta)))

    def _log(self, text: str) -> None:
        self.log.append(text)
