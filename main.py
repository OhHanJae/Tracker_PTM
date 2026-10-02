from __future__ import annotations

import argparse
import sys


def run_legacy_gui() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from pt503_tester.main_window import MainWindow

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("PT503 Tester")
    app.setOrganizationName("OSRND")
    window = MainWindow()
    window.show()
    return app.exec()


def run_client(host: str = "127.0.0.1", port: int = 8765) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from pt503_tester.remote_client_window import RemoteClientWindow

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("PT503 Remote Client")
    app.setOrganizationName("OSRND")
    window = RemoteClientWindow(host, port)
    window.show()
    return app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description="PT503 tester")
    parser.add_argument("--server", action="store_true", help="run headless HTTP/TCP server")
    parser.add_argument("--client", action="store_true", help="run PySide6 TCP API client")
    parser.add_argument("--legacy-gui", action="store_true", help="run legacy direct-control GUI")
    parser.add_argument("--host", default=None, help="server bind host or client target host, default 0.0.0.0 for server / 127.0.0.1 for client")
    parser.add_argument("--port", type=int, default=None, help="headless server port, default 8080")
    parser.add_argument("--tcp-host", default=None, help="headless TCP bind host, default 0.0.0.0")
    parser.add_argument("--tcp-port", type=int, default=None, help="headless TCP port, default 8765")
    parser.add_argument("--no-tcp", action="store_true", help="disable headless TCP server")
    args = parser.parse_args()
    if args.server:
        from pt503_tester.headless_server import (
            DEFAULT_HTTP_HOST,
            DEFAULT_HTTP_PORT,
            DEFAULT_TCP_HOST,
            DEFAULT_TCP_PORT,
            run_server,
        )

        return run_server(
            args.host or DEFAULT_HTTP_HOST,
            args.port or DEFAULT_HTTP_PORT,
            tcp_host=None if args.no_tcp else args.tcp_host or DEFAULT_TCP_HOST,
            tcp_port=args.tcp_port or DEFAULT_TCP_PORT,
        )
    if args.legacy_gui:
        return run_legacy_gui()
    return run_client(args.host or "127.0.0.1", args.tcp_port or 8765)


if __name__ == "__main__":
    raise SystemExit(main())
