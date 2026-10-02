"""Main PySide6 user interface for the PT503/PT510 functional tester."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from . import gamepad_mapping as gamepad_map
from .api_metadata import (
    protocol_catalog as api_protocol_catalog,
    supported_command_names,
)
from .control_api import (
    DEFAULT_PORT,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    ApiCommandError,
    ControlApiServer,
)
from .gamepad_control import axes_to_motion
from .gamepad_manager import GamepadDevice, GamepadManager, GamepadState
from .log_store import LogEntry, LogStore
from .motion_tracker import MotionTracker, TrackerResult
from .preset_store import PresetStore
from .recipe_store import RecipePoint, RecipeStore
from .protocol import (
    AUTO_PAN_SPEED,
    AUTO_TILT_SPEED,
    OutgoingCommand,
    PanDirection,
    ProtocolError,
    TiltDirection,
    adjust_preset_speed,
    adjust_scan_speed,
    auto_position_speed,
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
    manual_speed_percent,
    manual_speed_value,
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
    zone_scan,
)
from .serial_worker import SerialConfig, SerialWorker

APP_STYLE = """
QMainWindow, QWidget { background: #171b22; color: #e7edf5; font-size: 10pt; }
QGroupBox { border: 1px solid #394351; border-radius: 7px; margin-top: 12px; padding-top: 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #9fc6ff; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #222936; border: 1px solid #465367; border-radius: 4px;
    padding: 5px; padding-right: 24px; min-height: 22px;
}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    width: 20px; border-left: 1px solid #526177; background: #344154;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background: #1769aa; }
QPushButton { background: #2d3746; border: 1px solid #4b5a70; border-radius: 5px; padding: 7px 12px; }
QPushButton:hover { background: #3a485c; }
QPushButton:pressed { background: #1e6fbf; }
QPushButton:disabled { color: #687384; background: #20252d; border-color: #303742; }
QPushButton#primary { background: #1769aa; border-color: #2b88d8; font-weight: 700; }
QPushButton#stop { background: #a92c35; border-color: #e04b57; font-weight: 800; font-size: 12pt; }
QPushButton#danger { background: #702a30; border-color: #a8414b; }
QTabWidget::pane { border: 1px solid #394351; }
QTabBar::tab { background: #252c37; padding: 9px 14px; border: 1px solid #394351; }
QTabBar::tab:selected { background: #1769aa; }
QTableWidget { background: #11151b; alternate-background-color: #171d25; gridline-color: #2e3744; }
QHeaderView::section { background: #273140; color: #dbe8f7; padding: 6px; border: 0; }
QProgressBar { border: 1px solid #465367; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background: #2185d0; }
QToolTip { color: #f4f7fb; background: #263142; border: 1px solid #5a6b84; }
"""

SILENT_PREFIX = "[자동위치조회:숨김] "


DEFAULT_DRAWING_DIR = Path(r"C:\Users\gram\Desktop\트래커 관련\도면 이미지\EODCT288_ASSY")
STARTUP_BAUDRATE = 9600
STARTUP_SCAN_TIMEOUT_MS = 200
STARTUP_SCAN_DELAY_MS = 250
PELCOD_MANUAL_REFRESH_MS = 1000
PELCOD_MIN_DYNAMIC_UPDATE_S = 0.35


class SearchDialog(QDialog):
    """Collect a safe, query-only discovery range."""

    def __init__(self, parent: QWidget, port_count: int) -> None:
        super().__init__(parent)
        self.setWindowTitle("PT503 자동검색 설정")
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)

        info = QLabel(
            "자동검색은 장비를 움직이지 않고 Pan 위치 조회만 전송합니다. "
            "전체 주소 검색은 수 분이 걸릴 수 있습니다."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        self.scope = QComboBox()
        self.scope.addItem("현재 선택한 COM 포트", "selected")
        self.scope.addItem(f"검색된 모든 COM 포트 ({port_count}개)", "all")
        form.addRow("검색 포트", self.scope)

        baud_widget = QWidget()
        baud_layout = QHBoxLayout(baud_widget)
        baud_layout.setContentsMargins(0, 0, 0, 0)
        self.baud_checks: list[QCheckBox] = []
        for baud in (2400, 4800, 9600, 19200):
            check = QCheckBox(str(baud))
            check.setChecked(True)
            check.toggled.connect(self._update_estimate)
            baud_layout.addWidget(check)
            self.baud_checks.append(check)
        form.addRow("Baud rate", baud_widget)

        range_widget = QWidget()
        range_layout = QHBoxLayout(range_widget)
        range_layout.setContentsMargins(0, 0, 0, 0)
        self.first_address = QSpinBox()
        self.first_address.setRange(1, 255)
        self.first_address.setValue(1)
        self.last_address = QSpinBox()
        self.last_address.setRange(1, 255)
        self.last_address.setValue(16)
        self.first_address.valueChanged.connect(self._update_estimate)
        self.last_address.valueChanged.connect(self._update_estimate)
        range_layout.addWidget(self.first_address)
        range_layout.addWidget(QLabel("~"))
        range_layout.addWidget(self.last_address)
        form.addRow("주소 범위", range_widget)

        self.timeout_ms = QSpinBox()
        self.timeout_ms.setRange(80, 1000)
        self.timeout_ms.setSingleStep(20)
        self.timeout_ms.setValue(200)
        self.timeout_ms.setSuffix(" ms")
        self.timeout_ms.valueChanged.connect(self._update_estimate)
        form.addRow("주소당 응답 대기", self.timeout_ms)
        layout.addLayout(form)

        self.estimate = QLabel()
        self.estimate.setStyleSheet("color:#f5c06b")
        layout.addWidget(self.estimate)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._port_count = max(1, port_count)
        self.scope.currentIndexChanged.connect(self._update_estimate)
        self._update_estimate()

    def selected_bauds(self) -> list[int]:
        return [int(check.text()) for check in self.baud_checks if check.isChecked()]

    def accept(self) -> None:
        if not self.selected_bauds():
            QMessageBox.warning(self, "설정 확인", "Baud rate를 하나 이상 선택하세요.")
            return
        if self.first_address.value() > self.last_address.value():
            QMessageBox.warning(
                self, "설정 확인", "시작 주소가 종료 주소보다 클 수 없습니다."
            )
            return
        super().accept()

    def _update_estimate(self) -> None:
        bauds = max(1, len(self.selected_bauds()))
        addresses = max(0, self.last_address.value() - self.first_address.value() + 1)
        ports = self._port_count if self.scope.currentData() == "all" else 1
        attempts = bauds * addresses * ports
        seconds = attempts * self.timeout_ms.value() / 1000.0
        self.estimate.setText(f"최대 {attempts:,}회 조회 · 예상 최대 {seconds:.1f}초")


class JogDial(QWidget):
    jog_pressed = Signal(object, object)
    jog_released = Signal()
    stop_clicked = Signal()

    _DIRECTION_MAP = {
        "up": (PanDirection.STOP, TiltDirection.UP),
        "down": (PanDirection.STOP, TiltDirection.DOWN),
        "left": (PanDirection.LEFT, TiltDirection.STOP),
        "right": (PanDirection.RIGHT, TiltDirection.STOP),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(260, 260)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("방향 영역을 누르고 있는 동안 이동합니다. 가운데 버튼은 즉시 STOP입니다.")
        self._active_direction: str | None = None

    def paintEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center, outer_radius, inner_radius = self._dial_geometry()
        cx = center.x()
        cy = center.y()

        outer_rect = QRectF(cx - outer_radius, cy - outer_radius, outer_radius * 2, outer_radius * 2)
        ring_gradient = QRadialGradient(QPointF(cx - 18, cy - 20), outer_radius * 1.1)
        ring_gradient.setColorAt(0.0, QColor("#f7f8f6"))
        ring_gradient.setColorAt(0.44, QColor("#9aa1a8"))
        ring_gradient.setColorAt(0.72, QColor("#10141b"))
        ring_gradient.setColorAt(1.0, QColor("#020308"))
        painter.setPen(QPen(QColor("#030509"), 5))
        painter.setBrush(ring_gradient)
        painter.drawEllipse(outer_rect)

        sector_outer = outer_radius * 0.82
        sector_inner = inner_radius * 0.78
        for direction, start_angle in (
            ("right", -42),
            ("up", 48),
            ("left", 138),
            ("down", 228),
        ):
            path = self._sector_path(center, sector_outer, sector_inner, start_angle, 84)
            gradient = QLinearGradient(cx - outer_radius, cy - outer_radius, cx + outer_radius, cy + outer_radius)
            if direction == self._active_direction:
                gradient.setColorAt(0.0, QColor("#eaf7ff"))
                gradient.setColorAt(0.55, QColor("#69aee8"))
                gradient.setColorAt(1.0, QColor("#1f5f91"))
            else:
                gradient.setColorAt(0.0, QColor("#fbfbf8"))
                gradient.setColorAt(0.55, QColor("#b8bdc2"))
                gradient.setColorAt(1.0, QColor("#666d75"))
            painter.setPen(QPen(QColor("#080b10"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(gradient)
            painter.drawPath(path)

        painter.setPen(QPen(QColor("#05070b"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for angle in (45, 135, 225, 315):
            radians = math.radians(angle)
            start = QPointF(cx + sector_inner * math.cos(radians), cy - sector_inner * math.sin(radians))
            end = QPointF(cx + sector_outer * math.cos(radians), cy - sector_outer * math.sin(radians))
            painter.drawLine(start, end)

        center_rect = QRectF(cx - inner_radius, cy - inner_radius, inner_radius * 2, inner_radius * 2)
        center_gradient = QRadialGradient(QPointF(cx - 16, cy - 18), inner_radius * 1.25)
        center_gradient.setColorAt(0.0, QColor("#ffffff"))
        center_gradient.setColorAt(0.48, QColor("#cfd3d6"))
        center_gradient.setColorAt(1.0, QColor("#6d747c"))
        painter.setPen(QPen(QColor("#080b10"), 6))
        painter.setBrush(center_gradient)
        painter.drawEllipse(center_rect)

        if self._active_direction == "center":
            painter.setPen(QPen(QColor("#248bd0"), 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center_rect.adjusted(6, 6, -6, -6))

        painter.setBrush(QColor("#141820"))
        painter.setPen(Qt.PenStyle.NoPen)
        arrow_size = outer_radius * 0.17
        arrow_offset = outer_radius * 0.43
        arrows = {
            "up": [
                QPointF(cx, cy - arrow_offset - arrow_size * 0.7),
                QPointF(cx - arrow_size * 0.65, cy - arrow_offset + arrow_size * 0.55),
                QPointF(cx + arrow_size * 0.65, cy - arrow_offset + arrow_size * 0.55),
            ],
            "down": [
                QPointF(cx, cy + arrow_offset + arrow_size * 0.7),
                QPointF(cx - arrow_size * 0.65, cy + arrow_offset - arrow_size * 0.55),
                QPointF(cx + arrow_size * 0.65, cy + arrow_offset - arrow_size * 0.55),
            ],
            "left": [
                QPointF(cx - arrow_offset - arrow_size * 0.7, cy),
                QPointF(cx - arrow_offset + arrow_size * 0.55, cy - arrow_size * 0.65),
                QPointF(cx - arrow_offset + arrow_size * 0.55, cy + arrow_size * 0.65),
            ],
            "right": [
                QPointF(cx + arrow_offset + arrow_size * 0.7, cy),
                QPointF(cx + arrow_offset - arrow_size * 0.55, cy - arrow_size * 0.65),
                QPointF(cx + arrow_offset - arrow_size * 0.55, cy + arrow_size * 0.65),
            ],
        }
        for points in arrows.values():
            painter.drawPolygon(QPolygonF(points))

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature
        if event.button() != Qt.MouseButton.LeftButton:
            return
        direction = self._direction_at(event.position())
        self._active_direction = direction
        self.update()
        self._emit_direction(direction)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature
        if event.button() == Qt.MouseButton.LeftButton and self._active_direction in self._DIRECTION_MAP:
            self.jog_released.emit()
        self._active_direction = None
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt override signature
        # Do not interpret cursor-leave as key OFF. Qt keeps the mouse grabbed
        # while a button is pressed, so mouseReleaseEvent remains the real OFF.
        # This prevents a held manual JOG from stopping if the cursor drifts
        # outside the dial before the operator releases the mouse button.
        super().leaveEvent(event)

    def _emit_direction(self, direction: str | None) -> None:
        if direction == "center":
            self.stop_clicked.emit()
            return
        if direction in self._DIRECTION_MAP:
            pan, tilt = self._DIRECTION_MAP[direction]
            self.jog_pressed.emit(pan, tilt)

    def _direction_at(self, pos: QPointF) -> str | None:
        center, outer_radius, inner_radius = self._dial_geometry()
        dx = pos.x() - center.x()
        dy = pos.y() - center.y()
        distance = math.hypot(dx, dy)
        if distance > outer_radius:
            return None
        if distance <= inner_radius:
            return "center"
        if abs(dx) > abs(dy):
            return "right" if dx > 0 else "left"
        return "down" if dy > 0 else "up"

    def _dial_geometry(self) -> tuple[QPointF, float, float]:
        side = min(self.width(), self.height())
        center = QPointF(self.width() / 2, self.height() / 2)
        return center, side * 0.46, side * 0.19

    @staticmethod
    def _sector_path(center: QPointF, outer_radius: float, inner_radius: float, start_angle: float, span_angle: float) -> QPainterPath:
        cx = center.x()
        cy = center.y()
        outer_rect = QRectF(cx - outer_radius, cy - outer_radius, outer_radius * 2, outer_radius * 2)
        inner_rect = QRectF(cx - inner_radius, cy - inner_radius, inner_radius * 2, inner_radius * 2)
        path = QPainterPath()
        path.arcMoveTo(outer_rect, start_angle)
        path.arcTo(outer_rect, start_angle, span_angle)
        path.arcTo(inner_rect, start_angle + span_angle, -span_angle)
        path.closeSubpath()
        return path


class MainWindow(QMainWindow):
    MAX_LOG_ROWS = 5000

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BIT-PT503 / PT510 기능 시험기")
        self.resize(1460, 900)
        self.setMinimumSize(1180, 720)
        self.setStyleSheet(APP_STYLE)

        self.settings = QSettings("OSRND", "PT503Tester")
        self.log_store = LogStore()
        self.preset_store = PresetStore()
        self.recipe_store = RecipeStore()
        self.worker = SerialWorker()
        self.gamepad_manager = GamepadManager(self)
        self.api_server = ControlApiServer(self._handle_api_command, self)
        self.is_connected = False
        self.is_scanning = False
        self.current_jog: tuple[PanDirection, TiltDirection] | None = None
        self.last_tx_time = 0.0
        self.last_tx_checksum: int | None = None
        self.pending_response: dict[tuple[int, int], tuple[float, bool, str]] = {}
        self.last_tx_frames: list[tuple[bytes, bool]] = []
        self.recent_tx_meta: list[tuple[int, float, bool, str]] = []
        self.last_tx_silent = False
        self.last_tx_description = ""
        self.monitor_axis = "pan"
        self.search_progress: QProgressDialog | None = None
        self.scan_progress_dialog_enabled = True
        self.scan_notify_on_failure = True
        self.current_pan: float | None = None
        self.current_tilt: float | None = None
        self.motion_tracker: MotionTracker | None = None
        self.motion_callback: Callable[[], None] | None = None
        self.preset_capture: dict[str, object] | None = None
        self.recipe_edit_point_id = ""
        self.laser_is_on = False
        self.gamepad_motion: tuple[PanDirection, TiltDirection, int, int] | None = None
        self.gamepad_last_send = 0.0
        self.gamepad_previous_axes: tuple[float, ...] = ()
        self.gamepad_previous_buttons: tuple[bool, ...] = ()
        self.gamepad_previous_hat = (0, 0)
        self.gamepad_stop_latched = False
        self.api_motion_owner: str | None = None
        # Last physical API JOG command. Identical keepalives refresh only the
        # watchdog and must not re-send/restart the PT motor command.
        self.api_jog_key: tuple[PanDirection, TiltDirection, int, int] | None = None
        self.api_laser_owner: str | None = None
        self.api_motion_context: dict[str, Any] | None = None
        self.api_scan_context: dict[str, Any] | None = None
        self.auto_gamepad_on_serial_connect = True
        self.drawing_model_path = ""
        self.drawing_points: list[DrawingPoint] = []
        self.drawing_teach_capture: dict[str, object] | None = None
        self.drawing_upload_queue: list[tuple[int, DrawingPoint]] = []
        self.drawing_upload_active = False
        self.drawing_upload_address = 0

        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self._monitor_tick)
        self.laser_timer = QTimer(self)
        self.laser_timer.setSingleShot(True)
        self.laser_timer.timeout.connect(self._laser_off)
        self.jog_keepalive = QTimer(self)
        # Pelco-D runaway protection typically stops unattended motion after
        # at a device-dependent interval. PT503 field behavior can stop sooner than
        # generic Pelco guidance, so refresh an active manual command every 1 s.
        # Release/STOP remains the authoritative software stop condition.
        self.jog_keepalive.setInterval(PELCOD_MANUAL_REFRESH_MS)
        self.jog_keepalive.timeout.connect(self._repeat_jog)
        self.api_jog_watchdog = QTimer(self)
        self.api_jog_watchdog.setSingleShot(False)
        self.api_jog_watchdog.setInterval(PELCOD_MANUAL_REFRESH_MS)
        self.api_jog_watchdog.timeout.connect(self._api_jog_keepalive)

        self._build_ui()
        self._configure_spin_boxes()
        self._connect_worker()
        self._connect_gamepad()
        self._connect_api()
        self._load_settings()
        self._refresh_ports()
        self._refresh_gamepads()

        self.worker.start()
        self._set_connected(False, "연결 대기")
        if self.api_auto_start.isChecked():
            self._start_api_server()
        self._append_event(
            "SYSTEM",
            "",
            f"프로그램 시작 · 자동 로그: {self.log_store.session_path}",
        )

        QTimer.singleShot(STARTUP_SCAN_DELAY_MS, self._start_startup_scan)

    # ---------- UI construction ----------
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(self._connection_panel())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._manual_tab(), "수동·위치")
        self.tabs.addTab(self._recipe_tab(), "레시피·포인트")
        self.tabs.addTab(self._preset_tab(), "스캔·홈")
        self.tabs.addTab(self._gamepad_tab(), "게임패드")
        self.tabs.addTab(self._laser_tab(), "레이저 모듈")
        self.tabs.addTab(self._api_tab(), "외부 APP API")
        self.tabs.addTab(self._advanced_tab(), "고급·점검")
        splitter.addWidget(self.tabs)
        splitter.addWidget(self._log_panel())
        splitter.setSizes([560, 860])
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)

        status = QStatusBar()
        self.connection_status = QLabel("연결 대기")
        self.command_state = QLabel("명령 상태: -")
        self.last_activity = QLabel("마지막 통신: -")
        status.addWidget(self.connection_status, 1)
        status.addPermanentWidget(self.command_state)
        status.addPermanentWidget(self.last_activity)
        self.setStatusBar(status)

    def _connection_panel(self) -> QGroupBox:
        group = QGroupBox("통신 설정")
        layout = QHBoxLayout(group)
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(250)
        self.refresh_button = QPushButton("포트 새로고침")
        self.refresh_button.clicked.connect(self._refresh_ports)

        self.baud_combo = QComboBox()
        for baud in (2400, 4800, 9600, 19200):
            self.baud_combo.addItem(str(baud), baud)
        self.protocol_combo = QComboBox()
        self.protocol_combo.addItem("Pelco-D", "pelco-d")
        self.address_spin = QSpinBox()
        self.address_spin.setRange(1, 255)
        self.address_spin.setValue(1)
        self.address_spin.valueChanged.connect(self._update_preset_display)

        self.connect_button = QPushButton("연결")
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(self._toggle_connection)
        self.search_button = QPushButton("안전 자동검색")
        self.search_button.clicked.connect(self._start_search)

        for label, widget in (
            ("COM", self.port_combo),
            ("", self.refresh_button),
            ("Baud", self.baud_combo),
            ("Protocol", self.protocol_combo),
            ("주소", self.address_spin),
            ("", self.connect_button),
            ("", self.search_button),
        ):
            if label:
                layout.addWidget(QLabel(label))
            layout.addWidget(widget)
        layout.addStretch(1)
        return group

    def _manual_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        position_group = QGroupBox("실시간 위치")
        position_layout = QGridLayout(position_group)
        self.pan_value = self._large_value("---.--°")
        self.tilt_value = self._large_value("---.--°")
        position_layout.addWidget(
            QLabel("PAN"), 0, 0, alignment=Qt.AlignmentFlag.AlignCenter
        )
        position_layout.addWidget(
            QLabel("TILT (+위 / -아래)"), 0, 1, alignment=Qt.AlignmentFlag.AlignCenter
        )
        position_layout.addWidget(self.pan_value, 1, 0)
        position_layout.addWidget(self.tilt_value, 1, 1)

        monitor_row = QHBoxLayout()
        self.monitor_check = QCheckBox("위치 자동 모니터링")
        self.monitor_check.toggled.connect(self._toggle_monitoring)
        self.monitor_interval = QSpinBox()
        self.monitor_interval.setRange(350, 5000)
        self.monitor_interval.setValue(500)
        self.monitor_interval.setSuffix(" ms/명령")
        self.monitor_interval.valueChanged.connect(self._update_monitor_interval)
        query_button = QPushButton("지금 위치 조회")
        query_button.clicked.connect(self._query_all_positions)
        monitor_row.addWidget(self.monitor_check)
        monitor_row.addWidget(self.monitor_interval)
        monitor_row.addStretch()
        monitor_row.addWidget(query_button)
        position_layout.addLayout(monitor_row, 2, 0, 1, 2)

        completion_row = QHBoxLayout()
        self.completion_tolerance = QDoubleSpinBox()
        self.completion_tolerance.setRange(0.05, 5.0)
        self.completion_tolerance.setSingleStep(0.05)
        self.completion_tolerance.setDecimals(2)
        self.completion_tolerance.setValue(0.20)
        self.completion_tolerance.setSuffix("°")
        self.completion_samples = QSpinBox()
        self.completion_samples.setRange(1, 10)
        self.completion_samples.setValue(3)
        self.completion_timeout = QSpinBox()
        self.completion_timeout.setRange(2, 120)
        self.completion_timeout.setValue(30)
        self.completion_timeout.setSuffix(" s")
        completion_help = (
            "Pelco-D에는 이동 완료 전용 프레임이 없습니다. 앱이 위치를 조용히 조회해 "
            "목표 오차 안에 연속으로 들어오면 완료로 추정합니다."
        )
        for widget in (
            self.completion_tolerance,
            self.completion_samples,
            self.completion_timeout,
        ):
            widget.setToolTip(completion_help)
        completion_row.addWidget(QLabel("완료 오차"))
        completion_row.addWidget(self.completion_tolerance)
        completion_row.addWidget(QLabel("연속 샘플"))
        completion_row.addWidget(self.completion_samples)
        completion_row.addWidget(QLabel("타임아웃"))
        completion_row.addWidget(self.completion_timeout)
        position_layout.addLayout(completion_row, 3, 0, 1, 2)
        completion_note = self._help_label(completion_help)
        position_layout.addWidget(completion_note, 4, 0, 1, 2)
        layout.addWidget(position_group)

        jog_group = QGroupBox("수동 Jog · 버튼을 누르는 동안 이동, 놓으면 STOP")
        jog_layout = QGridLayout(jog_group)
        self.pan_speed = QSpinBox()
        self.pan_speed.setRange(1, 8)
        self.pan_speed.setValue(5)
        self.pan_speed.setSuffix(" 단계")
        self.tilt_speed = QSpinBox()
        self.tilt_speed.setRange(1, 8)
        self.tilt_speed.setValue(5)
        self.tilt_speed.setSuffix(" 단계")
        speed_help = "1~8단계: 1%, 15%, 29%, 43%, 58%, 72%, 86%, 100%"
        self.pan_speed.setToolTip(speed_help)
        self.tilt_speed.setToolTip(speed_help)
        self.invert_pan = QCheckBox("Pan 방향 반전")
        self.invert_tilt = QCheckBox("Tilt 방향 반전")

        jog_layout.addWidget(QLabel("Pan 수동 속도 (1~8단계)"), 0, 0)
        jog_layout.addWidget(self.pan_speed, 0, 1)
        jog_layout.addWidget(QLabel("Tilt 수동 속도 (1~8단계)"), 0, 2)
        jog_layout.addWidget(self.tilt_speed, 0, 3)
        jog_layout.addWidget(self.invert_pan, 1, 0, 1, 2)
        jog_layout.addWidget(self.invert_tilt, 1, 2, 1, 2)

        self.jog_dial = JogDial()
        self.jog_dial.jog_pressed.connect(self._start_jog)
        self.jog_dial.jog_released.connect(self._end_jog)
        self.jog_dial.stop_clicked.connect(self._emergency_stop)
        jog_layout.addWidget(self.jog_dial, 2, 0, 1, 4, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(jog_group)

        absolute_group = QGroupBox("절대좌표 이동")
        absolute_layout = QGridLayout(absolute_group)
        self.target_pan = QDoubleSpinBox()
        self.target_pan.setRange(0.0, 359.99)
        self.target_pan.setDecimals(2)
        self.target_pan.setSuffix("°")
        self.target_tilt = QDoubleSpinBox()
        self.target_tilt.setRange(-60.0, 60.0)
        self.target_tilt.setDecimals(2)
        self.target_tilt.setSuffix("°")
        move_pan = QPushButton("Pan 이동")
        move_pan.clicked.connect(self._move_pan)
        move_tilt = QPushButton("Tilt 이동")
        move_tilt.clicked.connect(self._move_tilt)
        move_both = QPushButton("Pan → Tilt 순차 이동")
        move_both.setObjectName("primary")
        move_both.clicked.connect(self._move_both)

        absolute_layout.addWidget(QLabel("Pan 목표 0~359.99°"), 0, 0)
        absolute_layout.addWidget(self.target_pan, 0, 1)
        absolute_layout.addWidget(move_pan, 0, 2)
        absolute_layout.addWidget(QLabel("Tilt 목표 -60~+60° (+위/-아래)"), 1, 0)
        absolute_layout.addWidget(self.target_tilt, 1, 1)
        absolute_layout.addWidget(move_tilt, 1, 2)
        absolute_layout.addWidget(QLabel("자동 이동 속도"), 2, 0)
        absolute_layout.addWidget(QLabel("최고속도 고정 (Pan 63 / Tilt 63)"), 2, 1)
        absolute_layout.addWidget(move_both, 2, 2)
        layout.addWidget(absolute_group)
        layout.addStretch()
        return page

    def _preset_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        # Preset save/call UI removed. Points are managed by recipe tables.

        scan_group = QGroupBox("라인 스캔")
        scan_layout = QGridLayout(scan_group)
        scan_help_text = (
            "라인 스캔은 현재 위치를 시작점·종료점으로 저장한 뒤 두 지점 사이를 반복 이동합니다. "
            "제조사 방식(매직 프리셋 0x6E/0x6F)과 표준 Zone Scan 중 펌웨어가 지원하는 쪽을 시험하세요."
        )
        scan_group.setToolTip(scan_help_text)
        set_start = QPushButton("현재 위치를 시작점으로")
        set_start.clicked.connect(
            lambda: self._send(set_line_scan_point(self._address(), True))
        )
        set_end = QPushButton("현재 위치를 종료점으로")
        set_end.clicked.connect(
            lambda: self._send(set_line_scan_point(self._address(), False))
        )
        vendor_start = QPushButton("제조사 스캔 시작")
        vendor_start.setObjectName("primary")
        vendor_start.clicked.connect(lambda: self._start_auto_scan("vendor"))
        vendor_stop = QPushButton("제조사 스캔 정지")
        vendor_stop.clicked.connect(
            lambda: self._send(vendor_line_scan(self._address(), False))
        )
        pelco_start = QPushButton("표준 Zone Scan ON")
        pelco_start.clicked.connect(lambda: self._start_auto_scan("zone"))
        pelco_stop = QPushButton("표준 Zone Scan OFF")
        pelco_stop.clicked.connect(
            lambda: self._send(zone_scan(self._address(), False))
        )
        scan_layout.addWidget(set_start, 0, 0)
        scan_layout.addWidget(set_end, 0, 1)
        scan_layout.addWidget(vendor_start, 1, 0)
        scan_layout.addWidget(vendor_stop, 1, 1)
        scan_layout.addWidget(pelco_start, 2, 0)
        scan_layout.addWidget(pelco_stop, 2, 1)
        scan_layout.addWidget(QLabel("이동 속도"), 3, 0)
        scan_layout.addWidget(QLabel("최고속도 고정"), 3, 1)
        scan_layout.addWidget(self._help_label(scan_help_text), 4, 0, 1, 2)
        layout.addWidget(scan_group)

        cruise_group = QGroupBox("크루징 · 장비 내부 경로 구성 사용")
        cruise_layout = QGridLayout(cruise_group)
        cruise_help_text = (
            "크루징은 장비에 미리 구성된 내부 경로를 반복 호출합니다. "
            "제공 사양은 8개 경로를 설명하지만 경로 편집 명령의 "
            "번호·체류시간 매핑은 명령표에 충분히 정의되어 있지 않습니다."
        )
        cruise_group.setToolTip(cruise_help_text)
        self.cruise_track = QSpinBox()
        self.cruise_track.setRange(1, 8)
        cruise_start = QPushButton("경로 시작")
        cruise_start.setObjectName("primary")
        cruise_start.clicked.connect(self._start_auto_cruise)
        cruise_stop = QPushButton("크루징 정지(STOP)")
        cruise_stop.clicked.connect(self._emergency_stop)
        cruise_layout.addWidget(QLabel("경로 1~8"), 0, 0)
        cruise_layout.addWidget(self.cruise_track, 0, 1)
        cruise_layout.addWidget(cruise_start, 0, 2)
        cruise_layout.addWidget(QLabel("이동 속도"), 1, 0)
        cruise_layout.addWidget(QLabel("최고속도 고정"), 1, 1, 1, 2)
        cruise_layout.addWidget(cruise_stop, 2, 0, 1, 3)
        cruise_layout.addWidget(self._help_label(cruise_help_text), 3, 0, 1, 3)
        layout.addWidget(cruise_group)

        home_group = QGroupBox("Auto Home")
        home_layout = QGridLayout(home_group)
        home_help_text = (
            "Auto Home은 일정 시간 조작이 없을 때 지정 동작으로 복귀하는 Guard 기능입니다. "
            "전원 인가 직후의 보정용 움직임과는 별개이므로 여기서 OFF해도 부팅 보정은 남을 수 있습니다."
        )
        home_group.setToolTip(home_help_text)
        for column, (text, command_factory) in enumerate(
            (
                ("기능 ON", lambda: auto_home(self._address(), True)),
                ("기능 OFF", lambda: auto_home(self._address(), False)),
                ("복귀 후 Cruise 1", lambda: home_then_cruise1(self._address())),
            )
        ):
            button = QPushButton(text)
            button.clicked.connect(
                lambda _=False, factory=command_factory: self._send(factory())
            )
            home_layout.addWidget(button, 0, column)
        note = self._help_label(
            home_help_text
            + " Auto Home 대기시간의 단위·값 매핑도 불명확해 전용 입력에서는 제외했습니다."
        )
        home_layout.addWidget(note, 1, 0, 1, 4)
        layout.addWidget(home_group)
        layout.addStretch()
        return self._scrollable(page)


    def _recipe_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        recipe_group = QGroupBox("레시피")
        recipe_layout = QGridLayout(recipe_group)
        self.recipe_combo = QComboBox()
        self.recipe_name = QLineEdit()
        self.recipe_description = QLineEdit()
        new_recipe = QPushButton("새 레시피")
        new_recipe.clicked.connect(self._new_recipe)
        save_recipe = QPushButton("레시피 저장")
        save_recipe.setObjectName("primary")
        save_recipe.clicked.connect(self._save_recipe)
        delete_recipe = QPushButton("레시피 삭제")
        delete_recipe.clicked.connect(self._delete_recipe)
        self.recipe_combo.currentIndexChanged.connect(self._recipe_selection_changed)
        recipe_layout.addWidget(QLabel("선택"), 0, 0)
        recipe_layout.addWidget(self.recipe_combo, 0, 1, 1, 2)
        recipe_layout.addWidget(new_recipe, 0, 3)
        recipe_layout.addWidget(QLabel("이름"), 1, 0)
        recipe_layout.addWidget(self.recipe_name, 1, 1)
        recipe_layout.addWidget(QLabel("설명"), 1, 2)
        recipe_layout.addWidget(self.recipe_description, 1, 3)
        recipe_layout.addWidget(save_recipe, 2, 2)
        recipe_layout.addWidget(delete_recipe, 2, 3)
        layout.addWidget(recipe_group)

        point_group = QGroupBox("포인트 테이블")
        point_layout = QVBoxLayout(point_group)
        self.recipe_point_table = QTableWidget(0, 7)
        self.recipe_point_table.setHorizontalHeaderLabels(
            ["#", "이름", "Pan", "Tilt", "대기ms", "사용", "메모"]
        )
        self.recipe_point_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.recipe_point_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.recipe_point_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.recipe_point_table.itemSelectionChanged.connect(self._recipe_point_selection_changed)
        header = self.recipe_point_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        point_layout.addWidget(self.recipe_point_table, 1)

        reorder_row = QHBoxLayout()
        move_up = QPushButton("위로")
        move_up.clicked.connect(lambda: self._move_recipe_point(-1))
        move_down = QPushButton("아래로")
        move_down.clicked.connect(lambda: self._move_recipe_point(1))
        delete_point = QPushButton("삭제")
        delete_point.clicked.connect(self._delete_recipe_point)
        goto_point = QPushButton("지령위치 이동")
        goto_point.setObjectName("primary")
        goto_point.clicked.connect(self._goto_recipe_point)
        for button in (move_up, move_down, delete_point, goto_point):
            reorder_row.addWidget(button)
        point_layout.addLayout(reorder_row)
        layout.addWidget(point_group, 1)

        editor_group = QGroupBox("포인트 편집")
        editor_layout = QGridLayout(editor_group)
        self.recipe_point_name = QLineEdit()
        self.recipe_point_pan = QDoubleSpinBox()
        self.recipe_point_pan.setRange(0.0, 359.99)
        self.recipe_point_pan.setDecimals(2)
        self.recipe_point_pan.setSuffix("°")
        self.recipe_point_tilt = QDoubleSpinBox()
        self.recipe_point_tilt.setRange(-60.0, 60.0)
        self.recipe_point_tilt.setDecimals(2)
        self.recipe_point_tilt.setSuffix("°")
        self.recipe_point_dwell = QSpinBox()
        self.recipe_point_dwell.setRange(0, 3_600_000)
        self.recipe_point_dwell.setSuffix(" ms")
        self.recipe_point_enabled = QCheckBox("사용")
        self.recipe_point_enabled.setChecked(True)
        self.recipe_point_note = QLineEdit()
        new_point = QPushButton("새 포인트")
        new_point.clicked.connect(self._new_recipe_point)
        capture_point = QPushButton("현재 위치 가져오기")
        capture_point.clicked.connect(self._capture_current_recipe_point)
        save_point = QPushButton("포인트 저장")
        save_point.setObjectName("primary")
        save_point.clicked.connect(self._save_recipe_point)
        editor_layout.addWidget(QLabel("이름"), 0, 0)
        editor_layout.addWidget(self.recipe_point_name, 0, 1)
        editor_layout.addWidget(QLabel("Pan"), 0, 2)
        editor_layout.addWidget(self.recipe_point_pan, 0, 3)
        editor_layout.addWidget(QLabel("Tilt"), 1, 0)
        editor_layout.addWidget(self.recipe_point_tilt, 1, 1)
        editor_layout.addWidget(QLabel("이동 속도"), 1, 2)
        editor_layout.addWidget(QLabel("최고속도 고정"), 1, 3)
        editor_layout.addWidget(QLabel("대기"), 2, 0)
        editor_layout.addWidget(self.recipe_point_dwell, 2, 1)
        editor_layout.addWidget(self.recipe_point_enabled, 2, 2)
        editor_layout.addWidget(self.recipe_point_note, 2, 3)
        editor_layout.addWidget(new_point, 3, 0)
        editor_layout.addWidget(capture_point, 3, 1)
        editor_layout.addWidget(save_point, 3, 2, 1, 2)
        layout.addWidget(editor_group)

        layout.addWidget(
            self._help_label(
                "포인트는 장비 프리셋에 저장하지 않고 PC/리눅스의 레시피 테이블에 저장합니다. "
                "이동은 제조사 절대좌표 지령위치 명령으로 실행합니다."
            )
        )
        self._refresh_recipe_combo()
        return page

    def _refresh_recipe_combo(self, select_id: str | None = None) -> None:
        if not hasattr(self, "recipe_combo"):
            return
        current = select_id or self.recipe_combo.currentData()
        recipes = self.recipe_store.list_recipes()
        self.recipe_combo.blockSignals(True)
        self.recipe_combo.clear()
        for recipe in recipes:
            self.recipe_combo.addItem(str(recipe["name"]), str(recipe["id"]))
        index = self.recipe_combo.findData(current)
        if index < 0 and self.recipe_combo.count() > 0:
            index = 0
        if index >= 0:
            self.recipe_combo.setCurrentIndex(index)
        self.recipe_combo.blockSignals(False)
        self._recipe_selection_changed()

    def _selected_recipe_id(self) -> str:
        recipe_id = self.recipe_combo.currentData() if hasattr(self, "recipe_combo") else None
        if isinstance(recipe_id, str) and recipe_id:
            return recipe_id
        recipes = self.recipe_store.list_recipes()
        if not recipes:
            recipe = self.recipe_store.upsert_recipe(name="Default")
            return recipe.id
        return str(recipes[0]["id"])

    def _selected_recipe(self):
        try:
            return self.recipe_store.get_recipe(self._selected_recipe_id())
        except KeyError:
            self._refresh_recipe_combo()
            return self.recipe_store.get_recipe()

    def _recipe_selection_changed(self) -> None:
        if not hasattr(self, "recipe_name"):
            return
        recipe = self._selected_recipe()
        self.recipe_name.setText(recipe.name)
        self.recipe_description.setText(recipe.description)
        self.recipe_edit_point_id = ""
        self._refresh_recipe_points()

    def _refresh_recipe_points(self, select_id: str | None = None) -> None:
        if not hasattr(self, "recipe_point_table"):
            return
        recipe = self._selected_recipe()
        selected = select_id or self._selected_recipe_point_id()
        self.recipe_point_table.blockSignals(True)
        self.recipe_point_table.setRowCount(len(recipe.points))
        for row, point in enumerate(recipe.points):
            values = [
                str(point.order),
                point.name,
                f"{point.pan:.2f}",
                f"{point.tilt:+.2f}",
                str(point.dwell_ms),
                "ON" if point.enabled else "OFF",
                point.note,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, point.id)
                if not point.enabled:
                    item.setForeground(QBrush(QColor("#8a93a3")))
                self.recipe_point_table.setItem(row, column, item)
        self.recipe_point_table.blockSignals(False)
        target_row = -1
        if selected:
            for row, point in enumerate(recipe.points):
                if point.id == selected:
                    target_row = row
                    break
        if target_row < 0 and recipe.points:
            target_row = 0
        if target_row >= 0:
            self.recipe_point_table.selectRow(target_row)
        else:
            self._new_recipe_point()

    def _selected_recipe_point_id(self) -> str:
        if not hasattr(self, "recipe_point_table"):
            return ""
        rows = self.recipe_point_table.selectionModel().selectedRows()
        if not rows:
            return ""
        item = self.recipe_point_table.item(rows[0].row(), 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else ""
        return str(value or "")

    def _selected_recipe_point(self) -> RecipePoint | None:
        point_id = self._selected_recipe_point_id()
        if not point_id:
            return None
        try:
            return self.recipe_store.get_point(self._selected_recipe_id(), point_id)
        except KeyError:
            return None

    def _recipe_point_selection_changed(self) -> None:
        point = self._selected_recipe_point()
        if point is None:
            return
        self.recipe_edit_point_id = point.id
        self.recipe_point_name.setText(point.name)
        self.recipe_point_pan.setValue(point.pan)
        self.recipe_point_tilt.setValue(point.tilt)
        self.recipe_point_dwell.setValue(point.dwell_ms)
        self.recipe_point_enabled.setChecked(point.enabled)
        self.recipe_point_note.setText(point.note)

    def _new_recipe(self) -> None:
        recipe = self.recipe_store.upsert_recipe(name="Recipe")
        self._refresh_recipe_combo(recipe.id)

    def _save_recipe(self) -> None:
        recipe = self.recipe_store.upsert_recipe(
            recipe_id=self._selected_recipe_id(),
            name=self.recipe_name.text().strip() or "Recipe",
            description=self.recipe_description.text().strip(),
        )
        self._refresh_recipe_combo(recipe.id)
        self.statusBar().showMessage("레시피 저장 완료", 2500)

    def _delete_recipe(self) -> None:
        recipe = self._selected_recipe()
        if QMessageBox.question(self, "레시피 삭제", f"{recipe.name} 레시피를 삭제할까요?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.recipe_store.delete_recipe(recipe.id)
        except ValueError as exc:
            QMessageBox.warning(self, "레시피 삭제", str(exc))
            return
        self._refresh_recipe_combo()

    def _new_recipe_point(self) -> None:
        if not hasattr(self, "recipe_point_name"):
            return
        recipe = self._selected_recipe()
        self.recipe_edit_point_id = ""
        self.recipe_point_name.setText(f"P{len(recipe.points) + 1}")
        self.recipe_point_pan.setValue(self.current_pan if self.current_pan is not None else self.target_pan.value())
        self.recipe_point_tilt.setValue(self.current_tilt if self.current_tilt is not None else self.target_tilt.value())
        self.recipe_point_dwell.setValue(0)
        self.recipe_point_enabled.setChecked(True)
        self.recipe_point_note.clear()

    def _capture_current_recipe_point(self) -> None:
        if self.current_pan is None or self.current_tilt is None:
            self._query_all_positions()
            self.statusBar().showMessage("현재 위치를 먼저 조회합니다. 응답 후 다시 눌러주세요.", 3000)
            return
        self.recipe_point_pan.setValue(self.current_pan)
        self.recipe_point_tilt.setValue(self.current_tilt)

    def _save_recipe_point(self) -> None:
        point = self.recipe_store.upsert_point(
            self._selected_recipe_id(),
            point_id=self.recipe_edit_point_id or None,
            name=self.recipe_point_name.text().strip() or "Point",
            pan=self.recipe_point_pan.value(),
            tilt=self.recipe_point_tilt.value(),
            dwell_ms=self.recipe_point_dwell.value(),
            note=self.recipe_point_note.text().strip(),
            enabled=self.recipe_point_enabled.isChecked(),
        )
        self.recipe_edit_point_id = point.id
        self._refresh_recipe_points(point.id)
        self.statusBar().showMessage("포인트 저장 완료", 2500)

    def _delete_recipe_point(self) -> None:
        point = self._selected_recipe_point()
        if point is None:
            return
        self.recipe_store.delete_point(self._selected_recipe_id(), point.id)
        self.recipe_edit_point_id = ""
        self._refresh_recipe_points()

    def _move_recipe_point(self, delta: int) -> None:
        recipe = self._selected_recipe()
        point_id = self._selected_recipe_point_id()
        ids = [point.id for point in recipe.points]
        if not point_id or point_id not in ids:
            return
        index = ids.index(point_id)
        target = index + delta
        if not 0 <= target < len(ids):
            return
        ids[index], ids[target] = ids[target], ids[index]
        self.recipe_store.reorder_points(recipe.id, ids)
        self._refresh_recipe_points(point_id)

    def _goto_recipe_point(self) -> None:
        point = self._selected_recipe_point()
        if point is None:
            return
        if not point.enabled:
            QMessageBox.information(self, "포인트 이동", "비활성 포인트입니다.")
            return
        self._move_to_recipe_point(point)

    def _move_to_recipe_point(
        self,
        point: RecipePoint,
        *,
        label: str | None = None,
        api_context: dict[str, Any] | None = None,
    ) -> bool:
        if not self.is_connected:
            self.statusBar().showMessage("장비 연결 후 포인트 이동이 가능합니다.", 3000)
            return False
        pan = float(point.pan) % 360.0
        tilt = max(-60.0, min(60.0, float(point.tilt)))
        return self._move_to_target_position(
            pan,
            tilt,
            label=label or f"Recipe point move: {point.name}",
            api_context=api_context,
            cancel_reason="recipe point move",
        )

    def _move_to_target_position(
        self,
        pan: float | None,
        tilt: float | None,
        *,
        label: str,
        api_context: dict[str, Any] | None = None,
        callback: Callable[[], None] | None = None,
        cancel_reason: str = "target move",
    ) -> bool:
        if not self.is_connected:
            self.statusBar().showMessage("Connect the COM port first.", 3000)
            return False
        self.api_jog_watchdog.stop()
        self.api_motion_owner = None
        self._cancel_motion_tracking(cancel_reason)
        delay = self._send_auto_position_speed()
        if delay == 0:
            return False

        def send_targets() -> None:
            self._send_position_targets(
                pan,
                tilt,
                label=label,
                api_context=api_context,
                callback=callback,
            )

        QTimer.singleShot(delay, send_targets)
        return True

    def _send_position_targets(
        self,
        pan: float | None,
        tilt: float | None,
        *,
        label: str,
        api_context: dict[str, Any] | None = None,
        callback: Callable[[], None] | None = None,
    ) -> bool:
        if pan is None and tilt is None:
            return False
        if pan is not None and not self._send(set_pan_position(self._address(), pan)):
            return False
        if tilt is not None and not self._send(set_tilt_position(self._address(), tilt)):
            return False
        self._begin_target_tracking(
            label,
            pan=pan,
            tilt=tilt,
            callback=callback,
            api_context=api_context,
        )
        return True

    def _drawing_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        file_group = QGroupBox("도면 파일")
        file_layout = QGridLayout(file_group)
        load_button = QPushButton("도면 불러오기")
        load_button.setObjectName("primary")
        load_button.clicked.connect(self._open_drawing_file)
        reset_view = QPushButton("뷰 리셋")
        reset_view.clicked.connect(lambda: self.drawing_viewer.reset_view())
        self.drawing_render_mode = QComboBox()
        for label, value in (
            ("쉐이딩", "shaded"),
            ("와이어", "wireframe"),
            ("투명", "xray"),
            ("포인트", "points"),
            ("높이색", "height"),
        ):
            self.drawing_render_mode.addItem(label, value)
        self.drawing_render_mode.currentIndexChanged.connect(
            lambda: self.drawing_viewer.set_render_mode(
                str(self.drawing_render_mode.currentData())
            )
        )
        self.drawing_file_label = QLabel("선택된 도면 없음")
        self.drawing_file_label.setWordWrap(True)
        file_layout.addWidget(load_button, 0, 0)
        file_layout.addWidget(QLabel("렌더 모드"), 0, 1)
        file_layout.addWidget(self.drawing_render_mode, 0, 2)
        file_layout.addWidget(reset_view, 0, 3)
        file_layout.addWidget(self.drawing_file_label, 1, 0, 1, 4)
        layout.addWidget(file_group)

        work_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.drawing_viewer = DrawingViewer()
        self.drawing_viewer.point_picked.connect(self._add_drawing_point)
        work_splitter.addWidget(self.drawing_viewer)

        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(8, 0, 0, 0)

        point_group = QGroupBox("포인트 리스트")
        point_layout = QVBoxLayout(point_group)
        self.drawing_table = QTableWidget(0, 8)
        self.drawing_table.setHorizontalHeaderLabels(
            ["#", "X", "Y", "Z", "구분", "Pan", "Tilt", "프리셋"]
        )
        self.drawing_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.drawing_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.drawing_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.drawing_table.itemSelectionChanged.connect(self._drawing_selection_changed)
        header = self.drawing_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        point_layout.addWidget(self.drawing_table, 1)

        order_row = QHBoxLayout()
        move_up = QPushButton("위로")
        move_up.clicked.connect(lambda: self._move_drawing_point(-1))
        move_down = QPushButton("아래로")
        move_down.clicked.connect(lambda: self._move_drawing_point(1))
        delete_point = QPushButton("삭제")
        delete_point.clicked.connect(self._delete_drawing_point)
        for button in (move_up, move_down, delete_point):
            order_row.addWidget(button)
        point_layout.addLayout(order_row)

        calibration_row = QHBoxLayout()
        toggle_calibration = QPushButton("캘리브레이션 지정")
        toggle_calibration.clicked.connect(self._toggle_drawing_calibration)
        teach_calibration = QPushButton("현재 위치 티칭")
        teach_calibration.clicked.connect(self._teach_drawing_calibration)
        calibration_row.addWidget(toggle_calibration)
        calibration_row.addWidget(teach_calibration)
        point_layout.addLayout(calibration_row)

        action_row = QHBoxLayout()
        solve_button = QPushButton("나머지 자동 계산")
        solve_button.setObjectName("primary")
        solve_button.clicked.connect(self._solve_drawing_points)
        self.drawing_upload_button = QPushButton("1번부터 장비 저장")
        self.drawing_upload_button.clicked.connect(self._upload_drawing_presets)
        action_row.addWidget(solve_button)
        action_row.addWidget(self.drawing_upload_button)
        point_layout.addLayout(action_row)

        side_layout.addWidget(point_group, 1)
        self.drawing_status_label = QLabel("도면을 불러온 뒤 모델 위를 클릭하면 포인트가 추가됩니다.")
        self.drawing_status_label.setWordWrap(True)
        self.drawing_status_label.setStyleSheet(
            "color:#75d8ff; background:#101720; padding:7px; border-radius:4px"
        )
        side_layout.addWidget(self.drawing_status_label)
        work_splitter.addWidget(side)
        work_splitter.setSizes([820, 430])
        work_splitter.setStretchFactor(0, 1)
        layout.addWidget(work_splitter, 1)
        return page

    def _gamepad_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            self._help_label(
                "360 스타일 USB 조이패드를 Windows 게임 컨트롤러로 읽습니다. "
                "왼쪽 스틱은 기울기만큼 Pan/Tilt 속도가 비례하고, 중앙으로 놓으면 STOP을 보냅니다. "
                "게임패드가 빠지거나 제어를 해제해도 STOP을 전송합니다."
            )
        )

        connection_group = QGroupBox("게임패드 연결 및 안전 허용")
        connection_layout = QGridLayout(connection_group)
        self.gamepad_combo = QComboBox()
        self.gamepad_combo.setMinimumWidth(280)
        self.gamepad_refresh_button = QPushButton("게임패드 새로고침")
        self.gamepad_refresh_button.clicked.connect(self._refresh_gamepads)
        self.gamepad_connect_button = QPushButton("게임패드 연결")
        self.gamepad_connect_button.clicked.connect(self._toggle_gamepad_connection)
        self.gamepad_enable = QCheckBox("게임패드로 장비 제어 허용")
        self.gamepad_enable.setEnabled(False)
        self.gamepad_enable.toggled.connect(self._gamepad_enable_changed)
        self.gamepad_rumble_button = QPushButton("진동 테스트")
        self.gamepad_rumble_button.setEnabled(False)
        self.gamepad_rumble_button.clicked.connect(self._test_gamepad_rumble)
        self.gamepad_status = QLabel("연결 대기")
        connection_layout.addWidget(QLabel("장치"), 0, 0)
        connection_layout.addWidget(self.gamepad_combo, 0, 1)
        connection_layout.addWidget(self.gamepad_refresh_button, 0, 2)
        connection_layout.addWidget(self.gamepad_connect_button, 0, 3)
        connection_layout.addWidget(self.gamepad_enable, 1, 0, 1, 2)
        connection_layout.addWidget(self.gamepad_rumble_button, 1, 2)
        connection_layout.addWidget(self.gamepad_status, 1, 3)
        layout.addWidget(connection_group)

        live_group = QGroupBox("실시간 입력 확인 · 축/버튼 번호 찾기")
        live_layout = QFormLayout(live_group)
        self.gamepad_axes_label = QLabel("-")
        self.gamepad_buttons_label = QLabel("-")
        self.gamepad_hat_label = QLabel("-")
        for label in (
            self.gamepad_axes_label,
            self.gamepad_buttons_label,
            self.gamepad_hat_label,
        ):
            label.setFont(QFont("Consolas", 10))
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
        live_layout.addRow("Axes", self.gamepad_axes_label)
        live_layout.addRow("Pressed buttons", self.gamepad_buttons_label)
        live_layout.addRow("D-pad hats", self.gamepad_hat_label)
        layout.addWidget(live_group)

        mapping_group = QGroupBox("기본 키 배치")
        mapping_layout = QVBoxLayout(mapping_group)
        mapping_text = QTextBrowser()
        mapping_text.setMaximumHeight(235)
        mapping_text.setHtml(
            "<table cellspacing='6'>"
            "<tr><td><b>왼쪽 스틱</b></td><td>Pan/Tilt 비례속도 Jog</td></tr>"
            "<tr><td><b>D-pad ←/→</b></td><td>Pan 최대속도 -/+</td></tr>"
            "<tr><td><b>D-pad ↓/↑</b></td><td>Tilt 최대속도 -/+</td></tr>"
            "<tr><td><b>A</b></td><td>즉시 STOP</td></tr>"
            "<tr><td><b>B</b></td><td>현재 Pan/Tilt 조회</td></tr>"
            "<tr><td><b>X</b></td><td>미사용</td></tr>"
            "<tr><td><b>Y</b></td><td>레이저 펄스(레이저 ARM 필요)</td></tr>"
            "<tr><td><b>LB/RB</b></td><td>미사용</td></tr>"
            "<tr><td><b>Back</b></td><td>레이저 OFF</td></tr>"
            "<tr><td><b>Start</b></td><td>위치 자동 모니터링 전환</td></tr>"
            "</table>"
        )
        mapping_layout.addWidget(mapping_text)
        mapping_layout.addWidget(
            self._help_label(
                "Windows/Linux 모두 SDL 표준 컨트롤러로 인식되면 A/B/X/Y, D-pad, LB/RB가 "
                "자동 보정됩니다. raw 매핑으로 표시되는 특이 패드는 위 실시간 입력에서 움직인 "
                "축과 눌린 버튼 번호를 확인한 뒤 pt503_tester/gamepad_mapping.py 상단의 "
                "숫자만 수정하세요."
            )
        )
        layout.addWidget(mapping_group)
        layout.addStretch()

        if not self.gamepad_manager.available:
            self.gamepad_status.setText("pygame 미설치 · requirements.txt 재설치 필요")
            self.gamepad_connect_button.setEnabled(False)
        return page

    def _laser_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        info = self._help_label(
            "이 탭은 Pelco-D AUX 스위치 명령을 레이저 ON/OFF 용도로 사용합니다. "
            "사진의 PT503 배선만으로 AUX 전원 출력이 확인되지는 않습니다. 반드시 외부 릴레이·MOSFET "
            "또는 제조사가 확인한 AUX 인터페이스로 레이저 전원을 스위칭하고, AUX 선에 레이저를 직접 연결하지 마세요."
        )
        layout.addWidget(info)

        laser_group = QGroupBox("레이저 안전 제어 (Pelco-D AUX 매핑)")
        laser_layout = QGridLayout(laser_group)
        self.laser_arm = QCheckBox("레이저 출력 허용(ARM)")
        self.laser_arm.toggled.connect(self._laser_arm_changed)
        self.aux_number = QSpinBox()
        self.aux_number.setRange(1, 255)
        self.aux_number.setValue(1)
        self.laser_pulse_ms = QSpinBox()
        self.laser_pulse_ms.setRange(50, 60_000)
        self.laser_pulse_ms.setSingleStep(50)
        self.laser_pulse_ms.setValue(500)
        self.laser_pulse_ms.setSuffix(" ms")
        self.laser_on_button = QPushButton("레이저 ON")
        self.laser_on_button.setObjectName("danger")
        self.laser_on_button.clicked.connect(self._laser_on)
        self.laser_on_button.setEnabled(False)
        self.laser_off_button = QPushButton("레이저 OFF")
        self.laser_off_button.clicked.connect(self._laser_off)
        self.laser_pulse_button = QPushButton("설정 시간만 펄스")
        self.laser_pulse_button.clicked.connect(self._laser_pulse)
        self.laser_pulse_button.setEnabled(False)
        self.laser_state_label = QLabel("출력 상태: OFF (앱 추정)")
        self.laser_state_label.setStyleSheet("color:#75e09a; font-weight:700")

        laser_layout.addWidget(self.laser_arm, 0, 0, 1, 3)
        laser_layout.addWidget(QLabel("AUX 스위치 번호"), 1, 0)
        laser_layout.addWidget(self.aux_number, 1, 1)
        laser_layout.addWidget(QLabel("펄스 시간"), 2, 0)
        laser_layout.addWidget(self.laser_pulse_ms, 2, 1)
        laser_layout.addWidget(self.laser_on_button, 3, 0)
        laser_layout.addWidget(self.laser_off_button, 3, 1)
        laser_layout.addWidget(self.laser_pulse_button, 3, 2)
        laser_layout.addWidget(self.laser_state_label, 4, 0, 1, 3)
        laser_layout.addWidget(
            self._help_label(
                "ON은 FF Addr 00 09 00 [채널], OFF는 FF Addr 00 0B 00 [채널]을 보냅니다. "
                "표시 상태는 장비 출력 피드백이 아니라 앱이 마지막으로 보낸 명령을 나타냅니다. "
                "연결 해제와 종료 시 OFF를 먼저 요청합니다."
            ),
            5,
            0,
            1,
            3,
        )
        layout.addWidget(laser_group)
        layout.addStretch()
        return page

    def _api_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            self._help_label(
                "Main APP은 TCP 클라이언트, 이 PySide6 프로그램은 로컬 제어서버입니다. "
                "PySide6만 COM 포트를 열고 Main APP은 줄바꿈으로 구분한 UTF-8 JSON 명령을 보냅니다. "
                "요청 응답 외에 위치·ACK·이동 완료 이벤트도 같은 연결로 수신합니다."
            )
        )

        server_group = QGroupBox("로컬 제어서버 · 같은 PC에서만 접속")
        server_layout = QGridLayout(server_group)
        self.api_port = QSpinBox()
        self.api_port.setRange(1024, 65_535)
        self.api_port.setValue(DEFAULT_PORT)
        self.api_start_button = QPushButton("서버 시작")
        self.api_start_button.setObjectName("primary")
        self.api_start_button.clicked.connect(self._toggle_api_server)
        self.api_auto_start = QCheckBox("프로그램 실행 시 자동 시작")
        self.api_auto_start.setChecked(True)
        self.api_status = QLabel("중지됨")
        self.api_clients = QLabel("접속 클라이언트: 0")
        server_layout.addWidget(QLabel("주소"), 0, 0)
        server_layout.addWidget(QLabel("127.0.0.1"), 0, 1)
        server_layout.addWidget(QLabel("포트"), 0, 2)
        server_layout.addWidget(self.api_port, 0, 3)
        server_layout.addWidget(self.api_start_button, 0, 4)
        server_layout.addWidget(self.api_auto_start, 1, 0, 1, 2)
        server_layout.addWidget(self.api_status, 1, 2, 1, 2)
        server_layout.addWidget(self.api_clients, 1, 4)
        server_layout.addWidget(
            self._help_label(
                "127.0.0.1에만 바인딩하므로 외부 네트워크에서는 접근할 수 없습니다. "
                "Main APP도 같은 Windows PC에서 127.0.0.1과 선택 포트로 접속하세요."
            ),
            2,
            0,
            1,
            5,
        )
        layout.addWidget(server_group)

        format_group = QGroupBox("명령/응답 형식")
        format_layout = QVBoxLayout(format_group)
        example = QTextBrowser()
        example.setMaximumHeight(245)
        example.setFont(QFont("Consolas", 9))
        example.setPlainText(
            '요청  {"id":"m1","command":"motion.absolute",'
            '"params":{"pan":90.0,"tilt":5.0}}\n'
            '응답  {"id":"m1","ok":true,"result":{"accepted":true}}\n'
            '이벤트 {"event":"motion.completed","data":{"request_id":"m1",...}}\n\n'
            'Jog   {"id":"j1","command":"motion.jog",'
            '"params":{"pan":"right","tilt":"stop","pan_level":3,"duration_ms":5000}}\n'
            'STOP  {"id":"s1","command":"motion.stop","params":{}}\n'
            '상태  {"id":"q1","command":"system.status","params":{}}'
        )
        format_layout.addWidget(example)
        format_layout.addWidget(
            self._help_label(
                "motion.jog는 연속 수동운전입니다. 같은 방향/속도는 서버가 약 5초마다 "
                "Pelco-D runaway 보호용으로만 재전송하며, motion.stop 또는 제어 종료 시 정지합니다. 전체 명령은 docs/EXTERNAL_API.md를 확인하세요."
            )
        )
        layout.addWidget(format_group)

        feature_group = QGroupBox("외부 APP에서 사용할 수 있는 기능")
        feature_layout = QVBoxLayout(feature_group)
        feature_layout.addWidget(
            QLabel(
                "통신 연결·상태 / Jog·STOP·절대좌표 / 위치 / 레시피 포인트 / 라인스캔 / "
                "크루징 / Auto Home / 레이저 / 장치조회 / 유지보수 / 원시 Pelco-D"
            )
        )
        feature_layout.addWidget(
            self._help_label(
                "유지보수와 raw.send는 params.confirm=true가 있어야 실행됩니다. "
                "레이저 ON/펄스는 먼저 laser.arm을 켜야 합니다. API 요청과 응답은 오른쪽 통신 로그에 기록됩니다."
            )
        )
        layout.addWidget(feature_group)
        layout.addStretch()
        return page

    def _advanced_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        query_group = QGroupBox("개별 조회 명령")
        query_layout = QHBoxLayout(query_group)
        pan_query = QPushButton("Pan 조회")
        pan_query.clicked.connect(lambda: self._send(query_pan(self._address())))
        tilt_query = QPushButton("Tilt 조회")
        tilt_query.clicked.connect(lambda: self._send(query_tilt(self._address())))
        focus_query = QPushButton("Focus Raw 조회")
        focus_query.clicked.connect(lambda: self._send(query_focus(self._address())))
        device_query = QPushButton("장치 유형 조회")
        device_query.clicked.connect(
            lambda: self._send(query_device_type(self._address()))
        )
        for button in (pan_query, tilt_query, focus_query, device_query):
            query_layout.addWidget(button)
        query_group.setToolTip(
            "각 조회를 한 번씩 송신해 응답 프레임과 체크섬을 확인합니다. "
            "Focus 명령은 제조사 명령표, 장치 유형은 표준 Pelco-D 명령입니다."
        )
        layout.addWidget(query_group)

        raw_group = QGroupBox("원시 Pelco-D HEX 송신")
        raw_layout = QVBoxLayout(raw_group)
        raw_help = QLabel(
            "7바이트 완성 프레임 또는 체크섬을 제외한 6바이트를 입력하세요. "
            "6바이트 입력 시 체크섬을 자동 계산합니다. 예: FF 01 00 51 00 00"
        )
        raw_help.setWordWrap(True)
        self.raw_hex = QLineEdit("FF 01 00 51 00 00")
        self.raw_hex.setFont(QFont("Consolas", 11))
        raw_send = QPushButton("검증 후 송신")
        raw_send.clicked.connect(self._send_raw)
        raw_layout.addWidget(raw_help)
        raw_layout.addWidget(self.raw_hex)
        raw_layout.addWidget(raw_send)
        layout.addWidget(raw_group)

        maintenance = QGroupBox(
            "유지보수 · 실행 즉시 장비가 움직이거나 재시작될 수 있음"
        )
        maintenance_layout = QGridLayout(maintenance)
        self_check_button = QPushButton("Self-check")
        self_check_button.setObjectName("danger")
        self_check_button.clicked.connect(self._self_check_confirmed)
        restart_button = QPushButton("원격 재시작")
        restart_button.setObjectName("danger")
        restart_button.clicked.connect(self._restart_confirmed)
        factory_button = QPushButton("공장 초기화")
        factory_button.setObjectName("danger")
        factory_button.clicked.connect(self._factory_default_confirmed)
        maintenance_layout.addWidget(self_check_button, 0, 0)
        maintenance_layout.addWidget(restart_button, 0, 1)
        maintenance_layout.addWidget(factory_button, 0, 2)
        startup_on = QPushButton("전원 인가 Self-check ON")
        startup_on.setObjectName("danger")
        startup_on.clicked.connect(lambda: self._set_power_on_self_check(True))
        startup_off = QPushButton("전원 인가 Self-check OFF")
        startup_off.setObjectName("danger")
        startup_off.clicked.connect(lambda: self._set_power_on_self_check(False))
        maintenance_layout.addWidget(startup_on, 1, 0)
        maintenance_layout.addWidget(startup_off, 1, 1)
        maintenance_layout.addWidget(
            self._help_label(
                "Auto Home과 별개입니다. 명령표의 0x03/0x05 + 0x77은 'customized'로 표시되어 "
                "PT503 펌웨어가 지원하지 않을 수 있습니다. 사양서의 부팅 보정(Start Movement)이 "
                "고정 동작이면 OFF 후에도 움직이며, 설정 조회 명령도 없어 재부팅 시험으로만 확인할 수 있습니다."
            ),
            2,
            0,
            1,
            3,
        )
        layout.addWidget(maintenance)

        notes = QTextBrowser()
        notes.setHtml(
            "<h3>시험 순서 권장</h3>"
            "<ol><li>Pan 위치 조회로 통신 확인</li><li>낮은 속도로 짧게 Jog</li>"
            "<li>좌표 방향 확인 후 절대위치 이동</li><li>레시피 포인트와 스캔 시험</li>"
            "<li>크루징·Auto Home·레이저·유지보수 기능 시험</li></ol>"
            "<p><b>PT503/PT510 공통 명령셋</b> 기준입니다. 제조사 확장 명령은 로그에서 "
            "TX 프레임과 응답을 반드시 확인하세요.</p>"
            "<p><b>응답 해석:</b> 일반 4바이트 응답은 명령 수신 ACK이며 이동 완료가 아닙니다. "
            "위치 이동 완료는 Pan/Tilt 조회값으로 앱이 추정합니다.</p>"
            "<p><b>전용 UI에서 제외:</b> Customized 상대각 명령은 부호·배율 정의가 없고, "
            "크루즈 체류시간/Auto Home 시간은 값 매핑이 불명확합니다. 제조사 확인 전에는 "
            "원시 HEX로만 제한적으로 시험하세요.</p>"
        )
        layout.addWidget(notes, 1)
        return page

    def _log_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        header = QHBoxLayout()
        title = QLabel("통신 내역 · 모든 값은 실제 송수신 바이트")
        title.setStyleSheet("font-size:12pt; font-weight:700; color:#9fc6ff")
        self.auto_scroll = QCheckBox("자동 스크롤")
        self.auto_scroll.setChecked(True)
        save_button = QPushButton("CSV 저장")
        save_button.clicked.connect(self._export_csv)
        folder_button = QPushButton("자동 로그 폴더")
        folder_button.clicked.connect(self._open_log_folder)
        clear_button = QPushButton("화면 지우기")
        clear_button.clicked.connect(self._clear_log_view)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.auto_scroll)
        header.addWidget(save_button)
        header.addWidget(folder_button)
        header.addWidget(clear_button)
        layout.addLayout(header)

        self.log_table = QTableWidget(0, 6)
        self.log_table.setHorizontalHeaderLabels(
            ["시간", "방향", "HEX 데이터", "해석", "응답 ms", "체크섬"]
        )
        self.log_table.setAlternatingRowColors(True)
        self.log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.log_table.verticalHeader().setVisible(False)
        header_view = self.log_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.log_table, 1)
        return panel

    @staticmethod
    def _large_value(text: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(
            "font-family:Consolas; font-size:22pt; font-weight:700; color:#75d8ff; "
            "background:#0f141b; border:1px solid #344150; border-radius:6px; padding:8px"
        )
        return label

    @staticmethod
    def _speed_spin(maximum: int = 63) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, maximum)
        spin.setValue(min(10, maximum))
        return spin

    @staticmethod
    def _help_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color:#f5c06b; font-weight:400")
        label.setToolTip(text)
        return label

    @staticmethod
    def _scrollable(page: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        area.setStyleSheet("QScrollArea { border: 0; }")
        return area

    def _open_drawing_file(self) -> None:
        start_dir = (
            str(DEFAULT_DRAWING_DIR)
            if DEFAULT_DRAWING_DIR.exists()
            else str(Path(self.drawing_model_path).parent)
            if self.drawing_model_path
            else str(Path.home())
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "도면 파일 열기",
            start_dir,
            "3D mesh/CAD files (*.glb *.gltf *.stp *.step *.obj *.stl *.ply);;2D plot files (*.gl2);;All files (*)",
        )
        if path:
            self._load_drawing_file(path, keep_points=False)

    def _load_drawing_file(self, path: str, *, keep_points: bool) -> bool:
        self._set_drawing_status("도면 로드 중...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            model = load_model_file(path)
        except (OSError, ModelLoadError, ValueError) as exc:
            QMessageBox.critical(self, "도면 파일", f"도면을 불러오지 못했습니다.\n{exc}")
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self.drawing_model_path = str(path)
        if not keep_points:
            self.drawing_points = []
        self.drawing_viewer.set_model(model)
        self.drawing_viewer.set_render_mode(str(self.drawing_render_mode.currentData()))
        self.drawing_viewer.set_points(self.drawing_points)
        warning = f"\n{model.warnings[0]}" if model.warnings else ""
        self.drawing_file_label.setText(
            f"{model.name} - vertices {len(model.vertices):,}, "
            f"faces {len(model.faces):,}{warning}"
        )
        self._set_drawing_status(f"도면 로드 완료: {model.name}")
        self._refresh_drawing_table()
        self._save_drawing_session()
        return True

    def _load_drawing_session(self) -> None:
        path, points = self.drawing_store.load()
        self.drawing_points = points
        self._normalize_drawing_calibration_slots()
        if path and Path(path).exists():
            self.drawing_model_path = path
            self.drawing_file_label.setText(
                f"마지막 도면 저장됨: {Path(path).name} (도면 불러오기 버튼으로 로드)"
            )
            self._refresh_drawing_table()
        elif path:
            self.drawing_model_path = path
            self.drawing_file_label.setText(f"저장된 도면을 찾을 수 없음: {path}")
            self._refresh_drawing_table()
        else:
            self._refresh_drawing_table()

    def _save_drawing_session(self) -> None:
        try:
            self.drawing_store.save(self.drawing_model_path, self.drawing_points)
        except OSError as exc:
            self._set_drawing_status(f"도면 작업 저장 실패: {exc}")

    def _set_drawing_status(self, text: str) -> None:
        if hasattr(self, "drawing_status_label"):
            self.drawing_status_label.setText(text)

    def _add_drawing_point(self, position: object) -> None:
        if not isinstance(position, tuple) or len(position) != 3:
            return
        point = DrawingPoint.create(position)
        self.drawing_points.append(point)
        self._refresh_drawing_table(select_id=point.id)
        self._save_drawing_session()
        self._set_drawing_status(
            f"포인트 {len(self.drawing_points)} 추가: "
            f"X {point.x:.3f}, Y {point.y:.3f}, Z {point.z:.3f}"
        )

    def _selected_drawing_row(self) -> int:
        rows = self.drawing_table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _selected_drawing_point(self) -> DrawingPoint | None:
        row = self._selected_drawing_row()
        if not 0 <= row < len(self.drawing_points):
            return None
        return self.drawing_points[row]

    def _drawing_selection_changed(self) -> None:
        point = self._selected_drawing_point()
        self.drawing_viewer.set_selected_point(point.id if point is not None else "")

    def _refresh_drawing_table(self, select_id: str | None = None) -> None:
        if not hasattr(self, "drawing_table"):
            return
        if select_id is None:
            point = self._selected_drawing_point()
            select_id = (
                point.id if point is not None else self.drawing_viewer.selected_point_id
            )
        self._normalize_drawing_calibration_slots()
        self.drawing_table.blockSignals(True)
        self.drawing_table.setRowCount(len(self.drawing_points))
        for row, point in enumerate(self.drawing_points):
            slot = self._calibration_slot_for(point)
            preset_text = str(row + 1)
            role = "일반"
            if slot is not None:
                role = f"CAL {slot + 1}"
                preset_text = f"{row + 1} / {200 + slot}"
            values = (
                str(row + 1),
                f"{point.x:.3f}",
                f"{point.y:.3f}",
                f"{point.z:.3f}",
                role,
                "-" if point.pan is None else f"{point.pan:.2f}",
                "-" if point.tilt is None else f"{point.tilt:+.2f}",
                preset_text,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column in (0, 4, 5, 6, 7):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if slot is not None:
                    item.setForeground(QBrush(QColor("#ffd166")))
                self.drawing_table.setItem(row, column, item)
        self.drawing_table.blockSignals(False)
        if select_id:
            for row, point in enumerate(self.drawing_points):
                if point.id == select_id:
                    self.drawing_table.selectRow(row)
                    break
        self.drawing_viewer.set_points(self.drawing_points)

    def _move_drawing_point(self, delta: int) -> None:
        row = self._selected_drawing_row()
        target = row + delta
        if (
            not 0 <= row < len(self.drawing_points)
            or not 0 <= target < len(self.drawing_points)
        ):
            return
        self.drawing_points[row], self.drawing_points[target] = (
            self.drawing_points[target],
            self.drawing_points[row],
        )
        selected_id = self.drawing_points[target].id
        self._refresh_drawing_table(select_id=selected_id)
        self._save_drawing_session()

    def _delete_drawing_point(self) -> None:
        row = self._selected_drawing_row()
        if not 0 <= row < len(self.drawing_points):
            return
        removed = self.drawing_points.pop(row)
        self._refresh_drawing_table()
        self._save_drawing_session()
        self._set_drawing_status(f"포인트 삭제: {removed.label or row + 1}")

    def _toggle_drawing_calibration(self) -> None:
        point = self._selected_drawing_point()
        if point is None:
            self._set_drawing_status("캘리브레이션으로 지정할 포인트를 선택하세요.")
            return
        if point.calibration:
            point.calibration = False
            point.calibration_slot = None
            self._set_drawing_status("선택 포인트를 일반 포인트로 변경했습니다.")
        else:
            slot = self._available_drawing_calibration_slot()
            if slot is None:
                QMessageBox.warning(
                    self,
                    "캘리브레이션",
                    "캘리브레이션 포인트는 최대 6개까지 지정할 수 있습니다.",
                )
                return
            point.calibration = True
            point.calibration_slot = slot
            self._set_drawing_status(
                f"캘리브레이션 포인트 지정: 장비 프리셋 {200 + slot}"
            )
        self._refresh_drawing_table(select_id=point.id)
        self._save_drawing_session()

    def _normalize_drawing_calibration_slots(self) -> None:
        used: set[int] = set()
        for point in self.drawing_points:
            if not point.calibration:
                point.calibration_slot = None
                continue
            slot = point.calibration_slot
            if slot is None or slot in used or not 0 <= slot <= 5:
                slot = next(
                    (candidate for candidate in range(6) if candidate not in used),
                    None,
                )
            if slot is None:
                point.calibration = False
                point.calibration_slot = None
                continue
            point.calibration_slot = slot
            used.add(slot)

    def _available_drawing_calibration_slot(self) -> int | None:
        used = {
            int(point.calibration_slot)
            for point in self.drawing_points
            if point.calibration and point.calibration_slot is not None
        }
        return next((slot for slot in range(6) if slot not in used), None)

    @staticmethod
    def _calibration_slot_for(point: DrawingPoint) -> int | None:
        if not point.calibration or point.calibration_slot is None:
            return None
        return point.calibration_slot

    def _teach_drawing_calibration(self) -> None:
        point = self._selected_drawing_point()
        if point is None:
            self._set_drawing_status("티칭할 포인트를 먼저 선택하세요.")
            return
        if not point.calibration:
            slot = self._available_drawing_calibration_slot()
            if slot is None:
                QMessageBox.warning(
                    self,
                    "캘리브레이션",
                    "캘리브레이션 포인트는 최대 6개까지 지정할 수 있습니다.",
                )
                return
            point.calibration = True
            point.calibration_slot = slot
        slot = self._calibration_slot_for(point)
        if slot is None:
            return
        if not self.is_connected:
            self.statusBar().showMessage("먼저 COM 포트를 연결하세요.", 3000)
            return
        self.drawing_teach_capture = {
            "address": self._address(),
            "point_id": point.id,
            "slot": slot,
            "pan": None,
            "tilt": None,
        }
        capture = self.drawing_teach_capture
        self._set_drawing_status(
            f"포인트 티칭 중: 현재 Pan/Tilt를 읽고 프리셋 {200 + slot}에 저장합니다."
        )
        self._send(query_pan(self._address()))
        QTimer.singleShot(180, lambda: self._send(query_tilt(self._address())))
        QTimer.singleShot(3000, lambda: self._expire_drawing_teach_capture(capture))
        self._refresh_drawing_table(select_id=point.id)
        self._save_drawing_session()

    def _expire_drawing_teach_capture(self, capture: dict[str, object]) -> None:
        if self.drawing_teach_capture is capture:
            self.drawing_teach_capture = None
            self._set_drawing_status("티칭 실패: Pan/Tilt 위치 응답 타임아웃")
            self._append_event(
                "TIMEOUT",
                "",
                "도면 캘리브레이션 티칭 중 Pan/Tilt 위치 응답이 없어 취소했습니다.",
            )

    def _capture_drawing_axis(
        self, axis: str, value: float, response_address: int | None
    ) -> None:
        capture = self.drawing_teach_capture
        if capture is None or response_address != int(capture["address"]):
            return
        capture[axis] = value
        self._finish_drawing_teach_if_ready()

    def _finish_drawing_teach_if_ready(self) -> None:
        capture = self.drawing_teach_capture
        if capture is None or capture["pan"] is None or capture["tilt"] is None:
            return
        self.drawing_teach_capture = None
        point_id = str(capture["point_id"])
        point = next((item for item in self.drawing_points if item.id == point_id), None)
        if point is None:
            return
        address = int(capture["address"])
        slot = int(capture["slot"])
        pan = round(float(capture["pan"]) % 360.0, 2)
        tilt = round(float(capture["tilt"]), 2)
        point.calibration = True
        point.calibration_slot = slot
        point.pan = pan
        point.tilt = tilt
        preset = 200 + slot
        if self._send(set_preset(address, preset)):
            self.preset_store.set(address, preset, pan, tilt)
            self._append_event(
                "PRESET",
                "",
                f"도면 CAL {slot + 1} 저장: 프리셋 {preset}, "
                f"Pan {pan:.2f}, Tilt {tilt:+.2f}",
            )
        self._refresh_drawing_table(select_id=point.id)
        self._save_drawing_session()
        self._set_drawing_status(
            f"포인트 티칭 완료: 프리셋 {preset}, Pan {pan:.2f}, Tilt {tilt:+.2f}"
        )

    def _solve_drawing_points(self) -> None:
        if not self.drawing_points:
            self._set_drawing_status("계산할 도면 포인트가 없습니다.")
            return
        try:
            fit = fit_affine_calibration(self.drawing_points)
        except CalibrationError as exc:
            QMessageBox.warning(self, "캘리브레이션", str(exc))
            return
        updated = 0
        for point in self.drawing_points:
            if point.calibration and point.has_pan_tilt:
                continue
            point.pan, point.tilt = fit.predict(point)
            updated += 1
        self._refresh_drawing_table()
        self._save_drawing_session()
        self._set_drawing_status(
            f"자동 계산 완료: {updated}개 업데이트, "
            f"보정 RMS Pan {fit.rms_pan_error:.2f}deg / "
            f"Tilt {fit.rms_tilt_error:.2f}deg"
        )

    def _upload_drawing_presets(self) -> None:
        if not self.is_connected:
            self.statusBar().showMessage("먼저 COM 포트를 연결하세요.", 3000)
            return
        if self.drawing_upload_active:
            self._abort_drawing_upload("사용자 취소")
            return
        if not self.drawing_points:
            self._set_drawing_status("저장할 포인트가 없습니다.")
            return
        if len(self.drawing_points) > 199:
            QMessageBox.warning(
                self,
                "프리셋 저장",
                "200번부터는 캘리브레이션용으로 예약되어 있어 "
                "일반 포인트는 199개 이하만 저장합니다.",
            )
            return
        missing = [
            str(index)
            for index, point in enumerate(self.drawing_points, start=1)
            if not point.has_pan_tilt
        ]
        if missing:
            QMessageBox.warning(
                self,
                "프리셋 저장",
                "Pan/Tilt가 없는 포인트가 있습니다: " + ", ".join(missing[:20]),
            )
            return
        if not self._confirm_motion(
            f"모터가 포인트 {len(self.drawing_points)}개를 순서대로 이동하며 "
            "프리셋 1번부터 저장합니다. 진행할까요?"
        ):
            return
        self.drawing_upload_active = True
        self.drawing_upload_address = self._address()
        self.drawing_upload_queue = list(enumerate(self.drawing_points, start=1))
        self.drawing_upload_button.setText("저장 중지")
        self._append_event(
            "PRESET",
            "",
            f"도면 포인트 프리셋 저장 시작: {len(self.drawing_upload_queue)}개",
        )
        self._upload_next_drawing_preset()

    def _abort_drawing_upload(self, reason: str) -> None:
        if not self.drawing_upload_active:
            return
        self.drawing_upload_queue = []
        self.drawing_upload_active = False
        self.drawing_upload_button.setText("1번부터 장비 저장")
        self._set_drawing_status(f"도면 프리셋 저장 중지: {reason}")
        self._append_event("CANCEL", "", f"도면 프리셋 저장 중지: {reason}")

    def _upload_next_drawing_preset(self) -> None:
        if not self.drawing_upload_active:
            return
        if not self.drawing_upload_queue:
            self.drawing_upload_active = False
            self.drawing_upload_button.setText("1번부터 장비 저장")
            self._set_drawing_status("도면 포인트 프리셋 저장 완료")
            self._append_event("PRESET", "", "도면 포인트 프리셋 저장 완료")
            self._update_preset_display()
            return
        preset, point = self.drawing_upload_queue.pop(0)
        address = self.drawing_upload_address
        pan = float(point.pan)
        tilt = float(point.tilt)
        self._set_drawing_status(
            f"프리셋 {preset} 저장 중: Pan {pan:.2f}, Tilt {tilt:+.2f}"
        )
        delay = self._send_auto_position_speed()

        def send_pan() -> None:
            if not self._send(set_pan_position(address, pan)):
                self._abort_drawing_upload("Pan 이동 명령 실패")

        def save_after_arrival() -> None:
            if not self._send(set_preset(address, preset)):
                self._abort_drawing_upload(f"프리셋 {preset} 저장 명령 실패")
                return
            self.preset_store.set(address, preset, pan, tilt)
            self._append_event(
                "PRESET",
                "",
                f"도면 포인트 {preset} 저장: Pan {pan:.2f}, Tilt {tilt:+.2f}",
            )
            self._update_preset_display()
            QTimer.singleShot(250, self._upload_next_drawing_preset)

        def send_tilt_and_track() -> None:
            if not self._send(set_tilt_position(address, tilt)):
                self._abort_drawing_upload("Tilt 이동 명령 실패")
                return
            self._begin_target_tracking(
                f"도면 포인트 {preset} 이동",
                pan=pan,
                tilt=tilt,
                callback=save_after_arrival,
            )

        QTimer.singleShot(delay, send_pan)
        QTimer.singleShot(delay + 350, send_tilt_and_track)

    def _configure_spin_boxes(self) -> None:
        """Keep native up/down controls usable even while disconnected."""

        for spin in self.findChildren(QAbstractSpinBox):
            spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
            spin.setAccelerated(True)
            spin.setKeyboardTracking(False)

    def _jog_button(
        self, text: str, pan: PanDirection, tilt: TiltDirection
    ) -> QPushButton:
        button = QPushButton(text)
        button.pressed.connect(lambda: self._start_jog(pan, tilt))
        button.released.connect(self._end_jog)
        return button

    # ---------- Worker and serial ----------
    def _connect_worker(self) -> None:
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.error.connect(self._on_error)
        self.worker.status.connect(
            lambda text: self.statusBar().showMessage(text, 3000)
        )
        self.worker.tx_frame.connect(self._on_tx)
        self.worker.rx_blob.connect(self._on_rx_blob)
        self.worker.scan_started.connect(self._on_scan_started)
        self.worker.scan_progress.connect(self._on_scan_progress)
        self.worker.scan_found.connect(self._on_scan_found)
        self.worker.scan_finished.connect(self._on_scan_finished)

    def _connect_gamepad(self) -> None:
        self.gamepad_manager.devices_changed.connect(self._on_gamepad_devices)
        self.gamepad_manager.connected.connect(self._on_gamepad_connected)
        self.gamepad_manager.disconnected.connect(self._on_gamepad_disconnected)
        self.gamepad_manager.state_changed.connect(self._on_gamepad_state)
        self.gamepad_manager.error.connect(
            lambda message: self._append_event("ERROR", "", message)
        )

    def _connect_api(self) -> None:
        self.api_server.running_changed.connect(self._on_api_running_changed)
        self.api_server.client_count_changed.connect(
            lambda count: self.api_clients.setText(f"접속 클라이언트: {count}")
        )
        self.api_server.client_connected.connect(
            lambda client_id, label: self._append_event(
                "API", "", f"{client_id} 접속: {label}"
            )
        )
        self.api_server.client_disconnected.connect(self._on_api_client_disconnected)
        self.api_server.exchange.connect(self._on_api_exchange)

    def _toggle_api_server(self) -> None:
        if self.api_server.is_running:
            self.api_server.stop()
        else:
            self._start_api_server()

    def _start_api_server(self) -> None:
        self.api_server.start(self.api_port.value())

    def _on_api_running_changed(self, running: bool, detail: str) -> None:
        self.api_port.setEnabled(not running)
        self.api_start_button.setText("서버 중지" if running else "서버 시작")
        self.api_status.setText(
            f"실행 중 · {detail}" if running else f"중지 · {detail}"
        )
        self._append_event(
            "API", "", f"외부 APP API 서버 {'시작' if running else '중지'}: {detail}"
        )

    def _on_api_exchange(self, direction: str, client_id: str, payload: str) -> None:
        if direction == "EVENT":
            try:
                if json.loads(payload).get("event") == "position.updated":
                    return
            except (json.JSONDecodeError, AttributeError):
                pass
        self._append_event(direction, "", f"{client_id} · {payload}")

    def _on_api_client_disconnected(self, client_id: str) -> None:
        if (
            self.api_scan_context is not None
            and self.api_scan_context["client_id"] == client_id
        ):
            self.worker.cancel_scan()
        if self.api_motion_owner == client_id:
            self.api_jog_watchdog.stop()
            self.api_motion_owner = None
            self._emergency_stop()
            self._append_event("API", "", f"{client_id} 연결 종료로 Pan/Tilt 자동 STOP")
        if self.api_laser_owner == client_id and self.laser_is_on:
            self._laser_off()
            self._append_event("API", "", f"{client_id} 연결 종료로 레이저 자동 OFF")
        self._append_event("API", "", f"{client_id} 연결 종료")

    def _handle_api_command(
        self,
        command: str,
        params: dict[str, Any],
        client_id: str,
        request_id: Any,
    ) -> dict[str, Any]:
        if command == "system.ping":
            return {
                "protocol": PROTOCOL_NAME,
                "version": PROTOCOL_VERSION,
                "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            }
        if command == "system.status":
            return self._api_status_result()
        if command == "system.commands":
            return {"commands": self._api_command_names()}
        if command == "serial.ports":
            return {
                "ports": [
                    {
                        "port": item.device,
                        "description": item.description or "Serial Port",
                        "hwid": item.hwid,
                    }
                    for item in list_ports.comports()
                ]
            }
        if command == "serial.connect":
            return self._api_serial_connect(params)
        if command == "serial.disconnect":
            if self.is_scanning:
                self.worker.cancel_scan()
                return {"accepted": True, "scan_cancel_requested": True}
            if self.laser_is_on:
                self._laser_off()
            self.worker.request_close()
            return {"accepted": True}
        if command == "serial.scan":
            return self._api_serial_scan(params, client_id, request_id)
        if command == "serial.scan_cancel":
            if not self.is_scanning:
                return {"accepted": True, "scanning": False}
            context = self.api_scan_context
            if context is not None and context["client_id"] != client_id:
                raise ApiCommandError("BUSY", "다른 클라이언트가 자동검색 중입니다.")
            self.worker.cancel_scan()
            return {"accepted": True, "scanning": True}

        if command == "motion.jog":
            return self._api_motion_jog(params, client_id)
        if command == "motion.stop":
            self.api_jog_watchdog.stop()
            self.api_motion_owner = None
            self._emergency_stop()
            return {"accepted": True}
        if command == "motion.absolute":
            return self._api_motion_absolute(params, client_id, request_id)
        if command == "motion.completion_config":
            tolerance = self._api_optional_float(
                params, "tolerance_deg", minimum=0.05, maximum=5.0
            )
            if tolerance is not None:
                self.completion_tolerance.setValue(tolerance)
            if "stable_samples" in params:
                self.completion_samples.setValue(
                    self._api_int(params, "stable_samples", minimum=1, maximum=10)
                )
            if "timeout_s" in params:
                self.completion_timeout.setValue(
                    self._api_int(params, "timeout_s", minimum=2, maximum=120)
                )
            return self._api_completion_config()
        if command == "position.get":
            refresh = self._api_bool(params, "refresh", True)
            if refresh:
                self._api_require_connected()
                self._query_all_positions()
            return {
                "pan": self.current_pan,
                "tilt": self.current_tilt,
                "fresh_query_requested": refresh,
            }
        if command == "monitor.set":
            enabled = self._api_bool(params, "enabled")
            if enabled:
                self._api_require_connected()
            if "interval_ms" in params:
                self.monitor_interval.setValue(
                    self._api_int(params, "interval_ms", minimum=350, maximum=5000)
                )
            self.monitor_check.setChecked(enabled)
            self.api_server.broadcast(
                "monitor.state",
                {
                    "enabled": self.monitor_check.isChecked(),
                    "interval_ms": self.monitor_interval.value(),
                },
            )
            return {
                "enabled": self.monitor_check.isChecked(),
                "interval_ms": self.monitor_interval.value(),
            }

        if command == "recipe.list":
            return {"recipes": self.recipe_store.list_recipes()}
        if command == "recipe.upsert":
            recipe_id = params.get("recipe_id")
            if recipe_id is not None and not isinstance(recipe_id, str):
                raise ApiCommandError("INVALID_PARAMS", "recipe_id는 문자열이어야 합니다.")
            name_value = params.get("name", "Recipe")
            if not isinstance(name_value, str) or not name_value.strip():
                raise ApiCommandError("INVALID_PARAMS", "name 문자열이 필요합니다.")
            description = params.get("description", "")
            if not isinstance(description, str):
                raise ApiCommandError("INVALID_PARAMS", "description은 문자열이어야 합니다.")
            recipe = self.recipe_store.upsert_recipe(
                recipe_id=recipe_id,
                name=name_value.strip(),
                description=description.strip(),
            )
            if hasattr(self, "recipe_combo"):
                self._refresh_recipe_combo(recipe.id)
            return {"recipe": recipe.to_dict()}
        if command == "recipe.delete":
            recipe_id = self._api_str(params, "recipe_id")
            try:
                self.recipe_store.delete_recipe(recipe_id)
            except ValueError as exc:
                raise ApiCommandError("INVALID_STATE", str(exc)) from exc
            if hasattr(self, "recipe_combo"):
                self._refresh_recipe_combo()
            return {"accepted": True, "recipe_id": recipe_id}
        if command == "point.upsert":
            return self._api_point_upsert(params)
        if command == "point.delete":
            recipe_id = self._api_str(params, "recipe_id")
            point_id = self._api_str(params, "point_id")
            self.recipe_store.delete_point(recipe_id, point_id)
            if hasattr(self, "recipe_combo"):
                self._refresh_recipe_combo(recipe_id)
                self._refresh_recipe_points()
            return {"accepted": True, "recipe_id": recipe_id, "point_id": point_id}
        if command == "point.reorder":
            recipe_id = self._api_str(params, "recipe_id")
            ordered_ids = params.get("ordered_ids")
            if not isinstance(ordered_ids, list) or any(not isinstance(item, str) for item in ordered_ids):
                raise ApiCommandError("INVALID_PARAMS", "ordered_ids는 문자열 배열이어야 합니다.")
            try:
                recipe = self.recipe_store.reorder_points(recipe_id, ordered_ids)
            except ValueError as exc:
                raise ApiCommandError("INVALID_PARAMS", str(exc)) from exc
            if hasattr(self, "recipe_combo"):
                self._refresh_recipe_combo(recipe.id)
                self._refresh_recipe_points()
            return {"recipe": recipe.to_dict()}
        if command == "point.goto":
            return self._api_point_goto(params, client_id, request_id)

        if command == "protocol.catalog":
            return self._api_protocol_catalog()
        if command == "raw.describe":
            raw_hex = self._api_str(params, "hex")
            try:
                raw_command = parse_hex_command(raw_hex, self._address())
            except ProtocolError as exc:
                raise ApiCommandError("INVALID_PARAMS", str(exc)) from exc
            return {
                "hex": frame_to_hex(raw_command.data),
                "description": raw_command.description,
            }
        if command in protocol_named_command_names():
            return self._api_protocol_named(command, params)
        if command == "lens.motion":
            self._api_require_connected()
            action = self._api_enum(
                params,
                "action",
                {"zoom_in", "zoom_out", "focus_near", "focus_far", "iris_open", "iris_close"},
            )
            self._api_send(lens_motion(self._address(), action))
            return {"accepted": True, "action": action}
        if command == "aux.set":
            self._api_require_connected()
            number = self._api_int(params, "number", self.aux_number.value(), minimum=1, maximum=255)
            enabled = self._api_bool(params, "enabled")
            self._api_send(set_auxiliary(self._address(), number, enabled))
            if number == self.aux_number.value():
                self.laser_is_on = enabled
                if hasattr(self, "laser_state_label"):
                    self.laser_state_label.setText(f"출력 상태: {'ON' if enabled else 'OFF'}")
            return {"accepted": True, "number": number, "enabled": enabled}
        if command == "preset.set":
            self._api_require_confirm(params)
            self._api_require_connected()
            number = self._api_int(params, "number", minimum=1, maximum=255)
            self._api_send(set_preset(self._address(), number))
            return {"accepted": True, "number": number, "note": "direct protocol preset save"}
        if command in {"preset.call", "preset.goto"}:
            self._api_require_connected()
            number = self._api_int(params, "number", minimum=1, maximum=255)
            self._api_send(auto_position_speed(self._address()))
            self._api_send(call_preset(self._address(), number))
            self._begin_target_tracking(f"프로토콜 프리셋 {number} 호출", settle_mode=True)
            return {"accepted": True, "number": number}
        if command in {"preset.clear", "preset.delete"}:
            self._api_require_confirm(params)
            self._api_require_connected()
            number = self._api_int(params, "number", minimum=1, maximum=255)
            self._api_send(clear_preset(self._address(), number))
            self.preset_store.delete(self._address(), number)
            return {"accepted": True, "number": number}
        if command == "preset.speed_adjust":
            self._api_require_connected()
            self._api_send(auto_position_speed(self._address()))
            return {"accepted": True, "fixed_maximum": True}

        if command == "scan.set_point":
            self._api_require_connected()
            point = self._api_enum(params, "point", {"start", "end"})
            self._api_send(set_line_scan_point(self._address(), point == "start"))
            return {"accepted": True, "point": point}
        if command == "scan.start":
            self._api_require_connected()
            mode = self._api_enum(params, "mode", {"vendor", "zone"}, "vendor")
            api_command = (
                vendor_line_scan(self._address(), True)
                if mode == "vendor"
                else zone_scan(self._address(), True)
            )
            self._api_send(set_scan_speed(self._address(), AUTO_PAN_SPEED, AUTO_TILT_SPEED))
            self._api_send(api_command)
            return {"accepted": True, "mode": mode, "fixed_maximum": True}
        if command == "scan.stop":
            self._api_require_connected()
            mode = self._api_enum(params, "mode", {"vendor", "zone"}, "vendor")
            api_command = (
                vendor_line_scan(self._address(), False)
                if mode == "vendor"
                else zone_scan(self._address(), False)
            )
            self._api_send(api_command)
            return {"accepted": True, "mode": mode}
        if command == "scan.speed":
            self._api_require_connected()
            self._api_send(set_scan_speed(self._address(), AUTO_PAN_SPEED, AUTO_TILT_SPEED))
            return {"accepted": True, "pan_speed": AUTO_PAN_SPEED, "tilt_speed": AUTO_TILT_SPEED, "fixed_maximum": True}
        if command == "scan.speed_adjust":
            self._api_require_connected()
            self._api_send(set_scan_speed(self._address(), AUTO_PAN_SPEED, AUTO_TILT_SPEED))
            return {"accepted": True, "fixed_maximum": True}

        if command == "cruise.start":
            self._api_require_connected()
            track = self._api_int(params, "track", minimum=1, maximum=8)
            self._api_send(set_cruise_speed(self._address(), AUTO_PAN_SPEED, AUTO_TILT_SPEED))
            self._api_send(start_cruise(self._address(), track))
            return {"accepted": True, "track": track, "fixed_maximum": True}
        if command == "cruise.stop":
            self._emergency_stop()
            return {"accepted": True}
        if command == "cruise.speed":
            self._api_require_connected()
            self._api_send(set_cruise_speed(self._address(), AUTO_PAN_SPEED, AUTO_TILT_SPEED))
            return {"accepted": True, "pan_speed": AUTO_PAN_SPEED, "tilt_speed": AUTO_TILT_SPEED, "fixed_maximum": True}

        if command == "home.auto":
            self._api_require_connected()
            enabled = self._api_bool(params, "enabled")
            self._api_send(auto_home(self._address(), enabled))
            return {"accepted": True, "enabled": enabled}
        if command == "home.after":
            self._api_require_connected()
            action = self._api_enum(params, "action", {"preset1", "cruise1"})
            self._api_send(
                home_then_preset1(self._address())
                if action == "preset1"
                else home_then_cruise1(self._address())
            )
            return {"accepted": True, "action": action}

        if command == "laser.arm":
            enabled = self._api_bool(params, "enabled")
            self.laser_arm.setChecked(enabled)
            return {"armed": self.laser_arm.isChecked(), "laser_on": self.laser_is_on}
        if command == "laser.on":
            self._api_require_connected()
            if not self.laser_arm.isChecked():
                raise ApiCommandError(
                    "LASER_NOT_ARMED", "먼저 laser.arm을 활성화하세요."
                )
            if self.api_laser_owner not in (None, client_id) and self.laser_is_on:
                raise ApiCommandError(
                    "BUSY", "다른 클라이언트가 레이저를 제어 중입니다."
                )
            self.api_laser_owner = client_id
            self._laser_on()
            return {"accepted": True, "laser_on": self.laser_is_on}
        if command == "laser.off":
            self._api_require_connected()
            self._laser_off()
            return {"accepted": True, "laser_on": self.laser_is_on}
        if command == "laser.pulse":
            self._api_require_connected()
            if not self.laser_arm.isChecked():
                raise ApiCommandError(
                    "LASER_NOT_ARMED", "먼저 laser.arm을 활성화하세요."
                )
            if self.api_laser_owner not in (None, client_id) and self.laser_is_on:
                raise ApiCommandError(
                    "BUSY", "다른 클라이언트가 레이저를 제어 중입니다."
                )
            duration = self._api_int(
                params, "duration_ms", 500, minimum=50, maximum=60_000
            )
            self.laser_pulse_ms.setValue(duration)
            self.api_laser_owner = client_id
            self._laser_pulse()
            return {"accepted": True, "duration_ms": duration}

        if command == "device.query":
            self._api_require_connected()
            query = self._api_enum(
                params, "type", {"pan", "tilt", "zoom", "focus", "device"}
            )
            api_command = {
                "pan": query_pan,
                "tilt": query_tilt,
                "zoom": query_zoom,
                "focus": query_focus,
                "device": query_device_type,
            }[query](self._address())
            self._api_send(api_command)
            return {"accepted": True, "type": query}

        if command == "maintenance.self_check":
            self._api_require_confirm(params)
            self._api_require_connected()
            self._api_send(self_check(self._address()))
            return {"accepted": True}
        if command == "maintenance.restart":
            self._api_require_confirm(params)
            self._api_require_connected()
            self._api_send(remote_restart(self._address()))
            return {"accepted": True}
        if command == "maintenance.factory_default":
            self._api_require_confirm(params)
            self._api_require_connected()
            self._api_send(factory_default(self._address()))
            return {"accepted": True}
        if command == "maintenance.power_on_self_check":
            self._api_require_confirm(params)
            self._api_require_connected()
            enabled = self._api_bool(params, "enabled")
            self._api_send(power_on_self_check(self._address(), enabled))
            return {"accepted": True, "enabled": enabled}
        if command == "raw.send":
            self._api_require_confirm(params)
            self._api_require_connected()
            raw_hex = self._api_str(params, "hex")
            try:
                raw_command = parse_hex_command(raw_hex, self._address())
            except ProtocolError as exc:
                raise ApiCommandError("INVALID_PARAMS", str(exc)) from exc
            self._api_send(raw_command)
            return {"accepted": True, "hex": frame_to_hex(raw_command.data)}

        raise ApiCommandError(
            "UNKNOWN_COMMAND",
            f"지원하지 않는 command입니다: {command}",
            {"hint": "system.commands로 목록을 조회하세요."},
        )

    @staticmethod
    def _api_command_names() -> list[str]:
        return supported_command_names()


    def _api_protocol_named(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        self._api_require_connected()
        try:
            built = build_named_protocol_command(self._address(), command, params)
        except ProtocolError as exc:
            raise ApiCommandError("INVALID_PARAMS", str(exc)) from exc
        if built.requires_confirm:
            self._api_require_confirm(params)
        if command in {"motion.relative", "pelco.flip", "pelco.zero_pan", "pattern.run", "preset.scan", "camera.scan"}:
            self._api_send(auto_position_speed(self._address()))
            self._api_send(set_scan_speed(self._address(), AUTO_PAN_SPEED, AUTO_TILT_SPEED))
            self._api_send(set_cruise_speed(self._address(), AUTO_PAN_SPEED, AUTO_TILT_SPEED))
        for item in built.commands:
            self._api_send(item)
        return {"accepted": True, **built.result}

    def _api_status_result(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "serial": {
                "connected": self.is_connected,
                "port": self.port_combo.currentData(),
                "baudrate": int(self.baud_combo.currentData()),
                "address": self._address(),
            },
            "position": {"pan": self.current_pan, "tilt": self.current_tilt},
            "motion": {
                "tracking": self.motion_tracker is not None,
                "label": self.motion_tracker.label if self.motion_tracker else None,
                "api_jog_owner": self.api_motion_owner,
                "completion": self._api_completion_config(),
            },
            "laser": {
                "armed": self.laser_arm.isChecked(),
                "on": self.laser_is_on,
                "aux_number": self.aux_number.value(),
            },
            "monitor": {
                "enabled": self.monitor_check.isChecked(),
                "interval_ms": self.monitor_interval.value(),
            },
            "scanning": self.is_scanning,
        }

    def _api_completion_config(self) -> dict[str, Any]:
        return {
            "tolerance_deg": self.completion_tolerance.value(),
            "stable_samples": self.completion_samples.value(),
            "timeout_s": self.completion_timeout.value(),
        }

    def _api_serial_connect(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.is_scanning:
            raise ApiCommandError("BUSY", "자동검색이 진행 중입니다.")
        port_value = params.get("port", self.port_combo.currentData())
        if not isinstance(port_value, str) or not port_value.strip():
            raise ApiCommandError(
                "INVALID_PARAMS", "serial.connect에 port가 필요합니다."
            )
        port = port_value.strip()
        baudrate = self._api_int(params, "baudrate", STARTUP_BAUDRATE, minimum=300, maximum=115_200)
        address = self._api_int(params, "address", 1, minimum=1, maximum=255)
        port_index = self.port_combo.findData(port)
        if port_index < 0:
            self.port_combo.addItem(port, port)
            port_index = self.port_combo.findData(port)
        self.port_combo.setCurrentIndex(port_index)
        baud_index = self.baud_combo.findData(baudrate)
        if baud_index < 0:
            self.baud_combo.addItem(str(baudrate), baudrate)
            baud_index = self.baud_combo.findData(baudrate)
        self.baud_combo.setCurrentIndex(baud_index)
        self.address_spin.setValue(address)
        self.connect_button.setEnabled(False)
        self.worker.request_open(SerialConfig(port, baudrate, address))
        return {
            "accepted": True,
            "port": port,
            "baudrate": baudrate,
            "address": address,
        }

    def _api_serial_scan(
        self, params: dict[str, Any], client_id: str, request_id: Any
    ) -> dict[str, Any]:
        if self.is_scanning:
            raise ApiCommandError("BUSY", "자동검색이 이미 진행 중입니다.")
        if self.is_connected:
            raise ApiCommandError(
                "ALREADY_CONNECTED", "자동검색 전에 serial.disconnect를 실행하세요."
            )
        available_ports = [item.device for item in list_ports.comports()]
        requested_ports = params.get("ports", available_ports)
        if (
            not isinstance(requested_ports, list)
            or not requested_ports
            or any(
                not isinstance(item, str) or not item.strip()
                for item in requested_ports
            )
        ):
            raise ApiCommandError(
                "INVALID_PARAMS", "ports는 COM 포트 문자열 배열이어야 합니다."
            )
        ports = [item.strip() for item in requested_ports]
        requested_bauds = params.get("baudrates", [STARTUP_BAUDRATE])
        allowed_bauds = {2400, 4800, 9600, 19200}
        if (
            not isinstance(requested_bauds, list)
            or not requested_bauds
            or any(
                isinstance(item, bool)
                or not isinstance(item, int)
                or item not in allowed_bauds
                for item in requested_bauds
            )
        ):
            raise ApiCommandError(
                "INVALID_PARAMS",
                "baudrates는 2400/4800/9600/19200 정수 배열이어야 합니다.",
            )
        first = self._api_int(params, "first_address", 1, minimum=1, maximum=255)
        last = self._api_int(params, "last_address", 16, minimum=1, maximum=255)
        if first > last:
            raise ApiCommandError(
                "INVALID_PARAMS", "first_address는 last_address보다 클 수 없습니다."
            )
        timeout_ms = self._api_int(params, "timeout_ms", 200, minimum=80, maximum=1000)
        attempts = len(ports) * len(requested_bauds) * (last - first + 1)
        self.api_scan_context = {
            "client_id": client_id,
            "request_id": request_id,
            "command": "serial.scan",
        }
        self.scan_progress_dialog_enabled = False
        self.scan_notify_on_failure = False
        self.is_scanning = True
        self._set_connected(False, "API 자동검색 준비")
        self.connect_button.setEnabled(False)
        self.worker.request_scan(ports, requested_bauds, first, last, timeout_ms)
        return {
            "accepted": True,
            "attempts": attempts,
            "ports": ports,
            "baudrates": requested_bauds,
            "address_range": [first, last],
            "timeout_ms": timeout_ms,
        }

    def _api_motion_jog(self, params: dict[str, Any], client_id: str) -> dict[str, Any]:
        self._api_require_connected()
        if "pan_speed" in params or "tilt_speed" in params:
            raise ApiCommandError("INVALID_PARAMS", "pan_level/tilt_level (1~8)을 사용하세요.")
        if (
            self.api_motion_context is not None
            and self.api_motion_context["client_id"] != client_id
        ):
            raise ApiCommandError(
                "BUSY", "다른 클라이언트의 이동 완료 판정이 진행 중입니다."
            )
        if self.api_motion_owner not in (None, client_id):
            raise ApiCommandError("BUSY", "다른 클라이언트가 Jog를 제어 중입니다.")
        pan_text = self._api_enum(params, "pan", {"left", "right", "stop"}, "stop")
        tilt_text = self._api_enum(params, "tilt", {"up", "down", "stop"}, "stop")
        pan = {
            "left": PanDirection.LEFT,
            "right": PanDirection.RIGHT,
            "stop": PanDirection.STOP,
        }[pan_text]
        tilt = {
            "up": TiltDirection.UP,
            "down": TiltDirection.DOWN,
            "stop": TiltDirection.STOP,
        }[tilt_text]
        pan_level = self._api_int(
            params, "pan_level", self.pan_speed.value(), minimum=1, maximum=8
        )
        tilt_level = self._api_int(
            params, "tilt_level", self.tilt_speed.value(), minimum=1, maximum=8
        )
        pan_speed = manual_speed_value(pan_level)
        tilt_speed = manual_speed_value(tilt_level)
        if pan is PanDirection.STOP and tilt is TiltDirection.STOP:
            self._emergency_stop()
            self.api_motion_owner = None
            self.api_jog_key = None
            self.api_jog_watchdog.stop()
            return {"accepted": True, "stopped": True}

        jog_key = (pan, tilt, pan_speed, tilt_speed)
        unchanged = self.api_motion_owner == client_id and self.api_jog_key == jog_key

        if not unchanged:
            self._cancel_motion_tracking("API Jog가 시작됨")
            self._api_send(
                manual_motion(self._address(), pan, tilt, pan_speed, tilt_speed)
            )
            self.api_jog_key = jog_key

        self.api_motion_owner = client_id
        if not unchanged or not self.api_jog_watchdog.isActive():
            # Start/restart only for a real motion change. Duplicate requests
            # from older clients do not postpone the 5 s Pelco refresh.
            self.api_jog_watchdog.start()
        return {
            "accepted": True,
            "continuous": True,
            "unchanged": unchanged,
            "pan": pan_text,
            "tilt": tilt_text,
            "pan_level": pan_level,
            "tilt_level": tilt_level,
            "pan_percent": manual_speed_percent(pan_level),
            "tilt_percent": manual_speed_percent(tilt_level),
            "resend_interval_ms": PELCOD_MANUAL_REFRESH_MS,
        }

    def _api_jog_keepalive(self) -> None:
        if (
            not self.is_connected
            or self.api_motion_owner is None
            or self.api_jog_key is None
        ):
            self.api_jog_watchdog.stop()
            return
        pan, tilt, pan_speed, tilt_speed = self.api_jog_key
        self._api_send(
            manual_motion(self._address(), pan, tilt, pan_speed, tilt_speed)
        )

    def _api_motion_absolute(
        self, params: dict[str, Any], client_id: str, request_id: Any
    ) -> dict[str, Any]:
        self._api_require_connected()
        if self.api_motion_owner not in (None, client_id):
            raise ApiCommandError("BUSY", "다른 클라이언트가 Jog를 제어 중입니다.")
        if self.motion_tracker is not None:
            raise ApiCommandError("BUSY", "이동 완료 판정이 이미 진행 중입니다.")
        self.api_jog_watchdog.stop()
        self.api_motion_owner = None
        self.api_jog_key = None
        pan = self._api_optional_float(params, "pan", minimum=0.0, maximum=359.99)
        tilt = self._api_optional_float(params, "tilt", minimum=-60.0, maximum=60.0)
        if pan is None and tilt is None:
            raise ApiCommandError(
                "INVALID_PARAMS", "pan 또는 tilt 중 하나 이상이 필요합니다."
            )
        self._api_send(auto_position_speed(self._address()))
        context = {
            "client_id": client_id,
            "request_id": request_id,
            "command": "motion.absolute",
        }

        def begin_tracking() -> None:
            self._begin_target_tracking(
                "API 절대좌표 이동", pan=pan, tilt=tilt, api_context=context
            )

        if pan is not None:
            self._api_send(set_pan_position(self._address(), pan))
        if tilt is not None:
            self._api_send(set_tilt_position(self._address(), tilt))
        begin_tracking()
        return {"accepted": True, "target": {"pan": pan, "tilt": tilt}}

    def _api_preset_set(
        self, params: dict[str, Any], client_id: str, request_id: Any
    ) -> dict[str, Any]:
        self._api_require_connected()
        if self.api_motion_owner not in (None, client_id):
            raise ApiCommandError("BUSY", "다른 클라이언트가 Jog를 제어 중입니다.")
        number = self._api_int(params, "number", minimum=1, maximum=255)
        pan = self._api_optional_float(params, "pan", minimum=0.0, maximum=359.99)
        tilt = self._api_optional_float(params, "tilt", minimum=-60.0, maximum=60.0)
        if (pan is None) != (tilt is None):
            raise ApiCommandError(
                "INVALID_PARAMS", "좌표 저장에는 pan과 tilt가 모두 필요합니다."
            )
        if pan is None:
            self._api_send(set_preset(self._address(), number))
            if self.current_pan is not None and self.current_tilt is not None:
                self.preset_store.set(
                    self._address(), number, self.current_pan, self.current_tilt
                )
            self.api_server.broadcast(
                "preset.saved",
                {"number": number, "pan": self.current_pan, "tilt": self.current_tilt},
            )
            return {"accepted": True, "number": number, "uses_current_position": True}
        if self.motion_tracker is not None:
            raise ApiCommandError("BUSY", "이동 완료 판정이 이미 진행 중입니다.")
        self.api_jog_watchdog.stop()
        self.api_motion_owner = None
        context = {
            "client_id": client_id,
            "request_id": request_id,
            "command": "preset.set",
        }
        self._api_send(auto_position_speed(self._address()))

        def save_after_arrival() -> None:
            if not self._send(set_preset(self._address(), number)):
                return
            self.preset_store.set(self._address(), number, pan, tilt)
            self.api_server.broadcast(
                "preset.saved", {"number": number, "pan": pan, "tilt": tilt}
            )

        self._api_send(set_pan_position(self._address(), pan))
        self._api_send(set_tilt_position(self._address(), tilt))
        self._begin_target_tracking(
            f"API preset {number} target move",
            pan=pan,
            tilt=tilt,
            callback=save_after_arrival,
            api_context=context,
        )
        return {
            "accepted": True,
            "number": number,
            "target": {"pan": pan, "tilt": tilt},
        }
        self._api_send(set_pan_position(self._address(), pan))

        def save_after_arrival() -> None:
            if not self._send(set_preset(self._address(), number)):
                return
            self.preset_store.set(self._address(), number, pan, tilt)
            self.api_server.broadcast(
                "preset.saved", {"number": number, "pan": pan, "tilt": tilt}
            )

        def send_tilt() -> None:
            if self._send(set_tilt_position(self._address(), tilt)):
                self._begin_target_tracking(
                    f"API 프리셋 {number} 좌표 이동",
                    pan=pan,
                    tilt=tilt,
                    callback=save_after_arrival,
                    api_context=context,
                )

        QTimer.singleShot(250, send_tilt)
        return {
            "accepted": True,
            "number": number,
            "target": {"pan": pan, "tilt": tilt},
        }

    def _api_preset_goto(
        self, params: dict[str, Any], client_id: str, request_id: Any
    ) -> dict[str, Any]:
        self._api_require_connected()
        if self.api_motion_owner not in (None, client_id):
            raise ApiCommandError("BUSY", "다른 클라이언트가 Jog를 제어 중입니다.")
        if self.motion_tracker is not None:
            raise ApiCommandError("BUSY", "이동 완료 판정이 이미 진행 중입니다.")
        self.api_jog_watchdog.stop()
        self.api_motion_owner = None
        number = self._api_int(params, "number", minimum=1, maximum=255)
        self._api_send(call_preset(self._address(), number))
        record = self.preset_store.get(self._address(), number)
        context = {
            "client_id": client_id,
            "request_id": request_id,
            "command": "preset.goto",
        }
        if record is None:
            self._begin_target_tracking(
                f"API 프리셋 {number} 이동", settle_mode=True, api_context=context
            )
        else:
            self._begin_target_tracking(
                f"API 프리셋 {number} 이동",
                pan=float(record["pan"]),
                tilt=float(record["tilt"]),
                api_context=context,
            )
        return {"accepted": True, "number": number, "known_coordinates": record}



    @staticmethod
    def _api_protocol_catalog() -> dict[str, Any]:
        return api_protocol_catalog(MainWindow._api_command_names())

    def _api_point_upsert(self, params: dict[str, Any]) -> dict[str, Any]:
        recipe_id = self._api_str(params, "recipe_id")
        point_id = params.get("point_id")
        if point_id is not None and not isinstance(point_id, str):
            raise ApiCommandError("INVALID_PARAMS", "point_id는 문자열이어야 합니다.")
        name = params.get("name", "Point")
        if not isinstance(name, str) or not name.strip():
            raise ApiCommandError("INVALID_PARAMS", "name 문자열이 필요합니다.")
        pan = self._api_optional_float(params, "pan", minimum=0.0, maximum=359.99)
        tilt = self._api_optional_float(params, "tilt", minimum=-60.0, maximum=60.0)
        if pan is None or tilt is None:
            raise ApiCommandError("INVALID_PARAMS", "pan과 tilt가 모두 필요합니다.")
        dwell_ms = self._api_int(params, "dwell_ms", 0, minimum=0, maximum=3_600_000)
        note = params.get("note", "")
        if not isinstance(note, str):
            raise ApiCommandError("INVALID_PARAMS", "note는 문자열이어야 합니다.")
        enabled = self._api_bool(params, "enabled", True)
        point = self.recipe_store.upsert_point(
            recipe_id,
            point_id=point_id,
            name=name.strip(),
            pan=pan,
            tilt=tilt,
            dwell_ms=dwell_ms,
            note=note.strip(),
            enabled=enabled,
        )
        if hasattr(self, "recipe_combo"):
            self._refresh_recipe_combo(recipe_id)
            self._refresh_recipe_points(point.id)
        return {"point": point.to_dict()}

    def _api_point_goto(
        self, params: dict[str, Any], client_id: str, request_id: Any
    ) -> dict[str, Any]:
        self._api_require_connected()
        if self.api_motion_owner not in (None, client_id):
            raise ApiCommandError("BUSY", "다른 클라이언트가 Jog를 제어 중입니다.")
        if self.motion_tracker is not None:
            raise ApiCommandError("BUSY", "이동 완료 판정이 이미 진행 중입니다.")
        recipe_id = self._api_str(params, "recipe_id")
        point_id = self._api_str(params, "point_id")
        try:
            point = self.recipe_store.get_point(recipe_id, point_id)
        except KeyError as exc:
            raise ApiCommandError("NOT_FOUND", str(exc)) from exc
        if not point.enabled:
            raise ApiCommandError("DISABLED", "비활성 포인트입니다.")
        context = {
            "client_id": client_id,
            "request_id": request_id,
            "command": "point.goto",
            "recipe_id": recipe_id,
            "point_id": point_id,
        }
        if not self._move_to_recipe_point(
            point,
            label=f"API 포인트 이동: {point.name}",
            api_context=context,
        ):
            raise ApiCommandError("NOT_CONNECTED", "PT 장비가 연결되지 않았습니다.")
        return {"accepted": True, "point": point.to_dict()}

    def _api_send(self, command: OutgoingCommand) -> None:
        if not self._send(command):
            raise ApiCommandError("NOT_CONNECTED", "PT 장비가 연결되지 않았습니다.")

    def _api_require_connected(self) -> None:
        if not self.is_connected:
            raise ApiCommandError("NOT_CONNECTED", "먼저 PT 장비를 연결하세요.")

    @staticmethod
    def _api_require_confirm(params: dict[str, Any]) -> None:
        if params.get("confirm") is not True:
            raise ApiCommandError(
                "CONFIRM_REQUIRED", "위험 명령에는 params.confirm=true가 필요합니다."
            )

    @staticmethod
    def _api_bool(
        params: dict[str, Any], name: str, default: bool | None = None
    ) -> bool:
        value = params.get(name, default)
        if not isinstance(value, bool):
            raise ApiCommandError("INVALID_PARAMS", f"{name}은 true/false여야 합니다.")
        return value

    @staticmethod
    def _api_int(
        params: dict[str, Any],
        name: str,
        default: int | None = None,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = params.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ApiCommandError("INVALID_PARAMS", f"{name}은 정수여야 합니다.")
        if not minimum <= value <= maximum:
            raise ApiCommandError(
                "OUT_OF_RANGE", f"{name}은 {minimum}~{maximum} 범위여야 합니다."
            )
        return value

    @staticmethod
    def _api_optional_float(
        params: dict[str, Any], name: str, *, minimum: float, maximum: float
    ) -> float | None:
        if name not in params or params[name] is None:
            return None
        value = params[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApiCommandError("INVALID_PARAMS", f"{name}은 숫자여야 합니다.")
        result = float(value)
        if not minimum <= result <= maximum:
            raise ApiCommandError(
                "OUT_OF_RANGE", f"{name}은 {minimum}~{maximum} 범위여야 합니다."
            )
        return result

    @staticmethod
    def _api_enum(
        params: dict[str, Any],
        name: str,
        allowed: set[str],
        default: str | None = None,
    ) -> str:
        value = params.get(name, default)
        if not isinstance(value, str) or value not in allowed:
            raise ApiCommandError(
                "INVALID_PARAMS", f"{name}은 {sorted(allowed)} 중 하나여야 합니다."
            )
        return value

    @staticmethod
    def _api_str(params: dict[str, Any], name: str) -> str:
        value = params.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ApiCommandError("INVALID_PARAMS", f"{name} 문자열이 필요합니다.")
        return value.strip()

    def _refresh_gamepads(self) -> None:
        self.gamepad_manager.refresh_devices()

    def _auto_connect_and_enable_gamepad(self) -> None:
        if not self.auto_gamepad_on_serial_connect or not self.is_connected:
            return
        if not self.gamepad_manager.available:
            self.gamepad_status.setText("pygame 미설치 · 게임패드 자동 연결 불가")
            return
        if self.gamepad_manager.is_connected:
            self._enable_gamepad_control()
            return
        devices = self.gamepad_manager.refresh_devices()
        if not devices:
            self.gamepad_status.setText("장비 연결됨 · 검색된 게임패드 없음")
            return
        index = self.gamepad_combo.currentData()
        if index is None:
            index = devices[0].index
        self.gamepad_status.setText("게임패드 자동 연결 중...")
        self.gamepad_manager.connect_device(int(index), auto_reconnect=True)

    def _enable_gamepad_control(self) -> None:
        if not self.is_connected or not self.gamepad_manager.is_connected:
            return
        self.gamepad_enable.setEnabled(True)
        if not self.gamepad_enable.isChecked():
            self.gamepad_enable.setChecked(True)

    def _on_gamepad_devices(self, devices: list[GamepadDevice]) -> None:
        previous = self.gamepad_combo.currentData()
        self.gamepad_combo.clear()
        for device in devices:
            mode = "SDL 표준" if device.standard_mapping else "raw"
            self.gamepad_combo.addItem(
                f"{device.index}: {device.name} · {mode} · {device.guid}", device.index
            )
        if previous is not None:
            index = self.gamepad_combo.findData(previous)
            if index >= 0:
                self.gamepad_combo.setCurrentIndex(index)
        if not devices:
            self.gamepad_combo.addItem("검색된 게임패드 없음", None)
        self.gamepad_connect_button.setEnabled(
            self.gamepad_manager.available and bool(devices)
        )

    def _toggle_gamepad_connection(self) -> None:
        if self.gamepad_manager.is_connected:
            self.gamepad_manager.set_auto_reconnect(False)
            self.gamepad_manager.close(reconnect=False)
            return
        index = self.gamepad_combo.currentData()
        if index is None:
            self.statusBar().showMessage("연결할 게임패드를 선택하세요.", 3000)
            return
        self.gamepad_manager.connect_device(int(index), auto_reconnect=True)

    def _on_gamepad_connected(self, name: str) -> None:
        self.gamepad_connect_button.setText("게임패드 연결 해제")
        self.gamepad_enable.setEnabled(True)
        self.gamepad_rumble_button.setEnabled(True)
        self.gamepad_status.setText(f"연결됨: {name} · 제어 허용 대기")
        self._append_event("GAMEPAD", "", f"게임패드 연결: {name}")

        self._enable_gamepad_control()

    def _on_gamepad_disconnected(self, reason: str) -> None:
        self._stop_gamepad_motion(send_stop=True)
        self.gamepad_enable.blockSignals(True)
        self.gamepad_enable.setChecked(False)
        self.gamepad_enable.blockSignals(False)
        self.gamepad_enable.setEnabled(False)
        self.gamepad_rumble_button.setEnabled(False)
        self.gamepad_connect_button.setText("게임패드 연결")
        self.gamepad_status.setText(reason)
        self._append_event("GAMEPAD", "", f"게임패드 연결 해제: {reason}")
        self._refresh_gamepads()

    def _test_gamepad_rumble(self) -> None:
        if self.gamepad_manager.rumble(0.25, 0.75, 300):
            self.gamepad_status.setText("진동 명령 전송")
        else:
            self.gamepad_status.setText("이 패드/드라이버는 진동 API를 지원하지 않음")

    def _gamepad_enable_changed(self, enabled: bool) -> None:
        if enabled and (not self.gamepad_manager.is_connected or not self.is_connected):
            self.gamepad_enable.blockSignals(True)
            self.gamepad_enable.setChecked(False)
            self.gamepad_enable.blockSignals(False)
            self.statusBar().showMessage(
                "PT 장비와 게임패드를 모두 연결한 뒤 제어를 허용하세요.", 3500
            )
            return
        if enabled:
            self.gamepad_stop_latched = True
            self.gamepad_status.setText("제어 활성 대기 · 스틱을 중앙으로 놓으세요")
            self._append_event("GAMEPAD", "", "게임패드 장비 제어 활성화")
        else:
            self._stop_gamepad_motion(send_stop=True)
            if self.gamepad_manager.is_connected:
                self.gamepad_status.setText("연결됨 · 장비 제어 비활성")
            self._append_event("GAMEPAD", "", "게임패드 장비 제어 비활성화")

    def _on_gamepad_state(self, state: GamepadState) -> None:
        axes_text = "  ".join(f"A{i}={value:+.3f}" for i, value in enumerate(state.axes))
        self.gamepad_axes_label.setText(
            f"{state.mapping_mode}: {axes_text}" if axes_text else "축 없음"
        )
        pressed = [str(i) for i, value in enumerate(state.buttons) if value]
        self.gamepad_buttons_label.setText(", ".join(pressed) or "없음")
        self.gamepad_hat_label.setText(
            "  ".join(f"H{i}={value}" for i, value in enumerate(state.hats))
            or "Hat 없음"
        )

        previous_buttons = self.gamepad_previous_buttons
        previous_axes = self.gamepad_previous_axes
        previous_hat = self.gamepad_previous_hat
        current_hat = self._gamepad_hat(state)
        self.gamepad_previous_axes = state.axes
        self.gamepad_previous_buttons = state.buttons
        self.gamepad_previous_hat = current_hat

        if not self.gamepad_enable.isChecked() or not self.is_connected:
            return

        if self._button_edge(gamepad_map.BUTTON_STOP, state.buttons, previous_buttons):
            self.gamepad_stop_latched = True
            self._emergency_stop()
            self.gamepad_manager.rumble(0.7, 1.0, 180)
            self.gamepad_status.setText("STOP 입력 · 스틱을 중앙으로 놓아 해제")
            return

        self._handle_gamepad_dpad(
            current_hat, previous_hat, state.buttons, previous_buttons
        )
        if self._button_edge(
            gamepad_map.BUTTON_GOTO_SELECTION, state.buttons, previous_buttons
        ):
            self.gamepad_stop_latched = True
            self._stop_gamepad_motion(send_stop=True)
            self._goto_recipe_point()
        if self._button_edge(gamepad_map.BUTTON_HOME, state.buttons, previous_buttons):
            self.gamepad_stop_latched = True
            self._stop_gamepad_motion(send_stop=True)
            self._move_to_target_position(
                0.0,
                0.0,
                label="Gamepad home 0,0",
                cancel_reason="gamepad home",
            )
        if self._button_edge(
            gamepad_map.BUTTON_LASER_TOGGLE, state.buttons, previous_buttons
        ):
            self._toggle_gamepad_laser()
        if self._button_edge(
            gamepad_map.BUTTON_RECIPE_PREVIOUS, state.buttons, previous_buttons
        ):
            self._step_gamepad_recipe(-1)
        if self._button_edge(
            gamepad_map.BUTTON_RECIPE_NEXT, state.buttons, previous_buttons
        ):
            self._step_gamepad_recipe(1)
        if self._trigger_edge(
            gamepad_map.TRIGGER_POINT_PREVIOUS_AXIS,
            state.axes,
            previous_axes,
        ) or self._button_edge(
            gamepad_map.TRIGGER_POINT_PREVIOUS_BUTTON,
            state.buttons,
            previous_buttons,
        ):
            self._step_gamepad_point(-1)
        if self._trigger_edge(
            gamepad_map.TRIGGER_POINT_NEXT_AXIS,
            state.axes,
            previous_axes,
        ) or self._button_edge(
            gamepad_map.TRIGGER_POINT_NEXT_BUTTON,
            state.buttons,
            previous_buttons,
        ):
            self._step_gamepad_point(1)
        if self._button_edge(
            gamepad_map.BUTTON_QUERY_POSITION, state.buttons, previous_buttons
        ):
            self._query_all_positions()
        if self._button_edge(
            gamepad_map.BUTTON_CALL_PRESET, state.buttons, previous_buttons
        ):
            self.gamepad_status.setText("프리셋 기능 비활성 · 레시피 포인트는 화면/API에서 이동")
        if self._button_edge(
            gamepad_map.BUTTON_LASER_PULSE, state.buttons, previous_buttons
        ):
            self._laser_pulse()
        if self._button_edge(
            gamepad_map.BUTTON_LASER_OFF, state.buttons, previous_buttons
        ):
            self._laser_off()
        # Preset number shortcuts are intentionally disabled; point tables are managed by recipe.
        if self._button_edge(
            gamepad_map.BUTTON_TOGGLE_MONITOR, state.buttons, previous_buttons
        ):
            self.monitor_check.toggle()

        self._apply_gamepad_motion(state.axes)

    @staticmethod
    def _button_edge(
        index: int | None,
        current: tuple[bool, ...],
        previous: tuple[bool, ...],
    ) -> bool:
        if index is None or not 0 <= index < len(current):
            return False
        was_pressed = previous[index] if index < len(previous) else False
        return current[index] and not was_pressed

    @staticmethod
    def _button_value(index: int | None, buttons: tuple[bool, ...]) -> bool:
        return index is not None and 0 <= index < len(buttons) and buttons[index]

    @staticmethod
    def _trigger_edge(
        index: int | None,
        current: tuple[float, ...],
        previous: tuple[float, ...],
    ) -> bool:
        if index is None or not 0 <= index < len(current):
            return False
        threshold = gamepad_map.TRIGGER_THRESHOLD
        was_pressed = previous[index] > threshold if index < len(previous) else False
        return current[index] > threshold and not was_pressed

    def _step_gamepad_recipe(self, delta: int) -> None:
        if not hasattr(self, "recipe_combo") or self.recipe_combo.count() <= 0:
            self.gamepad_status.setText("No recipe to select")
            return
        current = self.recipe_combo.currentIndex()
        next_index = (current + delta) % self.recipe_combo.count()
        self.recipe_combo.setCurrentIndex(next_index)
        recipe = self._selected_recipe()
        self.gamepad_status.setText(f"Recipe selected: {recipe.name}")

    def _step_gamepad_point(self, delta: int) -> None:
        recipe = self._selected_recipe()
        if not recipe.points:
            self.gamepad_status.setText("No point in selected recipe")
            return
        rows = self.recipe_point_table.selectionModel().selectedRows()
        current = rows[0].row() if rows else 0
        next_row = (current + delta) % len(recipe.points)
        self.recipe_point_table.selectRow(next_row)
        point = self._selected_recipe_point()
        if point is not None:
            item = self.recipe_point_table.item(next_row, 0)
            if item is not None:
                self.recipe_point_table.scrollToItem(item)
            self.gamepad_status.setText(
                f"Point selected: #{point.order} {point.name} · "
                f"Pan {point.pan:.2f} / Tilt {point.tilt:+.2f}"
            )

    def _toggle_gamepad_laser(self) -> None:
        if self.laser_is_on:
            self._laser_off()
            self.gamepad_status.setText("Laser OFF")
            return
        if not self.laser_arm.isChecked():
            self.gamepad_status.setText("Laser ARM required")
            self.gamepad_manager.rumble(0.2, 0.2, 120)
            return
        self._laser_on()
        self.gamepad_status.setText("Laser ON")

    def _gamepad_hat(self, state: GamepadState) -> tuple[int, int]:
        index = gamepad_map.HAT_INDEX
        if index is not None and 0 <= index < len(state.hats):
            return state.hats[index]
        x = int(self._button_value(gamepad_map.DPAD_RIGHT_BUTTON, state.buttons))
        x -= int(self._button_value(gamepad_map.DPAD_LEFT_BUTTON, state.buttons))
        y = int(self._button_value(gamepad_map.DPAD_UP_BUTTON, state.buttons))
        y -= int(self._button_value(gamepad_map.DPAD_DOWN_BUTTON, state.buttons))
        return x, y

    def _handle_gamepad_dpad(
        self,
        current_hat: tuple[int, int],
        previous_hat: tuple[int, int],
        current_buttons: tuple[bool, ...],
        previous_buttons: tuple[bool, ...],
    ) -> None:
        if gamepad_map.HAT_INDEX is not None:
            if current_hat == previous_hat:
                return
            pan_delta = gamepad_map.SPEED_STEP * current_hat[0]
            tilt_delta = gamepad_map.SPEED_STEP * current_hat[1]
        else:
            pan_delta = 0
            tilt_delta = 0
            if self._button_edge(
                gamepad_map.DPAD_LEFT_BUTTON, current_buttons, previous_buttons
            ):
                pan_delta -= gamepad_map.SPEED_STEP
            if self._button_edge(
                gamepad_map.DPAD_RIGHT_BUTTON, current_buttons, previous_buttons
            ):
                pan_delta += gamepad_map.SPEED_STEP
            if self._button_edge(
                gamepad_map.DPAD_DOWN_BUTTON, current_buttons, previous_buttons
            ):
                tilt_delta -= gamepad_map.SPEED_STEP
            if self._button_edge(
                gamepad_map.DPAD_UP_BUTTON, current_buttons, previous_buttons
            ):
                tilt_delta += gamepad_map.SPEED_STEP
        if pan_delta:
            self.pan_speed.setValue(self.pan_speed.value() + pan_delta)
        if tilt_delta:
            self.tilt_speed.setValue(self.tilt_speed.value() + tilt_delta)
        if pan_delta or tilt_delta:
            message = (
                f"D-pad 수동 속도 · Pan {self.pan_speed.value()}단계 "
                f"({manual_speed_percent(self.pan_speed.value())}%) / "
                f"Tilt {self.tilt_speed.value()}단계 "
                f"({manual_speed_percent(self.tilt_speed.value())}%)"
            )
            self.gamepad_status.setText(message)
            self._append_event("GAMEPAD", "", message)

    def _apply_gamepad_motion(self, axes: tuple[float, ...]) -> None:
        motion = axes_to_motion(
            axes,
            manual_speed_value(self.pan_speed.value()),
            manual_speed_value(self.tilt_speed.value()),
        )
        if self.gamepad_stop_latched:
            if motion is None:
                self.gamepad_stop_latched = False
                self.gamepad_status.setText("STOP 해제 · 제어 활성")
            return
        if self.current_jog is not None:
            return
        if motion is None:
            self._stop_gamepad_motion(send_stop=True)
            return

        pan, tilt, pan_speed, tilt_speed = motion
        if self.invert_pan.isChecked():
            pan = {
                PanDirection.LEFT: PanDirection.RIGHT,
                PanDirection.RIGHT: PanDirection.LEFT,
            }.get(pan, pan)
        if self.invert_tilt.isChecked():
            tilt = {
                TiltDirection.UP: TiltDirection.DOWN,
                TiltDirection.DOWN: TiltDirection.UP,
            }.get(tilt, tilt)
        motion = pan, tilt, pan_speed, tilt_speed
        now = time.perf_counter()

        previous = self.gamepad_motion
        direction_changed = (
            previous is None
            or previous[0] is not pan
            or previous[1] is not tilt
        )
        speed_changed = (
            previous is None
            or abs(previous[2] - pan_speed) >= 4
            or abs(previous[3] - tilt_speed) >= 4
        )
        # Raw analog axes fluctuate slightly even while the user's hand is still.
        # Respect Pelco-D command spacing for real speed changes, and resend a
        # stable command only every five seconds for runaway protection.
        elapsed = now - self.gamepad_last_send
        speed_update_due = speed_changed and elapsed >= PELCOD_MIN_DYNAMIC_UPDATE_S
        keepalive_due = previous is not None and elapsed >= (PELCOD_MANUAL_REFRESH_MS / 1000.0)
        should_send = direction_changed or speed_update_due or keepalive_due

        if should_send:
            self.api_jog_watchdog.stop()
            self.api_motion_owner = None
            self.api_jog_key = None
            self._cancel_motion_tracking("게임패드 Jog가 시작됨")

        if should_send and self._send(
            manual_motion(self._address(), pan, tilt, pan_speed, tilt_speed)
        ):
            self.gamepad_motion = motion
            self.gamepad_last_send = now
            self.gamepad_status.setText(
                f"Jog · Pan {pan.name} {pan_speed} / Tilt {tilt.name} {tilt_speed}"
            )

    def _stop_gamepad_motion(self, *, send_stop: bool) -> None:
        was_moving = self.gamepad_motion is not None
        self.gamepad_motion = None
        self.gamepad_last_send = 0.0
        if was_moving and send_stop and self.is_connected:
            self._send(stop(self._address()))

    def _refresh_ports(self) -> None:
        previous = self.port_combo.currentData() or getattr(self, "_saved_port", "")
        self.port_combo.clear()
        for info in sorted(list_ports.comports(), key=lambda item: item.device):
            description = info.description or "Serial Port"
            self.port_combo.addItem(f"{info.device} — {description}", info.device)
        if previous:
            index = self.port_combo.findData(previous)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        if self.port_combo.count() == 0:
            self.port_combo.addItem("사용 가능한 COM 포트 없음", None)

    def _toggle_connection(self) -> None:
        if self.is_connected:
            self.monitor_check.setChecked(False)
            if self.laser_is_on:
                self.worker.request_send(
                    set_auxiliary(self._address(), self.aux_number.value(), False)
                )
            self.worker.request_close()
            return
        port = self.port_combo.currentData()
        if not port:
            QMessageBox.warning(self, "COM 포트", "사용할 COM 포트를 선택하세요.")
            return
        config = SerialConfig(port, int(self.baud_combo.currentData()), self._address())
        self.connect_button.setEnabled(False)
        self.worker.request_open(config)

    def _send(self, command: OutgoingCommand, *, silent: bool = False) -> bool:
        if not self.is_connected:
            self.statusBar().showMessage("먼저 COM 포트를 연결하세요.", 3000)
            return False
        if self.is_scanning:
            self.statusBar().showMessage(
                "자동검색 중에는 조작 명령을 보낼 수 없습니다.", 3000
            )
            return False
        if silent:
            command = OutgoingCommand(
                command.data,
                SILENT_PREFIX + command.description,
                command.category,
            )
        self.worker.request_send(command)
        return True

    def _address(self) -> int:
        return self.address_spin.value()

    def _on_connected(self, port: str, baudrate: int, address: int) -> None:
        self.is_scanning = False
        self.is_connected = True
        self.connect_button.setEnabled(True)
        self.connect_button.setText("연결 해제")
        self._set_connected(True, f"연결됨 · {port} · {baudrate}bps · 주소 {address}")
        self._append_event(
            "SYSTEM", "", f"COM 연결 성공: {port}, {baudrate}bps, 주소 {address}"
        )
        self.api_server.broadcast(
            "serial.connected",
            {"port": port, "baudrate": baudrate, "address": address},
        )
        self._save_settings()

        self._auto_connect_and_enable_gamepad()

    def _on_disconnected(self, reason: str) -> None:
        self.is_connected = False
        self.api_jog_watchdog.stop()
        self.api_motion_owner = None
        self.api_jog_key = None
        self._stop_gamepad_motion(send_stop=False)
        if hasattr(self, "gamepad_enable"):
            self.gamepad_enable.setChecked(False)
        self.monitor_timer.stop()
        self.laser_timer.stop()
        self.jog_keepalive.stop()
        self.current_jog = None
        self._cancel_motion_tracking("시리얼 연결 해제")
        self._abort_drawing_upload("COM 연결 해제")
        self.laser_is_on = False
        self.api_laser_owner = None
        if hasattr(self, "laser_state_label"):
            self.laser_state_label.setText("출력 상태: OFF (연결 해제)")
        if hasattr(self, "monitor_check"):
            self.monitor_check.blockSignals(True)
            self.monitor_check.setChecked(False)
            self.monitor_check.blockSignals(False)
        self.connect_button.setEnabled(True)
        self.connect_button.setText("연결")
        self._set_connected(False, reason)
        self._append_event("SYSTEM", "", f"COM 연결 해제: {reason}")
        self.api_server.broadcast("serial.disconnected", {"reason": reason})

    def _on_error(self, message: str) -> None:
        self._append_event("ERROR", "", message)
        self.statusBar().showMessage(message, 5000)

    def _set_connected(self, connected: bool, text: str) -> None:
        # Inputs remain editable while disconnected; _send() is the safety gate.
        # This also keeps every spin-box arrow functional before connecting.
        self.tabs.setEnabled(True)
        self.connection_status.setText(text)
        self.connection_status.setStyleSheet(
            "color:#79d98b; font-weight:700"
            if connected
            else "color:#f09b75; font-weight:700"
        )
        self.port_combo.setEnabled(not connected and not self.is_scanning)
        self.baud_combo.setEnabled(not connected and not self.is_scanning)
        self.protocol_combo.setEnabled(not connected and not self.is_scanning)
        self.address_spin.setEnabled(not connected and not self.is_scanning)
        self.refresh_button.setEnabled(not connected and not self.is_scanning)
        self.search_button.setEnabled(not connected and not self.is_scanning)

    # ---------- Motion and functional commands ----------
    def _start_auto_scan(self, mode: str) -> None:
        command = (
            vendor_line_scan(self._address(), True)
            if mode == "vendor"
            else zone_scan(self._address(), True)
        )
        if self._send(set_scan_speed(self._address(), AUTO_PAN_SPEED, AUTO_TILT_SPEED)):
            QTimer.singleShot(350, lambda: self._send(command))

    def _start_auto_cruise(self) -> None:
        track = self.cruise_track.value()
        if self._send(
            set_cruise_speed(self._address(), AUTO_PAN_SPEED, AUTO_TILT_SPEED)
        ):
            QTimer.singleShot(
                350, lambda: self._send(start_cruise(self._address(), track))
            )

    def _start_jog(self, pan: PanDirection, tilt: TiltDirection) -> None:
        self.api_jog_watchdog.stop()
        self.api_motion_owner = None
        self._cancel_motion_tracking("UI Jog가 시작됨")
        if self.invert_pan.isChecked():
            pan = {
                PanDirection.LEFT: PanDirection.RIGHT,
                PanDirection.RIGHT: PanDirection.LEFT,
            }.get(pan, pan)
        if self.invert_tilt.isChecked():
            tilt = {
                TiltDirection.UP: TiltDirection.DOWN,
                TiltDirection.DOWN: TiltDirection.UP,
            }.get(tilt, tilt)
        self.current_jog = (pan, tilt)
        # Send immediately, then refresh once per second while the operator keeps
        # manual JOG active. Release/STOP remains the authoritative stop condition.
        self._repeat_jog()
        if self.is_connected:
            self.jog_keepalive.start()

    def _repeat_jog(self) -> None:
        if not self.current_jog:
            return
        pan, tilt = self.current_jog
        self._send(
            manual_motion(
                self._address(),
                pan,
                tilt,
                manual_speed_value(self.pan_speed.value()),
                manual_speed_value(self.tilt_speed.value()),
            )
        )

    def _end_jog(self) -> None:
        self.current_jog = None
        self.jog_keepalive.stop()
        self._send(stop(self._address()))

    def _emergency_stop(self) -> None:
        self.current_jog = None
        self.gamepad_motion = None
        self.gamepad_last_send = 0.0
        self.api_jog_watchdog.stop()
        self.api_motion_owner = None
        self.api_jog_key = None
        self.jog_keepalive.stop()
        self._cancel_motion_tracking("STOP 명령")
        self._refresh_monitor_timer()
        self._send(stop(self._address()))

    def _cancel_motion_tracking(self, reason: str) -> None:
        if self.motion_tracker is None:
            return
        context = self.api_motion_context
        label = self.motion_tracker.label
        self.motion_tracker = None
        self.motion_callback = None
        self.api_motion_context = None
        self._append_event(
            "CANCEL", "", f"{reason}으로 {label} 완료 판정을 취소했습니다."
        )
        if context is not None:
            self.api_server.broadcast(
                "motion.cancelled",
                {
                    **context,
                    "label": label,
                    "reason": reason,
                    "pan": self.current_pan,
                    "tilt": self.current_tilt,
                },
            )

    def _move_pan(self) -> None:
        target = self.target_pan.value()
        if not self.is_connected:
            self._send(set_pan_position(self._address(), target))
            return
        delay = self._send_auto_position_speed()

        def send_move() -> None:
            if self._send(set_pan_position(self._address(), target)):
                self._begin_target_tracking("Pan 절대위치 이동", pan=target)

        QTimer.singleShot(delay, send_move)

    def _move_tilt(self) -> None:
        target = self.target_tilt.value()
        if not self.is_connected:
            self._send(set_tilt_position(self._address(), target))
            return
        delay = self._send_auto_position_speed()

        def send_move() -> None:
            if self._send(set_tilt_position(self._address(), target)):
                self._begin_target_tracking("Tilt 절대위치 이동", tilt=target)

        QTimer.singleShot(delay, send_move)

    def _move_both(self) -> None:
        self._move_to_target_position(
            self.target_pan.value(),
            self.target_tilt.value(),
            label="Pan/Tilt target move",
            cancel_reason="UI target move",
        )
        return
        if not self.is_connected:
            self._send(set_pan_position(self._address(), self.target_pan.value()))
            return
        target_pan = self.target_pan.value()
        target_tilt = self.target_tilt.value()
        delay = self._send_auto_position_speed()
        QTimer.singleShot(
            delay,
            lambda: self._send(set_pan_position(self._address(), target_pan)),
        )

        def send_tilt_and_track() -> None:
            if self._send(set_tilt_position(self._address(), target_tilt)):
                self._begin_target_tracking(
                    "Pan/Tilt 절대위치 이동", pan=target_pan, tilt=target_tilt
                )

        QTimer.singleShot(delay + 350, send_tilt_and_track)

    def _send_auto_position_speed(self) -> int:
        sent = self._send(auto_position_speed(self._address()))
        return 350 if sent else 0

    def _query_all_positions(self) -> None:
        self._send(query_pan(self._address()))
        QTimer.singleShot(350, lambda: self._send(query_tilt(self._address())))

    def _toggle_monitoring(self, enabled: bool) -> None:
        if enabled and self.is_connected:
            self.monitor_axis = "pan"
            self._refresh_monitor_timer()
            self._monitor_tick()
        else:
            self._refresh_monitor_timer()

    def _update_monitor_interval(self, value: int) -> None:
        self._refresh_monitor_timer()

    def _monitor_tick(self) -> None:
        if not self.is_connected or self.is_scanning:
            return

        # Keep the RS-485 bus quiet during manual continuous motion.  PT503/PT510
        # can visibly hesitate when position queries are interleaved with JOG.
        if (
            self.current_jog is not None
            or self.gamepad_motion is not None
            or self.api_motion_owner is not None
        ):
            return

        if self.motion_tracker is not None:
            self._handle_tracker_result(self.motion_tracker.check_timeout())
            if self.motion_tracker is None and not self.monitor_check.isChecked():
                self._refresh_monitor_timer()
                return
        if self.monitor_axis == "pan":
            self._send(query_pan(self._address()), silent=True)
            self.monitor_axis = "tilt"
        else:
            self._send(query_tilt(self._address()), silent=True)
            self.monitor_axis = "pan"

    def _refresh_monitor_timer(self) -> None:
        should_run = self.is_connected and (
            self.monitor_check.isChecked() or self.motion_tracker is not None
        )
        if not should_run:
            self.monitor_timer.stop()
            return
        interval = self.monitor_interval.value()
        if self.motion_tracker is not None:
            interval = min(interval, 350)
        self.monitor_timer.start(interval)

    def _begin_target_tracking(
        self,
        label: str,
        *,
        pan: float | None = None,
        tilt: float | None = None,
        settle_mode: bool = False,
        callback: Callable[[], None] | None = None,
        api_context: dict[str, Any] | None = None,
    ) -> None:
        self.api_jog_watchdog.stop()
        self.api_motion_owner = None
        self._cancel_motion_tracking("새 위치이동 명령")
        self.motion_tracker = MotionTracker(
            label=label,
            target_pan=pan,
            target_tilt=tilt,
            tolerance=self.completion_tolerance.value(),
            stable_samples=self.completion_samples.value(),
            timeout_seconds=float(self.completion_timeout.value()),
            settle_mode=settle_mode,
        )
        self.motion_callback = callback
        self.api_motion_context = api_context
        target_text = (
            "정지 여부만 추정"
            if settle_mode
            else f"목표 Pan={pan if pan is not None else '-'}, Tilt={tilt if tilt is not None else '-'}"
        )
        self.command_state.setText(f"완료 판정 중: {label}")
        self._append_event(
            "TRACK",
            "",
            f"{label} 완료 판정 시작 · {target_text} · 허용오차 "
            f"±{self.completion_tolerance.value():.2f}° × {self.completion_samples.value()}회",
        )
        self.monitor_axis = "pan"
        self._refresh_monitor_timer()
        self._monitor_tick()

    def _handle_tracker_result(self, result: TrackerResult) -> None:
        if result.status == "tracking" or self.motion_tracker is None:
            return
        callback = self.motion_callback
        context = self.api_motion_context
        self.motion_tracker = None
        self.motion_callback = None
        self.api_motion_context = None
        if result.status == "completed":
            direction = "DONE"
            self.command_state.setText("이동 완료 추정")
            self.gamepad_manager.rumble(0.2, 0.5, 120)
        elif result.status == "settled":
            direction = "SETTLED"
            self.command_state.setText("정지 추정(목표 검증 불가)")
            self.gamepad_manager.rumble(0.2, 0.4, 120)
        else:
            direction = "TIMEOUT"
            self.command_state.setText("완료 확인 타임아웃")
            self.gamepad_manager.rumble(0.7, 0.7, 350)
            callback = None
            self._abort_drawing_upload("이동 완료 확인 타임아웃")
        self._append_event(direction, "", result.message)
        if context is not None:
            event_name = {
                "completed": "motion.completed",
                "settled": "motion.settled",
                "timeout": "motion.timeout",
            }.get(result.status, "motion.finished")
            self.api_server.broadcast(
                event_name,
                {
                    **context,
                    "status": result.status,
                    "message": result.message,
                    "pan": self.current_pan,
                    "tilt": self.current_tilt,
                },
            )
        self._refresh_monitor_timer()
        if callback is not None:
            callback()

    def _update_preset_display(self, _value: int | None = None) -> None:
        if not hasattr(self, "preset_coordinate_label"):
            return
        record = self.preset_store.get(self._address(), self.preset_number.value())
        if record is None:
            self.preset_coordinate_label.setText(
                "앱에 저장된 좌표 없음 · 장비 내부 프리셋 좌표는 Pelco-D로 역조회할 수 없습니다."
            )
            return
        pan = float(record["pan"])
        tilt = float(record["tilt"])
        self.preset_coordinate_label.setText(
            f"앱 저장 좌표: Pan {pan:.2f}° / Tilt {tilt:+.2f}° · {record.get('updated', '')}"
        )
        self.preset_pan.setValue(pan)
        self.preset_tilt.setValue(tilt)

    def _save_current_preset(self) -> None:
        if not self.is_connected:
            self._send(set_preset(self._address(), self.preset_number.value()))
            return
        self.preset_capture = {
            "address": self._address(),
            "preset": self.preset_number.value(),
            "pan": None,
            "tilt": None,
        }
        capture = self.preset_capture
        self.command_state.setText("현재 좌표 확인 후 프리셋 저장 중")
        self._send(query_pan(self._address()))
        QTimer.singleShot(180, lambda: self._send(query_tilt(self._address())))
        QTimer.singleShot(3000, lambda: self._expire_preset_capture(capture))

    def _expire_preset_capture(self, capture: dict[str, object]) -> None:
        if self.preset_capture is capture:
            self.preset_capture = None
            self.command_state.setText("프리셋 저장 실패: 위치 응답 타임아웃")
            self._append_event(
                "TIMEOUT",
                "",
                "현재 Pan/Tilt 위치 응답이 없어 프리셋 저장을 취소했습니다.",
            )

    def _finish_preset_capture_if_ready(self) -> None:
        capture = self.preset_capture
        if capture is None or capture["pan"] is None or capture["tilt"] is None:
            return
        self.preset_capture = None
        address = int(capture["address"])
        preset = int(capture["preset"])
        pan = float(capture["pan"])
        tilt = float(capture["tilt"])
        if self._send(set_preset(address, preset)):
            self.preset_store.set(address, preset, pan, tilt)
            self._append_event(
                "PRESET",
                "",
                f"프리셋 {preset} 로컬 좌표 기록: Pan {pan:.2f}°, Tilt {tilt:+.2f}°",
            )
            self.command_state.setText(f"프리셋 {preset} 저장 명령 송신")
            self._update_preset_display()

    def _call_selected_preset(self) -> None:
        number = self.preset_number.value()
        if not self._send(call_preset(self._address(), number)):
            return
        record = self.preset_store.get(self._address(), number)
        if record is None:
            self._begin_target_tracking(f"프리셋 {number} 이동", settle_mode=True)
        else:
            self._begin_target_tracking(
                f"프리셋 {number} 이동",
                pan=float(record["pan"]),
                tilt=float(record["tilt"]),
            )

    def _move_and_save_preset(self) -> None:
        if not self.is_connected:
            self.statusBar().showMessage("먼저 COM 포트를 연결하세요.", 3000)
            return
        address = self._address()
        number = self.preset_number.value()
        pan = self.preset_pan.value()
        tilt = self.preset_tilt.value()
        delay = self._send_auto_position_speed()
        QTimer.singleShot(delay, lambda: self._send(set_pan_position(address, pan)))

        def save_after_arrival() -> None:
            if self._send(set_preset(address, number)):
                self.preset_store.set(address, number, pan, tilt)
                self._append_event(
                    "PRESET",
                    "",
                    f"프리셋 {number} 저장 완료: Pan {pan:.2f}°, Tilt {tilt:+.2f}°",
                )
                self._update_preset_display()

        def send_tilt_and_track() -> None:
            if self._send(set_tilt_position(address, tilt)):
                self._begin_target_tracking(
                    f"프리셋 {number} 좌표 이동",
                    pan=pan,
                    tilt=tilt,
                    callback=save_after_arrival,
                )

        QTimer.singleShot(delay + 350, send_tilt_and_track)

    def _clear_preset_confirmed(self) -> None:
        number = self.preset_number.value()
        answer = QMessageBox.question(
            self,
            "프리셋 삭제",
            f"프리셋 {number}을 삭제할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes and self._send(
            clear_preset(self._address(), number)
        ):
            self.preset_store.delete(self._address(), number)
            self._update_preset_display()

    def _laser_arm_changed(self, armed: bool) -> None:
        self.laser_on_button.setEnabled(armed)
        self.laser_pulse_button.setEnabled(armed)
        if not armed and self.laser_is_on:
            self._laser_off()
        self.api_server.broadcast(
            "laser.state",
            {
                "armed": armed,
                "on": self.laser_is_on,
                "aux_number": self.aux_number.value(),
            },
        )

    def _laser_on(self) -> None:
        if not self.laser_arm.isChecked():
            self.statusBar().showMessage(
                "레이저 출력 허용(ARM)을 먼저 선택하세요.", 3000
            )
            return
        self.laser_timer.stop()
        if self._send(set_auxiliary(self._address(), self.aux_number.value(), True)):
            self.laser_is_on = True
            self.laser_state_label.setText("출력 상태: ON (앱 추정)")
            self.laser_state_label.setStyleSheet("color:#ff7675; font-weight:800")
            self.api_server.broadcast(
                "laser.state",
                {
                    "armed": self.laser_arm.isChecked(),
                    "on": True,
                    "aux_number": self.aux_number.value(),
                    "owner": self.api_laser_owner,
                },
            )

    def _laser_off(self) -> None:
        self.laser_timer.stop()
        if self._send(set_auxiliary(self._address(), self.aux_number.value(), False)):
            self.laser_is_on = False
            self.api_laser_owner = None
            self.laser_state_label.setText("출력 상태: OFF (앱 추정)")
            self.laser_state_label.setStyleSheet("color:#75e09a; font-weight:700")
            self.api_server.broadcast(
                "laser.state",
                {
                    "armed": self.laser_arm.isChecked(),
                    "on": False,
                    "aux_number": self.aux_number.value(),
                },
            )

    def _laser_pulse(self) -> None:
        self._laser_on()
        if self.laser_is_on:
            self.laser_timer.start(self.laser_pulse_ms.value())

    def _send_raw(self) -> None:
        try:
            command = parse_hex_command(self.raw_hex.text(), self._address())
        except ProtocolError as exc:
            QMessageBox.warning(self, "HEX 명령 오류", str(exc))
            return
        self.raw_hex.setText(frame_to_hex(command.data))
        self._send(command)

    def _self_check_confirmed(self) -> None:
        if self._confirm_motion(
            "Self-check를 실행하면 Pan/Tilt가 자동으로 움직일 수 있습니다. 실행할까요?"
        ):
            self._send(self_check(self._address()))

    def _set_power_on_self_check(self, enabled: bool) -> None:
        state = "ON" if enabled else "OFF"
        if self._confirm_motion(
            f"전원 인가 Self-check {state} 명령은 맞춤형 펌웨어용이며 지원 여부를 "
            "조회할 수 없습니다. 전송 후 전원을 재인가해 직접 확인해야 합니다. 전송할까요?"
        ):
            self._send(power_on_self_check(self._address(), enabled))

    def _restart_confirmed(self) -> None:
        if self._confirm_motion(
            "PT503을 원격 재시작할까요? 일시적으로 통신이 끊길 수 있습니다."
        ):
            self.monitor_check.setChecked(False)
            self._send(remote_restart(self._address()))

    def _factory_default_confirmed(self) -> None:
        answer = QMessageBox.warning(
            self,
            "공장 초기화",
            "주소·속도·프리셋 등 설정이 초기화될 수 있습니다. 정말 전송할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.monitor_check.setChecked(False)
            self._send(factory_default(self._address()))

    def _confirm_motion(self, text: str) -> bool:
        return (
            QMessageBox.question(
                self,
                "안전 확인",
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    # ---------- Automatic discovery ----------
    def _set_baudrate(self, baudrate: int) -> None:
        baud_index = self.baud_combo.findData(baudrate)
        if baud_index >= 0:
            self.baud_combo.setCurrentIndex(baud_index)

    def _begin_serial_scan(
        self,
        ports: list[str],
        baudrates: list[int],
        first_address: int,
        last_address: int,
        timeout_ms: int,
        *,
        show_progress_dialog: bool,
        notify_on_failure: bool,
        status: str,
    ) -> bool:
        ports = [port for port in ports if port]
        if not ports or self.is_connected or self.is_scanning:
            return False
        self.scan_progress_dialog_enabled = show_progress_dialog
        self.scan_notify_on_failure = notify_on_failure
        self.api_scan_context = None
        self.is_scanning = True
        self._set_connected(False, status)
        self.connect_button.setEnabled(False)
        self.worker.request_scan(
            ports,
            baudrates,
            first_address,
            last_address,
            timeout_ms,
        )
        return True

    def _start_startup_scan(self) -> None:
        if self.is_connected or self.is_scanning:
            return
        self._refresh_ports()
        ports = [info.device for info in list_ports.comports()]
        if not ports:
            self._set_connected(False, "자동검색 대기 · 검색된 COM 포트 없음")
            self._append_event("SYSTEM", "", "시작 자동검색 건너뜀: 검색된 COM 포트 없음")
            return
        self._set_baudrate(STARTUP_BAUDRATE)
        address = self.address_spin.value()
        started = self._begin_serial_scan(
            ports,
            [STARTUP_BAUDRATE],
            address,
            address,
            STARTUP_SCAN_TIMEOUT_MS,
            show_progress_dialog=False,
            notify_on_failure=False,
            status=f"시작 자동검색 준비 · {STARTUP_BAUDRATE}bps · 주소 {address}",
        )
        if started:
            self._append_event(
                "SYSTEM",
                "",
                f"시작 자동검색: {len(ports)}개 COM, {STARTUP_BAUDRATE}bps, 주소 {address}",
            )

    def _start_search(self) -> None:
        all_ports = [info.device for info in list_ports.comports()]
        selected = self.port_combo.currentData()
        if not all_ports:
            QMessageBox.warning(self, "자동검색", "검색할 COM 포트가 없습니다.")
            return
        dialog = SearchDialog(self, len(all_ports))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        ports = all_ports if dialog.scope.currentData() == "all" else [selected]
        ports = [port for port in ports if port]
        self._begin_serial_scan(
            ports,
            dialog.selected_bauds(),
            dialog.first_address.value(),
            dialog.last_address.value(),
            dialog.timeout_ms.value(),
            show_progress_dialog=True,
            notify_on_failure=True,
            status="auto scan ready",
        )
        return

    def _on_scan_started(self, attempts: int) -> None:
        if self.api_scan_context is None and self.scan_progress_dialog_enabled:
            self.search_progress = QProgressDialog(
                "장비 응답 검색 중...", "취소", 0, attempts, self
            )
            self.search_progress.setWindowTitle("PT503 안전 자동검색")
            self.search_progress.setWindowModality(Qt.WindowModality.WindowModal)
            self.search_progress.setMinimumDuration(0)
            self.search_progress.setAutoClose(False)
            self.search_progress.canceled.connect(self.worker.cancel_scan)
            self.search_progress.show()
        elif self.api_scan_context is not None:
            self.api_server.broadcast(
                "serial.scan_started",
                {**self.api_scan_context, "attempts": attempts},
            )
        self._append_event("SYSTEM", "", f"자동검색 시작: 최대 {attempts}회 위치 조회")

    def _on_scan_progress(self, current: int, total: int, label: str) -> None:
        if self.search_progress:
            self.search_progress.setMaximum(total)
            self.search_progress.setValue(current)
            self.search_progress.setLabelText(f"{label}\n{current}/{total}")
        self.connection_status.setText(f"자동검색 {current}/{total} · {label}")
        if self.api_scan_context is not None:
            self.api_server.broadcast(
                "serial.scan_progress",
                {
                    **self.api_scan_context,
                    "current": current,
                    "total": total,
                    "label": label,
                },
            )

    def _on_scan_found(self, port: str, baudrate: int, address: int) -> None:
        self._refresh_ports()
        port_index = self.port_combo.findData(port)
        if port_index >= 0:
            self.port_combo.setCurrentIndex(port_index)
        baud_index = self.baud_combo.findData(baudrate)
        if baud_index >= 0:
            self.baud_combo.setCurrentIndex(baud_index)
        self.address_spin.setValue(address)
        if self.api_scan_context is not None:
            self.api_server.broadcast(
                "serial.scan_found",
                {
                    **self.api_scan_context,
                    "port": port,
                    "baudrate": baudrate,
                    "address": address,
                },
            )

    def _on_scan_finished(self, found: bool, message: str) -> None:
        context = self.api_scan_context
        self.api_scan_context = None
        self.is_scanning = False
        if self.search_progress:
            self.search_progress.close()
            self.search_progress = None
        self.connect_button.setEnabled(True)
        if not found:
            self._set_connected(False, message)
        self._append_event("SYSTEM", "", message)
        if context is not None:
            self.api_server.broadcast(
                "serial.scan_finished",
                {**context, "found": found, "message": message},
            )
        elif not found and self.scan_notify_on_failure:
            QMessageBox.information(self, "자동검색", message)

        self.scan_progress_dialog_enabled = True
        self.scan_notify_on_failure = True

    # ---------- Communication logging ----------
    def _on_tx(self, data: bytes, description: str, sent_at: float) -> None:
        silent = description.startswith(SILENT_PREFIX)
        clean_description = description[len(SILENT_PREFIX) :] if silent else description
        self.last_tx_time = sent_at
        self.last_tx_checksum = data[6] if len(data) == 7 else None
        self.last_tx_silent = silent
        self.last_tx_description = clean_description
        self.last_tx_frames.append((bytes(data), silent))
        self.last_tx_frames = self.last_tx_frames[-10:]
        if len(data) == 7:
            self.recent_tx_meta.append((data[6], sent_at, silent, clean_description))
            self.recent_tx_meta = self.recent_tx_meta[-30:]
            expected = {
                0x51: 0x59,
                0x53: 0x5B,
                0x55: 0x5D,
                0x65: 0x6D,
                0x6B: 0x6D,
            }.get(data[3])
            if expected is not None:
                self.pending_response[(data[1], expected)] = (
                    sent_at,
                    silent,
                    clean_description,
                )
        if silent:
            return
        self._append_log("TX", data, clean_description, "", "OK")
        self.command_state.setText(f"명령 송신: {clean_description}")
        self.last_activity.setText(
            f"마지막 통신: TX {datetime.now().astimezone().strftime('%H:%M:%S')}"
        )

    def _on_rx_blob(self, blob: bytes, received_at: float) -> None:
        visible_received = False
        for frame in split_response_blob(blob):
            echo_silent = next(
                (
                    silent
                    for sent_frame, silent in reversed(self.last_tx_frames)
                    if sent_frame == frame
                ),
                None,
            )
            if echo_silent is not None and len(frame) == 7:
                if not echo_silent:
                    self._append_log("RX/ECHO", frame, "송신 프레임 Echo", "", "OK")
                    visible_received = True
                continue
            general_meta: tuple[int, float, bool, str] | None = None
            if len(frame) == 4:
                alarm, received_checksum = frame[2], frame[3]
                general_meta = next(
                    (
                        meta
                        for meta in reversed(self.recent_tx_meta)
                        if ((meta[0] + alarm) & 0xFF) == received_checksum
                    ),
                    None,
                )
            checksum_for_decode = (
                general_meta[0] if general_meta is not None else self.last_tx_checksum
            )
            response = decode_response(frame, checksum_for_decode)
            elapsed = ""
            silent = False
            related_description = ""
            if len(frame) == 7:
                pending = self.pending_response.pop((frame[1], frame[3]), None)
                if pending is not None:
                    sent_at, silent, related_description = pending
                    elapsed = f"{(received_at - sent_at) * 1000.0:.1f}"
            elif self.last_tx_time:
                if general_meta is not None:
                    _, sent_at, silent, related_description = general_meta
                    elapsed = f"{(received_at - sent_at) * 1000.0:.1f}"
                else:
                    elapsed = f"{(received_at - self.last_tx_time) * 1000.0:.1f}"
                    silent = self.last_tx_silent

            checksum_text = {
                True: "OK",
                False: "FAIL",
                None: "N/A",
            }[response.checksum_ok]
            description = response.description
            if response.kind == "general" and response.checksum_ok:
                description = (
                    f"ACK: 장비가 명령을 수신·처리함(이동 완료 신호 아님) · "
                    f"관련 명령: {related_description or self.last_tx_description} · {description}"
                )
                if not silent:
                    self.command_state.setText("ACK 수신(명령 접수, 이동 완료 아님)")
                    self.api_server.broadcast(
                        "device.ack",
                        {
                            "command": related_description or self.last_tx_description,
                            "checksum_ok": True,
                            "alarm": response.value,
                            "hex": frame_to_hex(frame),
                        },
                    )
            elif related_description and response.kind == "focus_or_device":
                description = f"{related_description}에 대한 응답 · {description}"

            if not silent:
                self._append_log("RX", frame, description, elapsed, checksum_text)
                visible_received = True
            if response.kind == "pan" and isinstance(response.value, float):
                self.current_pan = response.value
                self.pan_value.setText(f"{response.value:07.2f}°")
                self.api_server.broadcast(
                    "position.updated",
                    {
                        "axis": "pan",
                        "pan": self.current_pan,
                        "tilt": self.current_tilt,
                    },
                )
                self._capture_preset_axis("pan", response.value, response.address)
                self._capture_drawing_axis("pan", response.value, response.address)
                if self.motion_tracker is not None:
                    self._handle_tracker_result(
                        self.motion_tracker.update("pan", response.value, received_at)
                    )
            elif response.kind == "tilt" and isinstance(response.value, float):
                self.current_tilt = response.value
                self.tilt_value.setText(f"{response.value:+07.2f}°")
                self.api_server.broadcast(
                    "position.updated",
                    {
                        "axis": "tilt",
                        "pan": self.current_pan,
                        "tilt": self.current_tilt,
                    },
                )
                self._capture_preset_axis("tilt", response.value, response.address)
                self._capture_drawing_axis("tilt", response.value, response.address)
                if self.motion_tracker is not None:
                    self._handle_tracker_result(
                        self.motion_tracker.update("tilt", response.value, received_at)
                    )
            elif not silent:
                self.api_server.broadcast(
                    "device.response",
                    {
                        "kind": response.kind,
                        "value": response.value,
                        "description": response.description,
                        "checksum_ok": response.checksum_ok,
                        "hex": frame_to_hex(frame),
                    },
                )
        if visible_received:
            self.last_activity.setText(
                f"마지막 통신: RX {datetime.now().astimezone().strftime('%H:%M:%S')}"
            )

    def _capture_preset_axis(
        self, axis: str, value: float, response_address: int | None
    ) -> None:
        capture = self.preset_capture
        if capture is None or response_address != int(capture["address"]):
            return
        capture[axis] = value
        self._finish_preset_capture_if_ready()

    def _append_event(self, direction: str, hex_data: str, description: str) -> None:
        self._append_log(
            direction, bytes.fromhex(hex_data) if hex_data else b"", description, "", ""
        )

    def _append_log(
        self,
        direction: str,
        data: bytes,
        description: str,
        elapsed_ms: str,
        checksum: str,
    ) -> None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        entry = LogEntry(
            timestamp=timestamp,
            direction=direction,
            hex_data=frame_to_hex(data),
            description=description,
            elapsed_ms=elapsed_ms,
            checksum=checksum,
        )
        self.log_store.append(entry)

        row = self.log_table.rowCount()
        self.log_table.insertRow(row)
        values = (
            entry.timestamp.split(" ", 1)[1],
            entry.direction,
            entry.hex_data,
            entry.description,
            entry.elapsed_ms,
            entry.checksum,
        )
        color = {
            "TX": QColor("#74b9ff"),
            "RX": QColor("#75e09a"),
            "RX/ECHO": QColor("#b7a5e6"),
            "DONE": QColor("#55efc4"),
            "SETTLED": QColor("#81ecec"),
            "TIMEOUT": QColor("#ff9f43"),
            "TRACK": QColor("#a29bfe"),
            "PRESET": QColor("#55efc4"),
            "CANCEL": QColor("#fab1a0"),
            "GAMEPAD": QColor("#fdcb6e"),
            "API": QColor("#81ecec"),
            "API RX": QColor("#a29bfe"),
            "API TX": QColor("#74b9ff"),
            "EVENT": QColor("#55efc4"),
            "ERROR": QColor("#ff7675"),
            "SYSTEM": QColor("#f5c06b"),
        }.get(direction, QColor("#e7edf5"))
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setForeground(QBrush(color))
            if column in (1, 4, 5):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.log_table.setItem(row, column, item)
        if self.log_table.rowCount() > self.MAX_LOG_ROWS:
            self.log_table.removeRow(0)
        if self.auto_scroll.isChecked():
            self.log_table.scrollToBottom()

    def _clear_log_view(self) -> None:
        self.log_table.setRowCount(0)
        self.log_store.clear_memory()
        self._append_event(
            "SYSTEM", "", "로그 화면을 지웠습니다. 기존 자동 로그 파일은 유지됩니다."
        )

    def _export_csv(self) -> None:
        default = str(
            Path.home()
            / f"PT503_log_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "통신 로그 CSV 저장", default, "CSV (*.csv)"
        )
        if not path:
            return
        try:
            self.log_store.export_csv(path)
        except OSError as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))
            return
        self.statusBar().showMessage(f"CSV 저장 완료: {path}", 5000)

    def _open_log_folder(self) -> None:
        folder = str(self.log_store.log_dir)
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError as exc:
            QMessageBox.warning(self, "폴더 열기 실패", str(exc))

    # ---------- Settings and shutdown ----------
    def _load_settings(self) -> None:
        baud = int(self.settings.value("serial/baud", STARTUP_BAUDRATE))
        baud_index = self.baud_combo.findData(baud)
        if baud_index >= 0:
            self.baud_combo.setCurrentIndex(baud_index)
        self._set_baudrate(STARTUP_BAUDRATE)
        self.address_spin.setValue(int(self.settings.value("serial/address", 1)))
        self.monitor_interval.setValue(
            int(self.settings.value("monitor/interval", 500))
        )
        self.completion_tolerance.setValue(
            float(self.settings.value("monitor/completion_tolerance", 0.20))
        )
        self.completion_samples.setValue(
            int(self.settings.value("monitor/completion_samples", 3))
        )
        self.completion_timeout.setValue(
            int(self.settings.value("monitor/completion_timeout", 30))
        )
        self.pan_speed.setValue(int(self.settings.value("motion/pan_speed_level", 5)))
        self.tilt_speed.setValue(int(self.settings.value("motion/tilt_speed_level", 5)))
        self.invert_pan.setChecked(
            str(self.settings.value("motion/invert_pan", "false")).lower() == "true"
        )
        self.invert_tilt.setChecked(
            str(self.settings.value("motion/invert_tilt", "false")).lower() == "true"
        )
        self.aux_number.setValue(int(self.settings.value("laser/aux_number", 1)))
        self.laser_pulse_ms.setValue(int(self.settings.value("laser/pulse_ms", 500)))
        self.api_port.setValue(int(self.settings.value("api/port", DEFAULT_PORT)))
        self.api_auto_start.setChecked(
            str(self.settings.value("api/auto_start", "true")).lower() == "true"
        )
        self._saved_port = str(self.settings.value("serial/port", ""))
        self._refresh_recipe_combo()

    def _save_settings(self) -> None:
        self.settings.setValue("serial/port", self.port_combo.currentData() or "")
        self.settings.setValue("serial/baud", int(self.baud_combo.currentData()))
        self.settings.setValue("serial/address", self.address_spin.value())
        self.settings.setValue("monitor/interval", self.monitor_interval.value())
        self.settings.setValue(
            "monitor/completion_tolerance", self.completion_tolerance.value()
        )
        self.settings.setValue(
            "monitor/completion_samples", self.completion_samples.value()
        )
        self.settings.setValue(
            "monitor/completion_timeout", self.completion_timeout.value()
        )
        self.settings.setValue("motion/pan_speed_level", self.pan_speed.value())
        self.settings.setValue("motion/tilt_speed_level", self.tilt_speed.value())
        self.settings.setValue("motion/invert_pan", self.invert_pan.isChecked())
        self.settings.setValue("motion/invert_tilt", self.invert_tilt.isChecked())
        self.settings.setValue("laser/aux_number", self.aux_number.value())
        self.settings.setValue("laser/pulse_ms", self.laser_pulse_ms.value())
        self.settings.setValue("api/port", self.api_port.value())
        self.settings.setValue("api/auto_start", self.api_auto_start.isChecked())
        self.settings.sync()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_settings()
        self._stop_gamepad_motion(send_stop=True)
        self.gamepad_manager.set_auto_reconnect(False)
        self.gamepad_manager.close("프로그램 종료", notify=False, reconnect=False)
        self.api_jog_watchdog.stop()
        self.api_server.stop()
        self.monitor_timer.stop()
        self.laser_timer.stop()
        self.jog_keepalive.stop()
        if self.is_connected:
            if self.laser_is_on:
                self.worker.request_send(
                    set_auxiliary(self._address(), self.aux_number.value(), False)
                )
            self.worker.request_send(stop(self._address()))
        self.worker.shutdown()
        self.worker.wait(1500)
        event.accept()
