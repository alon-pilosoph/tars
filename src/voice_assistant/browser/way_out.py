"""The browser's way out to the internet when the browser runs somewhere else (a Fly Sprite, docs/browser.md): a
small SOCKS5 proxy on this machine that the remote Chromium sends every connection to, through the SSH link's
reverse forward. Pages then load from the home's address, which the sites TARS uses treat better than a data
centre's, and their names are looked up here too, since Chromium hands a SOCKS5 proxy names, not addresses.

It is also what keeps the home network out of the remote browser's reach, a page's or anyone's who has taken the
remote machine over: the way out ends inside the home, so it refuses anything at home itself. A name is resolved
here, refused if it is a home network's name or if any address it resolves to isn't a public one, and the
connection is then made to the address that was checked, so a name that answers differently the second time can't
slip past. Only the ports web pages use are let through. It answers to this machine only: the SSH link is the one
thing meant to reach it."""

import ipaddress
import logging
import selectors
import socket
import socketserver
import struct
import threading
from collections.abc import Callable, Iterable
from typing import Self

from ..homenet import address, home_name

log = logging.getLogger(__name__)

PORTS = frozenset({80, 443})  # what pages use; anything else (mail, a database) has no business leaving through home
CONNECT_S = 10.0  # how long a site is given to answer before the page is told it can't be reached
IDLE_S = 300.0  # a connection with nothing passing either way for this long is closed
NAT64 = ipaddress.IPv6Network("64:ff9b::/96")  # an IPv4 address dressed as IPv6 by a network that translates

# SOCKS5's own words (RFC 1928)
VERSION = 5
NO_AUTH, NO_METHOD = 0, 0xFF
CONNECT = 1
IPV4, NAME, IPV6 = 1, 3, 4
SUCCEEDED, FAILED, NOT_ALLOWED, UNREACHABLE, REFUSED, NOT_SUPPORTED, BAD_ADDRESS = 0, 1, 2, 4, 5, 7, 8

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolve = Callable[[str, int], Iterable[Address]]


class Refused(Exception):
    """A destination the way out won't go to, with the SOCKS5 reply that says so."""

    def __init__(self, reply: int, why: str):
        super().__init__(why)
        self.reply = reply


def public(found: Address) -> bool:
    """Whether `found` is an address on the internet, not one at home or anywhere private: an IPv4 address written as
    IPv6 is judged as the IPv4 address it is."""
    if isinstance(found, ipaddress.IPv6Address):
        if found.ipv4_mapped is not None:
            found = found.ipv4_mapped
        elif found in NAT64:
            found = ipaddress.IPv4Address(int(found) & 0xFFFFFFFF)
    return found.is_global and not found.is_multicast


def resolved(name: str, port: int) -> list[Address]:
    """Every address `name` resolves to here."""
    try:
        infos = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]


def destination(host: str, port: int, home: frozenset[str] = frozenset(), resolve: Resolve = resolved) -> list[Address]:
    """The addresses to connect to for `host` and `port`, every one of them checked; Refused if any isn't allowed.
    `home`: the names TARS is set up to reach home by, besides any home network's (homenet.configured)."""
    if port not in PORTS:
        raise Refused(NOT_ALLOWED, f"port {port}")
    host = host.lower().rstrip(".")
    written = address(host)
    if written is not None:
        found = [written]
    elif home_name(host, home):
        raise Refused(NOT_ALLOWED, f"{host} is at home")
    else:
        found = list(dict.fromkeys(resolve(host, port)))
        if not found:
            raise Refused(UNREACHABLE, f"{host} doesn't resolve")
    if not_public := [a for a in found if not public(a)]:
        raise Refused(NOT_ALLOWED, f"{host} is at {not_public[0]}")
    return found


def connected(addresses: list[Address], port: int) -> socket.socket:
    """A connection to the first of `addresses` that answers."""
    last: OSError = OSError("no address")
    for found in addresses:
        try:
            return socket.create_connection((str(found), port), timeout=CONNECT_S)
        except OSError as error:
            last = error
    raise last


