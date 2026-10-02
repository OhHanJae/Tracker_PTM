"""Small cross-platform helpers for binding/listening addresses."""

from __future__ import annotations

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
