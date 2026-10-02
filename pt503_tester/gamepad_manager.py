"""Non-blocking pygame controller discovery and polling for the Qt UI."""

from __future__ import annotations

import os
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal

from . import gamepad_mapping as mapping

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
try:
    import pygame
except ImportError:  # The UI remains usable and explains how to install it.
    pygame = None

try:
    from pygame._sdl2 import controller as sdl_controller
except (ImportError, AttributeError):  # Raw joystick fallback still works.
    sdl_controller = None


STANDARD_AXES = (
    "CONTROLLER_AXIS_LEFTX",
    "CONTROLLER_AXIS_LEFTY",
    "CONTROLLER_AXIS_RIGHTX",
    "CONTROLLER_AXIS_RIGHTY",
    "CONTROLLER_AXIS_TRIGGERLEFT",
    "CONTROLLER_AXIS_TRIGGERRIGHT",
)

STANDARD_BUTTONS = (
    "CONTROLLER_BUTTON_A",
    "CONTROLLER_BUTTON_B",
    "CONTROLLER_BUTTON_X",
    "CONTROLLER_BUTTON_Y",
    "CONTROLLER_BUTTON_LEFTSHOULDER",
    "CONTROLLER_BUTTON_RIGHTSHOULDER",
    "CONTROLLER_BUTTON_BACK",
    "CONTROLLER_BUTTON_START",
)


@dataclass(frozen=True)
class GamepadDevice:
    index: int
    name: str
    guid: str
    standard_mapping: bool = False


@dataclass(frozen=True)
class GamepadState:
    axes: tuple[float, ...]
    buttons: tuple[bool, ...]
    hats: tuple[tuple[int, int], ...]
    mapping_mode: str = "raw"


