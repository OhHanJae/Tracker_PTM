"""PT503 Control API example client using only the Python standard library.

Run the Ubuntu headless server first, then execute:
    python examples/python_api_client.py
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import uuid
from collections.abc import Callable
from typing import Any, Self


class ApiError(RuntimeError):
    """Error response returned by the PT503 control service."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(f"{error.get('code')}: {error.get('message')}")
        self.error = error


class Pt503ApiClient:
    """Persistent JSON Lines client with response correlation and events."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.on_event = on_event or self._print_event
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._closed = threading.Event()

    def connect(self, timeout: float = 3.0) -> None:
        """Connect to the PySide6 service and start its receive thread."""
        if self._socket is not None:
            return
        self._socket = socket.create_connection((self.host, self.port), timeout)
        self._socket.settimeout(None)
        self._closed.clear()
        self._reader = threading.Thread(
            target=self._read_loop,
            name="pt503-api-reader",
            daemon=True,
        )
        self._reader.start()

    def close(self) -> None:
        """Close the connection; the service applies disconnect safety rules."""
        self._closed.set()
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
            self._socket = None
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=1.0)
        self._reader = None

    def request(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        """Send one command and wait for the response with the matching id."""
        if self._socket is None:
            raise ConnectionError("API에 연결되어 있지 않습니다.")
        request_id = uuid.uuid4().hex
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = response_queue
        packet = {
            "id": request_id,
            "command": command,
            "params": params or {},
        }
        encoded = (
            json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            with self._send_lock:
                self._socket.sendall(encoded)
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"{command} 응답 제한 시간을 초과했습니다.") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if not response.get("ok"):
            raise ApiError(response.get("error", {}))
        return response.get("result", {})

    def _read_loop(self) -> None:
        buffer = bytearray()
        try:
            while not self._closed.is_set() and self._socket is not None:
                chunk = self._socket.recv(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw_line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    if raw_line.rstrip(b"\r"):
                        self._dispatch(json.loads(raw_line.decode("utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if not self._closed.is_set():
                self.on_event({"event": "client.error", "data": {"message": str(exc)}})
        finally:
            self._closed.set()

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "event" in message:
            self.on_event(message)
            return
        request_id = str(message.get("id"))
        with self._pending_lock:
            response_queue = self._pending.get(request_id)
        if response_queue is not None:
            response_queue.put_nowait(message)

    @staticmethod
    def _print_event(message: dict[str, Any]) -> None:
        print("EVENT", json.dumps(message, ensure_ascii=False, indent=2))

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


if __name__ == "__main__":
    with Pt503ApiClient() as client:
        print("PING", client.request("system.ping"))
        print("STATUS", client.request("system.status"))

        # 실제 장비 연결 예:
        # client.request(
        #     "serial.connect",
        #     {"port": "COM5", "baudrate": 2400, "address": 1},
        # )
        # client.request("laser.arm", {"enabled": True})
        # client.request("laser.pulse", {"duration_ms": 500})
        # client.request(
        #     "motion.absolute", {"pan": 90.0, "tilt": 5.0}, timeout=5.0
        # )
        # Headless 서버에서는 system.status의 motion.state로 완료/타임아웃을 확인합니다.
