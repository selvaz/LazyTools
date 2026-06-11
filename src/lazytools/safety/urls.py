"""Small SSRF guard for connector-constructed URLs.

The financial connectors talk to a fixed set of well-known hosts (SEC EDGAR,
stooq.com, …), so every URL they fetch is constructed in code rather than
supplied by a caller or an LLM. This helper is the belt-and-braces check
applied to each constructed URL *and* to every redirect target before it is
followed:

* only ``http`` / ``https`` schemes are allowed;
* a literal IP host must be globally routable — loopback, private (RFC 1918),
  link-local, multicast, reserved, and unspecified addresses are refused;
* when ``allowed_hosts`` is given, the hostname must be in that set. This is
  what actually pins a connector to its service: a DNS name that resolves to a
  private IP is only caught here when it is written as a literal IP, so
  connectors should always pass their service's host set.

The check is purely syntactic — it performs **no DNS resolution** and no I/O,
so it is safe to call on every request without latency cost. Denials raise
:class:`UrlBlocked`, a subclass of
:class:`~lazytools.safety.ActionBlocked`.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Collection
from urllib.parse import urlsplit

from lazytools.safety import ActionBlocked


class UrlBlocked(ActionBlocked):
    """Raised when a URL fails the SSRF guard (scheme / host / IP check)."""


def validate_public_url(url: str, *, allowed_hosts: Collection[str] | None = None) -> str:
    """Validate that ``url`` is a public http(s) URL; return it unchanged.

    Args:
        url: The absolute URL about to be fetched (including redirect targets).
        allowed_hosts: Optional set of permitted hostnames (compared
            case-insensitively). ``None`` skips the host pinning check.

    Raises:
        UrlBlocked: On a non-http(s) scheme, a missing hostname, a hostname
            outside ``allowed_hosts``, or a non-global literal IP host
            (loopback, private, link-local, multicast, reserved, unspecified).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UrlBlocked(f"refused URL {url!r}: scheme {parts.scheme!r} is not http(s)")
    host = parts.hostname
    if not host:
        raise UrlBlocked(f"refused URL {url!r}: missing hostname")
    host = host.lower()
    if allowed_hosts is not None and host not in {h.lower() for h in allowed_hosts}:
        raise UrlBlocked(f"refused URL {url!r}: host {host!r} is not an allowed host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A DNS name, not a literal IP. Pinning via ``allowed_hosts`` is the
        # real control for names; nothing more to check syntactically.
        return url
    if not ip.is_global:
        raise UrlBlocked(f"refused URL {url!r}: IP {host!r} is not globally routable")
    return url
