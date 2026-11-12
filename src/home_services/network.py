"""Private bind validation.

The gateway binds only to loopback, RFC 1918/ULA LAN addresses, or the
Tailscale CGNAT/ULA ranges. A wildcard or public address is refused. If a
configured interface has no address yet (Tailscale still starting at boot),
the server waits instead of falling back to anything broader.
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess
import time

TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
# Explicit allow-list; `ip.is_private` also covers documentation/benchmark
# ranges that are not home-LAN addresses.
LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


class BindRefused(ValueError):
    pass


def classify(address: str) -> str:
    ip = ipaddress.ip_address(address)
    if ip.is_unspecified:
        raise BindRefused(f"{address} is a wildcard address; Home Services never binds every interface")
    if ip.is_loopback:
        return "loopback"
    if (ip.version == 4 and ip in TAILSCALE_V4) or (ip.version == 6 and ip in TAILSCALE_V6):
        return "tailscale"
    if any(ip.version == n.version and ip in n for n in LAN_NETWORKS):
        return "lan"
    raise BindRefused(f"{address} is not a loopback, private LAN, or Tailscale address")


def interface_addresses(name: str) -> list[str]:
    """Addresses currently assigned to an interface (Linux `ip`), or, for the
    special name `tailscale`, those reported by `tailscale ip`."""
    try:
        if name == "tailscale":
            out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
            return [a.strip() for a in out.stdout.split() if a.strip()] if out.returncode == 0 else []
        out = subprocess.run(["ip", "-o", "addr", "show", "dev", name], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    addrs = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if "inet" in parts:
            addrs.append(parts[parts.index("inet") + 1].split("/")[0])
        elif "inet6" in parts:
            addr = parts[parts.index("inet6") + 1].split("/")[0]
            if not addr.startswith("fe80"):
                addrs.append(addr)
    return addrs


def resolve_bind_addresses(addresses: tuple[str, ...], interfaces: tuple[str, ...], lookup=interface_addresses) -> list[str]:
    result = []
    for a in addresses:
        classify(a)
        result.append(a)
    for name in interfaces:
        for a in lookup(name):
            try:
                classify(a)
            except BindRefused:
                continue  # e.g. a public address on the same NIC: skip it, never bind it
            result.append(a)
    return sorted(set(result))


def wait_for_bind_addresses(addresses, interfaces, timeout: float, lookup=interface_addresses, sleep=time.sleep) -> list[str]:
    """Wait until every configured interface has a private address."""
    deadline = time.monotonic() + timeout
    while True:
        missing = [i for i in interfaces if not resolve_bind_addresses((), (i,), lookup)]
        if not missing:
            return resolve_bind_addresses(addresses, interfaces, lookup)
        if time.monotonic() >= deadline:
            raise BindRefused(f"no private address on interface(s) {missing} after {timeout:.0f}s; not binding anything broader")
        sleep(2)


def bind_sockets(addresses: list[str], port: int) -> list[socket.socket]:
    socks = []
    for a in addresses:
        family = socket.AF_INET6 if ":" in a else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((a, port))
        socks.append(s)
    return socks
