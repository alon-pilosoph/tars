"""What's at home, as an address says it: this machine and the home network, by name or by address. The web UI
answers only to names it's reached by at home (webui.allowed_host), so a page elsewhere can't reach it through DNS
rebinding; TARS's browser never opens anything at home (browser/session.py), whatever a page asks, since a page can
say anything to the model working it, and nothing at home asks for a password.

A name counts as at home if it's one a home network gives (a single word like tars, an mDNS .local, a Tailscale
.ts.net, what routers call their own) or one TARS is set up with ([web] allowed_hosts, [home] url). An address does
if it isn't a public one, written however a browser takes it: 127.1 and 0177.0.0.1 are 127.0.0.1 to Chrome."""

import functools
import ipaddress
import socket
import threading
from urllib.parse import urlsplit

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
# The endings of names a home network gives its machines: mDNS, Tailscale's MagicDNS, and what routers call theirs.
HOME_SUFFIXES = (".local", ".ts.net", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".localhost")
RESOLVE_S = 3.0  # how long a name is given to resolve: one that takes longer is let through, as Chrome resolves it


def address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """`host` as an address, if it is one, in any form a browser takes: 127.0.0.1, 127.1, 0x7f.1, [::1]."""
    host = host.strip("[]")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host and all(c in "0123456789abcdefx." for c in host.lower()) and any(c.isdigit() for c in host):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))  # the shorthand and octal forms, as browsers read them
        except OSError:
            return None
    return None


def home_name(name: str, extra: frozenset[str] = frozenset()) -> bool:
    """Whether `name` (a host name, lower case) is one this machine or the home network goes by."""
    return name in extra or name in LOOPBACK or "." not in name or name.endswith(HOME_SUFFIXES)


def configured(web_hosts: list[str], home_url: str) -> frozenset[str]:
    """The names TARS is set up to reach home by, besides any home network's: [web] allowed_hosts, [home] url's."""
    return frozenset(h.lower() for h in [*web_hosts, urlsplit(home_url).hostname or ""] if h)


def host_of(url: str) -> str:
    """The host a browser would go to for `url`, lower case: a backslash is a slash to it, so the host of
    https://evil.example\\@wolt.com is evil.example, not wolt.com as a plain parse says."""
    return (urlsplit(url.replace("\\", "/")).hostname or "").lower().rstrip(".")


def at_home(url: str, extra: frozenset[str] = frozenset(), resolve=None) -> bool:
    """Whether `url` is on this machine or the home network: by its name, by its address, or by what its name
    resolves to (192-168-1-1.nip.io is a public name for a home address). Only web addresses are; about:blank and
    data: aren't anywhere."""
    if urlsplit(url).scheme not in ("http", "https", "ws", "wss"):
        return False
    host = host_of(url)
    if not host:
        return True  # a web address with no host: nowhere a browser should go
    found = address(host)
    if found is not None:
        return not found.is_global
    if home_name(host, extra):
        return True
    return any(not a.is_global for a in (resolve or _resolved)(host))


@functools.lru_cache(maxsize=512)
def _resolved(host: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """What `host` resolves to here; nothing for a name that doesn't, or not in time (a slow resolver isn't a reason
    to refuse a site: the address check after the page loads still holds)."""
    found: list = []

    def look() -> None:
        try:
            found.extend(info[4][0] for info in socket.getaddrinfo(host, None))
        except OSError:
            return

    asking = threading.Thread(target=look, daemon=True, name="resolve")
    asking.start()
    asking.join(RESOLVE_S)
    if asking.is_alive():
        return ()
    return tuple(a for a in (address(str(x).split("%")[0]) for x in found) if a is not None)
