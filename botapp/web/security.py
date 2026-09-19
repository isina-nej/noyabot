"""Security rules, SSRF protection, and URL validation."""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from .errors import BlockedURLError

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = {"http", "https"}

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "instance-data",
    "169.254.169.254",
}

# Private and reserved networks (IPv4 and IPv6)
BLOCKED_NETWORKS = [
    # Loopback
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    # Private / Local
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    # Link-local / Cloud metadata
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    # Current network / Broadcast
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("255.255.255.255/32"),
    # Shared address space / CGNAT
    ipaddress.ip_network("100.64.0.0/10"),
    # Benchmarking / Reserved
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("240.0.0.0/4"),
]


def is_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address belongs to any blocked or private range."""
    # Convert IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) to IPv4
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    for net in BLOCKED_NETWORKS:
        if ip in net:
            return True
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def validate_url_security(url: str) -> str:
    """Validate that URL uses http/https and does not resolve to private/loopback IPs.

    Raises BlockedURLError if the URL violates security policies.
    Returns normalized URL.
    """
    raw = (url or "").strip()
    if not raw:
        raise BlockedURLError("آدرس URL خالی است.")

    try:
        parsed = urlparse(raw)
    except Exception as exc:
        raise BlockedURLError(f"فرمت URL نامعتبر است: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURLError(f"پروتکل نامعتبر یا ناامن است: {scheme} (فقط http و https مجازند)")

    hostname = parsed.hostname
    if not hostname:
        raise BlockedURLError("آدرس URL فاقد دامنه معتبر است.")

    hostname_lower = hostname.lower().strip(".")
    if hostname_lower in BLOCKED_HOSTNAMES or hostname_lower.endswith(".local"):
        raise BlockedURLError(f"دسترسی به میزبان {hostname} مسدود است.")

    # Check direct IP addresses
    try:
        ip_obj = ipaddress.ip_address(hostname_lower)
        if is_ip_blocked(ip_obj):
            raise BlockedURLError(f"دسترسی به آدرس IP محلی/داخلی {hostname_lower} مسدود است.")
        return raw
    except ValueError:
        # Not a raw IP literal, proceed to DNS resolution
        pass

    # Resolve DNS to check resulting IP addresses
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        addr_info = socket.getaddrinfo(hostname_lower, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        resolved_ips: list[str] = []
        for family, _, _, _, sockaddr in addr_info:
            ip_str = str(sockaddr[0])
            resolved_ips.append(ip_str)
            ip_obj = ipaddress.ip_address(ip_str)
            if is_ip_blocked(ip_obj):
                logger.warning("SSRF blocked: host=%s resolved to private IP=%s", hostname_lower, ip_str)
                raise BlockedURLError(f"دامنه {hostname} به IP مسدود/خصوصی ({ip_str}) اشاره می‌کند.")
    except socket.gaierror as exc:
        raise BlockedURLError(f"خطای بررسی DNS برای دامنه {hostname}: {exc}") from exc

    return raw
