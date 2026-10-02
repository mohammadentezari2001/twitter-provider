"""Outbound HTTP with SSRF protection.

Two layers of defence:

1. `UrlPolicy.validate_url()` checks scheme, host allowlist and every resolved
   IP address *before* a connection is attempted.
2. `SafeResolver` re-applies the same checks inside aiohttp's connector, so a
   DNS-rebinding race or an HTTP redirect to an internal host is also blocked.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver

from app import __version__

USER_AGENT = f"tweet-relay-bot/{__version__} (+https://github.com/)"

#: Ranges that `ipaddress` does not flag but which must never be reachable.
_EXTRA_BLOCKED = (
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped, handled explicitly too
)


class UnsafeUrlError(Exception):
    """A URL was rejected by the SSRF policy.

    Intentionally **not** an `OSError`: aiohttp wraps `OSError` from a resolver
    into `ClientConnectorError`, which would hide the real reason.
    """

    def __init__(self, message: str, *, host: str | None = None) -> None:
        super().__init__(message)
        self.host = host


class UrlPolicy:
    """Decides which URLs may be fetched."""

    __slots__ = ("allowed_hosts", "allow_private", "allowed_schemes")

    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...] = (),
        allow_private: bool = False,
        allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
    ) -> None:
        self.allowed_hosts = tuple(h.lower().lstrip(".") for h in allowed_hosts)
        self.allow_private = allow_private
        self.allowed_schemes = allowed_schemes

    # ----------------------------------------------------------------- checks
    def host_allowed(self, host: str) -> bool:
        """Empty allowlist means "any public host"; otherwise suffix match."""
        if not self.allowed_hosts:
            return True
        host = host.lower().rstrip(".")
        return any(host == a or host.endswith(f".{a}") for a in self.allowed_hosts)

    def ip_allowed(self, address: str) -> bool:
        if self.allow_private:
            return True
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
        return not any(ip in net for net in _EXTRA_BLOCKED if ip.version == net.version)

    def ensure_url_allowed(self, url: str) -> tuple[str, int]:
        """Validate scheme + host, returning `(host, port)`.

        Raises:
            UnsafeUrlError: if the URL is structurally unusable or disallowed.
        """
        try:
            parsed = aiohttp.client.URL(url)
        except Exception as exc:  # malformed URL
            raise UnsafeUrlError("malformed URL") from exc

        if parsed.scheme not in self.allowed_schemes:
            raise UnsafeUrlError(f"scheme {parsed.scheme!r} is not allowed")
        host = parsed.host
        if not host:
            raise UnsafeUrlError("URL has no hostname")
        if not self.host_allowed(host):
            raise UnsafeUrlError(f"host {host!r} is not in the allowlist", host=host)

        # A literal IP in the URL never reaches the resolver, so check it here.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not self.ip_allowed(host):
                raise UnsafeUrlError("target resolves to a non-public address", host=host)

        return host, parsed.port or (443 if parsed.scheme == "https" else 80)

    async def validate_url(self, url: str) -> None:
        """Full pre-flight validation including DNS resolution."""
        host, port = self.ensure_url_allowed(url)
        try:
            ipaddress.ip_address(host)
            return  # literal IP, already validated
        except ValueError:
            pass
        await self.resolve_checked(host, port)

    async def resolve_checked(self, host: str, port: int) -> list[str]:
        """Resolve `host`, raising unless at least one address is permitted."""
        try:
            infos = await _getaddrinfo(host, port)
        except OSError as exc:
            raise UnsafeUrlError(f"could not resolve {host!r}", host=host) from exc
        addresses = [info[4][0] for info in infos]
        allowed = [a for a in addresses if self.ip_allowed(a)]
        if not allowed:
            raise UnsafeUrlError("target resolves to a non-public address", host=host)
        return allowed


async def _getaddrinfo(host: str, port: int) -> list[Any]:
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)


class SafeResolver(AbstractResolver):
    """aiohttp resolver that drops every non-permitted address.

    Because the connector dials exactly the addresses we return, a hostname that
    resolves to both a public and a private IP can never reach the private one.
    """

    def __init__(self, policy: UrlPolicy) -> None:
        self._policy = policy
        self._delegate = ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, Any]]:
        if not self._policy.host_allowed(host):
            raise UnsafeUrlError(f"host {host!r} is not in the allowlist", host=host)
        results = await self._delegate.resolve(host, port, family)
        allowed = [r for r in results if self._policy.ip_allowed(r["host"])]
        if not allowed:
            raise UnsafeUrlError("target resolves to a non-public address", host=host)
        return allowed

    async def close(self) -> None:
        await self._delegate.close()


def create_session(
    policy: UrlPolicy,
    *,
    connect_timeout: float = 10.0,
    total_timeout: float | None = 60.0,
    headers: dict[str, str] | None = None,
    limit: int = 10,
) -> aiohttp.ClientSession:
    """Build a `ClientSession` whose DNS is filtered by `policy`.

    The caller owns the session and must close it.
    """
    connector = aiohttp.TCPConnector(
        resolver=SafeResolver(policy),
        limit=limit,
        ttl_dns_cache=60,
        enable_cleanup_closed=True,
    )
    timeout = aiohttp.ClientTimeout(total=total_timeout, connect=connect_timeout)
    return aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        raise_for_status=False,
        auto_decompress=True,
    )
