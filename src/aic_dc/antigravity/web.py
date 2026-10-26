"""What a second opinion may look up, shared by both transports.

A second opinion may search the web and read public pages, and nothing
else. The two tools that do that run in different places — ``agy``'s on
this machine, the SDK's in its bundled harness, also on this machine —
so a URL is the one argument that can turn a web read into a local one:
``file://``, ``localhost`` and the private ranges reach this computer and
its network, which is the repository access a consultation is promised
not to have. :func:`public_url` is the test both transports apply.

**A syntactic test, deliberately.** It does not resolve the host. A
lookup here would block the gate on DNS, and its answer would not bind
the fetch that follows, which resolves again. So a public name that
resolves to a private address (DNS rebinding) is not caught, and that is
a stated limit rather than an oversight — see
``specs5/plan-ag/decisions.md`` § AG-36.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

#: Hostnames that name this machine without being an address.
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

#: Suffixes that only ever resolve on a local network.
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def public_url(url: Any) -> bool:
    """Whether ``url`` is an http(s) URL naming a host off this network.

    Anything that is not plainly public is refused, including what cannot
    be parsed: a refusal costs one page, a wrong admission costs the
    isolation the consultation's header asserts.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not host:
        return False
    host = host.rstrip(".").lower()
    if host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A name rather than an address. A dotless one is a local alias
        # (`http://router/`), since no public site is reached by one.
        return "." in host
    return address.is_global and not address.is_multicast


def url_argument(args: Any) -> Any:
    """The URL a ``read_url_content`` call names, whatever it is keyed as.

    The SDK spells the key ``url``; ``agy`` has not been measured, so the
    match is case-insensitive. Absent is ``None``, which
    :func:`public_url` refuses.
    """
    if not isinstance(args, dict):
        return None
    for key, value in args.items():
        if str(key).lower() == "url":
            return value
    return None


def public_url_call(args: Any) -> bool:
    """:func:`public_url` applied to a tool call's arguments."""
    return public_url(url_argument(args))