class WayOut(socketserver.ThreadingTCPServer):
    """The proxy, listening on this machine's loopback; `serve_forever` on a thread, `shutdown` to stop."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = 0, home: frozenset[str] = frozenset(), resolve: Resolve = resolved):
        self.home, self.resolve = home, resolve
        super().__init__(("127.0.0.1", port), _Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def __enter__(self) -> Self:
        threading.Thread(target=self.serve_forever, daemon=True, name="way-out").start()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()
        self.server_close()


class _Handler(socketserver.BaseRequestHandler):
    server: WayOut

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(CONNECT_S)
        try:
            host, port = self.greeted(client)
        except Refused as refused:
            log.info("way out: refused a request (%s)", refused)
            return
        except OSError, ValueError:
            return
        try:
            remote = connected(destination(host, port, self.server.home, self.server.resolve), port)
        except Refused as refused:
            log.info("way out: refused %s:%d (%s)", host, port, refused)
            self.reply(client, refused.reply)
            return
        except OSError:
            self.reply(client, REFUSED)
            return
        with remote:
            self.reply(client, SUCCEEDED, remote.getsockname())
            pump(client, remote)

    @staticmethod
    def greeted(client: socket.socket) -> tuple[str, int]:
        """The SOCKS5 greeting and request: where the browser wants to go."""
        version, count = exactly(client, 2)
        if version != VERSION:
            raise Refused(FAILED, f"SOCKS version {version}")
        if NO_AUTH not in exactly(client, count):
            client.sendall(bytes([VERSION, NO_METHOD]))
            raise Refused(FAILED, "no method without a password offered")
        client.sendall(bytes([VERSION, NO_AUTH]))
        version, command, _, kind = exactly(client, 4)
        if version != VERSION or command != CONNECT:
            _Handler.reply(client, NOT_SUPPORTED)
            raise Refused(NOT_SUPPORTED, f"command {command}")
        if kind == IPV4:
            host = str(ipaddress.IPv4Address(exactly(client, 4)))
        elif kind == IPV6:
            host = str(ipaddress.IPv6Address(exactly(client, 16)))
        elif kind == NAME:
            host = exactly(client, exactly(client, 1)[0]).decode("ascii")
        else:
            _Handler.reply(client, BAD_ADDRESS)
            raise Refused(BAD_ADDRESS, f"address type {kind}")
        (port,) = struct.unpack("!H", exactly(client, 2))
        return host, port

    @staticmethod
    def reply(client: socket.socket, code: int, bound: tuple = ("0.0.0.0", 0)) -> None:  # nosec B104  # not bound
        found = ipaddress.ip_address(bound[0])
        kind = IPV4 if found.version == 4 else IPV6
        try:
            client.sendall(bytes([VERSION, code, 0, kind]) + found.packed + struct.pack("!H", bound[1]))
        except OSError:
            return


def exactly(client: socket.socket, size: int) -> bytes:
    """`size` bytes from `client`, or ConnectionError if it closes first."""
    got = b""
    while len(got) < size:
        chunk = client.recv(size - len(got))
        if not chunk:
            raise ConnectionError("closed mid-request")
        got += chunk
    return got


def pump(one: socket.socket, other: socket.socket) -> None:
    """Passes bytes both ways until either side closes or nothing passes for IDLE_S."""
    one.setblocking(False)
    other.setblocking(False)
    peer = {one: other, other: one}
    with selectors.DefaultSelector() as waiting:
        for side in peer:
            waiting.register(side, selectors.EVENT_READ)
        while ready := waiting.select(IDLE_S):
            for key, _ in ready:
                try:
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    peer[key.fileobj].settimeout(IDLE_S)  # a side that stops reading is let go of, not waited on
                    peer[key.fileobj].sendall(data)
                    peer[key.fileobj].setblocking(False)
                except OSError:
                    return
