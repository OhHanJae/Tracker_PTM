"""Local TCP/JSON request-response server for external control applications."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

PROTOCOL_NAME = "PT503-Control"
PROTOCOL_VERSION = "1.0"
DEFAULT_PORT = 8765
MAX_LINE_BYTES = 65_536


class ApiCommandError(ValueError):
    """A safe, client-visible command validation error."""

    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass
class _Client:
    client_id: str
    label: str
    buffer: bytearray


CommandHandler = Callable[[str, dict[str, Any], str, Any], dict[str, Any]]


class ControlApiServer(QObject):
    """Newline-delimited JSON server bound to localhost only.

    Qt owns all sockets in the GUI event loop.  The server never touches the
    serial port directly; the supplied command handler delegates to MainWindow,
    which remains the sole hardware owner.
    """

    running_changed = Signal(bool, str)
    client_count_changed = Signal(int)
    client_connected = Signal(str, str)
    client_disconnected = Signal(str)
    exchange = Signal(str, str, str)

    def __init__(self, handler: CommandHandler, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._handler = handler
        self._server = QTcpServer(self)
        self._server.setMaxPendingConnections(8)
        self._server.newConnection.connect(self._accept_connections)
        self._clients: dict[QTcpSocket, _Client] = {}
        self._next_client_number = 1
        self.port = DEFAULT_PORT

    @property
    def is_running(self) -> bool:
        return self._server.isListening()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self, port: int = DEFAULT_PORT) -> bool:
        if self.is_running:
            self.stop()
        if not 1024 <= int(port) <= 65_535:
            self.running_changed.emit(False, "포트는 1024~65535 범위여야 합니다.")
            return False
        self.port = int(port)
        localhost = QHostAddress(QHostAddress.SpecialAddress.LocalHost)
        if not self._server.listen(localhost, self.port):
            self.running_changed.emit(False, self._server.errorString())
            return False
        self.running_changed.emit(True, f"127.0.0.1:{self.port}")
        return True

    def stop(self) -> None:
        if not self.is_running and not self._clients:
            return
        self.broadcast("service.stopping", {"reason": "API server stopped"})
        self._server.close()
        for socket in list(self._clients):
            client = self._clients.pop(socket, None)
            socket.blockSignals(True)
            socket.abort()
            socket.deleteLater()
            if client is not None:
                self.client_disconnected.emit(client.client_id)
        self.client_count_changed.emit(0)
        self.running_changed.emit(False, "중지됨")

    def broadcast(self, event: str, data: dict[str, Any]) -> None:
        message = {
            "event": event,
            "data": data,
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        }
        for socket, client in list(self._clients.items()):
            self._send_json(socket, client, message, "EVENT")

    def send_event(self, client_id: str, event: str, data: dict[str, Any]) -> bool:
        message = {
            "event": event,
            "data": data,
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        }
        for socket, client in self._clients.items():
            if client.client_id == client_id:
                self._send_json(socket, client, message, "EVENT")
                return True
        return False

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            client_id = f"client-{self._next_client_number}"
            self._next_client_number += 1
            label = f"{socket.peerAddress().toString()}:{socket.peerPort()}"
            client = _Client(client_id, label, bytearray())
            self._clients[socket] = client
            socket.readyRead.connect(lambda sock=socket: self._read_client(sock))
            socket.disconnected.connect(lambda sock=socket: self._drop_client(sock))
            self.client_connected.emit(client_id, label)
            self.client_count_changed.emit(self.client_count)
            self._send_json(
                socket,
                client,
                {
                    "event": "service.ready",
                    "data": {
                        "protocol": PROTOCOL_NAME,
                        "version": PROTOCOL_VERSION,
                        "client_id": client_id,
                    },
                    "timestamp": datetime.now()
                    .astimezone()
                    .isoformat(timespec="milliseconds"),
                },
                "EVENT",
            )

    def _read_client(self, socket: QTcpSocket) -> None:
        client = self._clients.get(socket)
        if client is None:
            return
        client.buffer.extend(bytes(socket.readAll()))
        if len(client.buffer) > MAX_LINE_BYTES and b"\n" not in client.buffer:
            self._send_error(
                socket, client, None, "FRAME_TOO_LARGE", "요청이 너무 큽니다."
            )
            socket.disconnectFromHost()
            return

        while b"\n" in client.buffer:
            raw_line, _, remaining = client.buffer.partition(b"\n")
            client.buffer = bytearray(remaining)
            raw_line = raw_line.rstrip(b"\r")
            if not raw_line:
                continue
            if len(raw_line) > MAX_LINE_BYTES:
                self._send_error(
                    socket, client, None, "FRAME_TOO_LARGE", "요청이 너무 큽니다."
                )
                continue
            self._handle_line(socket, client, raw_line)

    def _handle_line(
        self, socket: QTcpSocket, client: _Client, raw_line: bytes
    ) -> None:
        request_id: Any = None
        try:
            text = raw_line.decode("utf-8")
            self.exchange.emit("API RX", client.client_id, text)
            request = json.loads(text)
            if not isinstance(request, dict):
                raise ApiCommandError("INVALID_REQUEST", "JSON object가 필요합니다.")
            request_id = request.get("id")
            if request_id is None or isinstance(request_id, (dict, list, bool)):
                raise ApiCommandError("INVALID_ID", "id는 문자열 또는 숫자여야 합니다.")
            command = request.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ApiCommandError("INVALID_COMMAND", "command 문자열이 필요합니다.")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ApiCommandError(
                    "INVALID_PARAMS", "params는 JSON object여야 합니다."
                )
            result = self._handler(
                command.strip(), params, client.client_id, request_id
            )
            response = {"id": request_id, "ok": True, "result": result}
            self._send_json(socket, client, response, "API TX")
        except UnicodeDecodeError:
            self._send_error(
                socket,
                client,
                request_id,
                "INVALID_ENCODING",
                "UTF-8 요청이 필요합니다.",
            )
        except json.JSONDecodeError as exc:
            self._send_error(
                socket,
                client,
                request_id,
                "INVALID_JSON",
                f"JSON 파싱 오류: {exc.msg}",
            )
        except ApiCommandError as exc:
            self._send_error(
                socket, client, request_id, exc.code, exc.message, exc.details
            )
        except Exception as exc:  # noqa: BLE001 - isolate malformed client requests.
            self._send_error(
                socket,
                client,
                request_id,
                "INTERNAL_ERROR",
                f"요청 처리 중 오류: {exc}",
            )

    def _send_error(
        self,
        socket: QTcpSocket,
        client: _Client,
        request_id: Any,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if details:
            error["details"] = details
        self._send_json(
            socket, client, {"id": request_id, "ok": False, "error": error}, "API TX"
        )

    def _send_json(
        self,
        socket: QTcpSocket,
        client: _Client,
        message: dict[str, Any],
        direction: str,
    ) -> None:
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        socket.write((text + "\n").encode("utf-8"))
        self.exchange.emit(direction, client.client_id, text)

    def _drop_client(self, socket: QTcpSocket) -> None:
        client = self._clients.pop(socket, None)
        socket.deleteLater()
        if client is None:
            return
        self.client_disconnected.emit(client.client_id)
        self.client_count_changed.emit(self.client_count)