class GamepadManager(QObject):
    """Poll one USB gamepad without blocking the PySide6 event loop."""

    devices_changed = Signal(object)
    connected = Signal(str)
    disconnected = Signal(str)
    state_changed = Signal(object)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._controller = None
        self._joystick = None
        self._mapping_mode = "raw"
        self._auto_reconnect = False
        self._preferred_index: int | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(mapping.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(1500)
        self._reconnect_timer.timeout.connect(self._attempt_reconnect)

    @property
    def available(self) -> bool:
        return pygame is not None

    @property
    def is_connected(self) -> bool:
        if pygame is None:
            return False
        if self._controller is not None:
            try:
                return bool(self._controller.get_init() and self._controller.attached())
            except pygame.error:
                return False
        return self._joystick is not None and bool(self._joystick.get_init())

    def refresh_devices(self) -> list[GamepadDevice]:
        if pygame is None:
            self.devices_changed.emit([])
            return []
        try:
            self._initialize_pygame()
            devices = []
            for index in range(pygame.joystick.get_count()):
                joystick = pygame.joystick.Joystick(index)
                standard = self._is_standard_controller(index)
                name = self._controller_name(index) if standard else joystick.get_name()
                devices.append(
                    GamepadDevice(index, name, joystick.get_guid(), standard)
                )
            self.devices_changed.emit(devices)
            return devices
        except pygame.error as exc:
            self.error.emit(f"게임패드 검색 실패: {exc}")
            self.devices_changed.emit([])
            return []

    def set_auto_reconnect(
        self, enabled: bool, preferred_index: int | None = None
    ) -> None:
        self._auto_reconnect = enabled
        if preferred_index is not None:
            self._preferred_index = preferred_index
        if enabled and not self.is_connected:
            self._reconnect_timer.start()
        elif not enabled:
            self._reconnect_timer.stop()

    def connect_device(self, index: int, *, auto_reconnect: bool = True) -> None:
        if pygame is None:
            self.error.emit("pygame가 설치되지 않았습니다. requirements.txt를 다시 설치하세요.")
            return
        self._preferred_index = index
        self._auto_reconnect = auto_reconnect
        self._reconnect_timer.stop()
        self.close("재연결", notify=False, reconnect=False)
        try:
            self._initialize_pygame()
            if not 0 <= index < pygame.joystick.get_count():
                raise pygame.error("선택한 게임패드를 찾을 수 없습니다.")
            if self._is_standard_controller(index):
                assert sdl_controller is not None
                self._controller = sdl_controller.Controller(index)
                self._mapping_mode = "SDL 표준"
                name = f"{self._controller_name(index)} · SDL 표준 매핑"
            else:
                self._joystick = pygame.joystick.Joystick(index)
                self._joystick.init()
                self._mapping_mode = "raw"
                name = f"{self._joystick.get_name()} · raw 매핑"
            self._timer.start()
            self.connected.emit(name)
        except pygame.error as exc:
            self._controller = None
            self._joystick = None
            self.error.emit(f"게임패드 연결 실패: {exc}")
            if self._auto_reconnect:
                self._reconnect_timer.start()

    def close(
        self,
        reason: str = "사용자 연결 해제",
        *,
        notify: bool = True,
        reconnect: bool | None = None,
    ) -> None:
        if reconnect is None:
            reconnect = self._auto_reconnect
        self._timer.stop()
        self._reconnect_timer.stop()
        controller = self._controller
        joystick = self._joystick
        self._controller = None
        self._joystick = None
        self._mapping_mode = "raw"
        if controller is not None:
            try:
                controller.quit()
            except pygame.error:
                pass
        if joystick is not None:
            try:
                joystick.quit()
            except pygame.error:
                pass
        if notify:
            self.disconnected.emit(reason)
        if reconnect and self.available:
            self._reconnect_timer.start()

    def _attempt_reconnect(self) -> None:
        if pygame is None or self.is_connected:
            return
        devices = self.refresh_devices()
        if not devices:
            return
        index = self._preferred_index
        if index is None or not any(device.index == index for device in devices):
            index = devices[0].index
        self.connect_device(index, auto_reconnect=True)

    def rumble(self, low: float, high: float, duration_ms: int) -> bool:
        """Request optional vibration feedback; unsupported pads return False."""

        if pygame is None:
            return False
        device = self._controller if self._controller is not None else self._joystick
        if device is None:
            return False
        try:
            return bool(device.rumble(low, high, duration_ms))
        except (AttributeError, pygame.error):
            return False

    @staticmethod
    def _initialize_pygame() -> None:
        assert pygame is not None
        if not pygame.display.get_init():
            pygame.display.init()
        if not pygame.joystick.get_init():
            pygame.joystick.init()
        if sdl_controller is not None and not sdl_controller.get_init():
            sdl_controller.init()

    @staticmethod
    def _is_standard_controller(index: int) -> bool:
        if not mapping.USE_SDL_CONTROLLER or sdl_controller is None:
            return False
        try:
            return bool(sdl_controller.is_controller(index))
        except pygame.error:
            return False

    @staticmethod
    def _controller_name(index: int) -> str:
        if sdl_controller is None:
            return "Gamepad"
        try:
            return sdl_controller.name_forindex(index) or f"Gamepad {index}"
        except pygame.error:
            return f"Gamepad {index}"

    @staticmethod
    def _controller_axis_value(value: int) -> float:
        if value < 0:
            return max(-1.0, value / 32768.0)
        return min(1.0, value / 32767.0)

    def _poll_controller(self) -> GamepadState:
        assert self._controller is not None and sdl_controller is not None
        axes = tuple(
            self._controller_axis_value(
                int(self._controller.get_axis(getattr(sdl_controller, axis_name)))
            )
            for axis_name in STANDARD_AXES
        )
        buttons = tuple(
            bool(self._controller.get_button(getattr(sdl_controller, button_name)))
            for button_name in STANDARD_BUTTONS
        )
        dpad_left = bool(
            self._controller.get_button(sdl_controller.CONTROLLER_BUTTON_DPAD_LEFT)
        )
        dpad_right = bool(
            self._controller.get_button(sdl_controller.CONTROLLER_BUTTON_DPAD_RIGHT)
        )
        dpad_up = bool(
            self._controller.get_button(sdl_controller.CONTROLLER_BUTTON_DPAD_UP)
        )
        dpad_down = bool(
            self._controller.get_button(sdl_controller.CONTROLLER_BUTTON_DPAD_DOWN)
        )
        hats = ((int(dpad_right) - int(dpad_left), int(dpad_up) - int(dpad_down)),)
        return GamepadState(axes=axes, buttons=buttons, hats=hats, mapping_mode="SDL")

    def _poll_joystick(self) -> GamepadState:
        assert self._joystick is not None
        return GamepadState(
            axes=tuple(
                self._joystick.get_axis(index)
                for index in range(self._joystick.get_numaxes())
            ),
            buttons=tuple(
                bool(self._joystick.get_button(index))
                for index in range(self._joystick.get_numbuttons())
            ),
            hats=tuple(
                self._joystick.get_hat(index)
                for index in range(self._joystick.get_numhats())
            ),
            mapping_mode="raw",
        )

    def _poll(self) -> None:
        if pygame is None or (self._controller is None and self._joystick is None):
            return
        try:
            pygame.event.pump()
            if self._controller is not None:
                if not self._controller.attached():
                    self.close("게임패드 연결 끊김", reconnect=True)
                    return
                state = self._poll_controller()
            elif self._joystick is not None and self._joystick.get_init():
                state = self._poll_joystick()
            else:
                self.close("게임패드 연결 끊김", reconnect=True)
                return
            self.state_changed.emit(state)
        except pygame.error as exc:
            self.error.emit(f"게임패드 읽기 실패: {exc}")
            self.close("게임패드 통신 오류", reconnect=True)
