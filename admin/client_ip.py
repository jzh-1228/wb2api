"""Canonical client addresses shared by gateway logging and IP access control."""
import ipaddress
import os
from functools import lru_cache

from admin.config import settings


def normalize_ip(value: str) -> str:
    address = ipaddress.ip_address(value.strip())
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


@lru_cache(maxsize=8)
def _proxy_networks(config):
    return tuple(ipaddress.ip_network(part.strip(), strict=False)
                 for part in config.split(',') if part.strip())


def request_ip(request) -> str:
    direct = request.client.host if request.client else ''
    try:
        peer = ipaddress.ip_address(normalize_ip(direct))
    except ValueError:
        return 'unknown'
    trusted = getattr(settings, 'TRUSTED_PROXY_IPS', os.getenv('ADMIN_TRUSTED_PROXY_IPS', '127.0.0.1,::1'))
    if any(peer in network for network in _proxy_networks(trusted)):
        # nginx must overwrite X-Real-IP and append the actual peer to XFF.
        real = request.headers.get('x-real-ip')
        forwarded = request.headers.get('x-forwarded-for')
        candidate = real or (forwarded.split(',')[-1] if forwarded else '')
        if candidate:
            try:
                return normalize_ip(candidate)
            except ValueError:
                pass
    return str(peer)
