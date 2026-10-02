"""Background serial I/O and non-moving device discovery."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

import serial
from PySide6.QtCore import QThread, Signal

from .protocol import (
    OutgoingCommand,
    query_pan,
    stop,
    verify_frame_checksum,
)


@dataclass(frozen=True)
class SerialConfig:
    port: str
    baudrate: int = 2400
    address: int = 1


class SerialWorker(QThread):
    """Own the serial port in a dedicated thread.

    Public request methods only enqueue work, so the GUI thread never blocks on
    a slow COM port or on the automatic discovery loop.
    """

    connected = Signal(str, int, int)
    disconnected = Signal(str)
    error = Signal(str)
    status = Signal(str)

    tx_frame = Signal(object, str, float)
    rx_blob = Signal(object, float)

    scan_started = Signal(int)
    scan_progress = Signal(int, int, str)
    scan_found = Signal(str, int, int)
    scan_finished = Signal(bool, str)

    def __init__(self) -> None:
        super().__init__()
        self._commands: queue.Queue[tuple] = queue.Queue()
        self._stop_thread = threading.Event()
        self._cancel_scan = threading.Event()
        self._serial: serial.Serial | None = None
        self._active_config: SerialConfig | None = None
        self._rx_accumulator = bytearray()
        self._last_rx_time = 0.0
        self._last_tx_time = 0.0
        self._min_command_gap_s = 0.32

    # These methods are safe to call from the GUI thread.
    def request_open(self, config: SerialConfig) -> None:
        self._commands.put(("open", config))

    def request_close(self) -> None:
        self._commands.put(("close",))

    def request_send(self, command: OutgoingCommand) -> None:
        self._commands.put(("send", command))

    def request_scan(
        self,
        ports: Iterable[str],
        baudrates: Iterable[int],
        first_address: int,
        last_address: int,
        timeout_ms: int,
    ) -> None:
        self._cancel_scan.clear()
        self._commands.put(
            (
                "scan",
                list(ports),
                list(baudrates),
                first_address,
                last_address,
                timeout_ms,
            )
        )

    def cancel_scan(self) -> None:
        self._cancel_scan.set()

    def shutdown(self) -> None:
        self._cancel_scan.set()
        # Queue shutdown after any pending Laser OFF / STOP command.  Setting
        # the stop flag here would skip those already queued safety writes.
        self._commands.put(("shutdown",))

    def run(self) -> None:
        while not self._stop_thread.is_set():
            try:
                action = self._commands.get(timeout=0.01)
            except queue.Empty:
                action = None

            if action:
                kind = action[0]
                try:
                    if kind == "open":
                        self._open(action[1])
                    elif kind == "close":
                        self._close("사용자 연결 해제", send_stop=True)
                    elif kind == "send":
                        self._send(action[1])
                    elif kind == "scan":
                        self._scan(*action[1:])
                    elif kind == "shutdown":
                        self._stop_thread.set()
                        break
                except Exception as exc:  # noqa: BLE001 - keep worker alive after I/O errors.
                    self.error.emit(f"시리얼 처리 오류: {exc}")

            self._read_live_port()

        self._close("프로그램 종료", send_stop=True)

    def _open_serial(self, port: str, baudrate: int) -> serial.Serial:
        return serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0,
            write_timeout=0.5,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

    def _open(self, config: SerialConfig) -> None:
        self._close("재연결", send_stop=True, notify=False)
        self.status.emit(f"{config.port} 열기 중...")
        try:
            self._serial = self._open_serial(config.port, config.baudrate)
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except serial.SerialException as exc:
            self._serial = None
            self._active_config = None
            self.error.emit(f"{config.port} 연결 실패: {exc}")
            self.disconnected.emit("연결 실패")
            return

        self._active_config = config
        self._rx_accumulator.clear()
        self._last_tx_time = 0.0
        self.connected.emit(config.port, config.baudrate, config.address)

    def _close(self, reason: str, send_stop: bool, notify: bool = True) -> None:
        serial_port = self._serial
        config = self._active_config
        if serial_port and serial_port.is_open:
            if send_stop and config:
                try:
                    command = stop(config.address)
                    serial_port.write(command.data)
                    serial_port.flush()
                    self.tx_frame.emit(
                        command.data,
                        f"[자동 안전정지] {command.description}",
                        time.perf_counter(),
                    )
                except serial.SerialException:
                    pass
            try:
                serial_port.close()
            except serial.SerialException:
                pass
        self._serial = None
        self._active_config = None
        self._rx_accumulator.clear()
        self._last_tx_time = 0.0
        if notify:
            self.disconnected.emit(reason)

    def _send(self, command: OutgoingCommand) -> None:
        if not self._serial or not self._serial.is_open:
            self.error.emit("COM 포트가 연결되지 않았습니다.")
            return
        try:
            # Pelco-D guidance requires a deliberate gap between commands.  This
            # also gives the PT unit time to finish its reply before another
            # request or motion frame is placed on the bus.
            now = time.perf_counter()
            remaining = self._min_command_gap_s - (now - self._last_tx_time)
            if self._last_tx_time and remaining > 0:
                time.sleep(remaining)

            sent_at = time.perf_counter()
            written = self._serial.write(command.data)
            self._serial.flush()
            self._last_tx_time = sent_at
            if written != len(command.data):
                raise serial.SerialTimeoutException(
                    f"{len(command.data)}바이트 중 {written}바이트만 송신됨"
                )
            self.tx_frame.emit(command.data, command.description, sent_at)
        except serial.SerialException as exc:
            self.error.emit(f"송신 실패: {exc}")
            self._close("통신 오류로 연결 해제", send_stop=False)

    def _read_live_port(self) -> None:
        if not self._serial or not self._serial.is_open:
            return
        try:
            waiting = self._serial.in_waiting
            if waiting:
                self._rx_accumulator.extend(self._serial.read(waiting))
                self._last_rx_time = time.perf_counter()
            elif (
                self._rx_accumulator
                and time.perf_counter() - self._last_rx_time >= 0.040
            ):
                blob = bytes(self._rx_accumulator)
                self._rx_accumulator.clear()
                self.rx_blob.emit(blob, time.perf_counter())
        except serial.SerialException as exc:
            self.error.emit(f"수신 실패: {exc}")
            self._close("수신 오류로 연결 해제", send_stop=False)

    @staticmethod
    def _scan_has_match(data: bytes, address: int, tx_checksum: int) -> bool:
        # Accept a valid Pan/Tilt extended response for the probed address.
        for start in range(max(0, len(data) - 20), max(0, len(data) - 6)):
            candidate = data[start : start + 7]
            if (
                len(candidate) == 7
                and candidate[0] == 0xFF
                and candidate[1] == address
                and candidate[3] in (0x59, 0x5B)
                and verify_frame_checksum(candidate)
            ):
                return True

        # Some vendor firmwares return only the four-byte general response.
        for start in range(max(0, len(data) - 12), max(0, len(data) - 3)):
            candidate = data[start : start + 4]
            if (
                len(candidate) == 4
                and candidate[0] == 0xFF
                and candidate[1] == address
                and candidate[3] == ((tx_checksum + candidate[2]) & 0xFF)
            ):
                return True
        return False

    def _scan(
        self,
        ports: list[str],
        baudrates: list[int],
        first_address: int,
        last_address: int,
        timeout_ms: int,
    ) -> None:
        self._close("자동검색 시작", send_stop=True, notify=False)
        attempts = len(ports) * len(baudrates) * (last_address - first_address + 1)
        self.scan_started.emit(attempts)
        current = 0

        for port in ports:
            for baudrate in baudrates:
                if self._cancel_scan.is_set() or self._stop_thread.is_set():
                    self.scan_finished.emit(False, "자동검색이 취소되었습니다.")
                    return
                try:
                    candidate_port = self._open_serial(port, baudrate)
                except serial.SerialException as exc:
                    skipped = last_address - first_address + 1
                    current += skipped
                    self.scan_progress.emit(
                        current, attempts, f"{port} 열기 실패: {exc}"
                    )
                    continue

                try:
                    for address in range(first_address, last_address + 1):
                        if self._cancel_scan.is_set() or self._stop_thread.is_set():
                            candidate_port.close()
                            self.scan_finished.emit(False, "자동검색이 취소되었습니다.")
                            return

                        current += 1
                        label = f"{port}, {baudrate}bps, 주소 {address}"
                        self.scan_progress.emit(current, attempts, label)
                        command = query_pan(address)
                        candidate_port.reset_input_buffer()
                        candidate_port.write(command.data)
                        candidate_port.flush()
                        sent_at = time.perf_counter()
                        self.tx_frame.emit(
                            command.data,
                            f"[자동검색 {current}/{attempts}] {command.description}",
                            sent_at,
                        )

                        deadline = sent_at + timeout_ms / 1000.0
                        received = bytearray()
                        while time.perf_counter() < deadline:
                            count = candidate_port.in_waiting
                            if count:
                                received.extend(candidate_port.read(count))
                                # A full response is enough; do not wait out the timeout.
                                if self._scan_has_match(
                                    received, address, command.data[6]
                                ):
                                    break
                            time.sleep(0.005)

                        if received:
                            self.rx_blob.emit(bytes(received), time.perf_counter())
                        if self._scan_has_match(received, address, command.data[6]):
                            self._serial = candidate_port
                            self._active_config = SerialConfig(port, baudrate, address)
                            self._rx_accumulator.clear()
                            self.scan_found.emit(port, baudrate, address)
                            self.connected.emit(port, baudrate, address)
                            self.scan_finished.emit(True, f"장비 발견: {label}")
                            return
                finally:
                    if candidate_port is not self._serial and candidate_port.is_open:
                        candidate_port.close()

        self.scan_finished.emit(False, "선택 범위에서 응답 장비를 찾지 못했습니다.")
        self.disconnected.emit("자동검색 완료 - 장비 없음")
