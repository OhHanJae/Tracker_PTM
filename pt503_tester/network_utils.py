"""Small cross-platform helpers for binding/listening addresses."""

from __future__ import annotations

import os
import socket


def primary_ipv4() -> str:
    """Return the primary LAN IPv4 address, falling back to localhost."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    finally:
        probe.close()

    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = item[4][0]
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass
    return "127.0.0.1"


def resolve_bind_host(host: str | None, *, default: str = "auto") -> str:
    """Resolve an app-level bind host value into a concrete socket host."""

    selected = (host or default).strip().lower()
    if selected in {"", "auto", "lan"}:
        return primary_ipv4()
    return host or default


def managed_server_endpoints(
    host: str, port: int, tcp_host: str | None, tcp_port: int,
) -> tuple[str, int, str | None, int]:
    """Core-managed listeners take priority over launcher/command-line defaults."""
    if os.environ.get("CORE_MANAGED_PROCESS") != "1":
        return host, port, tcp_host, tcp_port
    selected: dict[str, str | int] = {}
    for protocol in ("HTTP", "TCP"):
        bind_host = os.environ.get(f"CORE_{protocol}_HOST", "").strip()
        if not bind_host:
            raise ValueError(f"CORE_{protocol}_HOST is required for managed startup")
        try:
            bind_port = int(os.environ[f"CORE_{protocol}_PORT"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"CORE_{protocol}_PORT must be an integer") from exc
        if not 1 <= bind_port <= 65535:
            raise ValueError(f"CORE_{protocol}_PORT must be between 1 and 65535")
        selected[f"{protocol}_HOST"] = bind_host
        selected[f"{protocol}_PORT"] = bind_port
    return (
        str(selected["HTTP_HOST"]), int(selected["HTTP_PORT"]),
        str(selected["TCP_HOST"]), int(selected["TCP_PORT"]),
    )
