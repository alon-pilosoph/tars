"""The way out for a browser that runs elsewhere (browser/way_out.py): the SOCKS5 proxy that must never let a page,
or whoever has the remote machine, reach anything at home. The refusals one by one, then real SOCKS5 conversations
on this machine's loopback, with the connection to "the site" sent to a local echo server."""

import ipaddress
import socket
import struct
import threading

import pytest

from voice_assistant.browser import way_out
from voice_assistant.browser.way_out import NOT_ALLOWED, UNREACHABLE, Refused, WayOut, destination, public

SITE = ipaddress.ip_address("93.184.215.14")


def resolving(*answers: str):
    """A resolver that answers every name with `answers`, and counts how often it was asked."""

    def resolve(name: str, port: int):
        resolve.asked += 1
        return [ipaddress.ip_address(a) for a in answers]

    resolve.asked = 0
    return resolve


@pytest.mark.parametrize(
    "found",
    [
        "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254", "100.100.100.100", "0.0.0.0",
        "224.0.0.251", "::1", "fe80::1", "fd7a:115c:a1e0::1", "::ffff:192.168.1.1", "64:ff9b::c0a8:101",
    ],
)  # fmt: skip
def test_nothing_private_is_public(found):
    assert not public(ipaddress.ip_address(found))


@pytest.mark.parametrize("found", ["93.184.215.14", "2606:4700::6810:84e5", "::ffff:93.184.215.14"])
def test_the_internet_is(found):
    assert public(ipaddress.ip_address(found))


def test_a_site_goes_through():
    assert destination("wolt.com", 443, resolve=resolving(str(SITE))) == [SITE]


@pytest.mark.parametrize("port", [22, 25, 8123, 9333, 3306])
def test_only_web_ports(port):
    with pytest.raises(Refused) as refused:
        destination("wolt.com", port, resolve=resolving(str(SITE)))
    assert refused.value.reply == NOT_ALLOWED


@pytest.mark.parametrize(
    "host", ["localhost", "homepi.local", "router", "tars.ts.net", "nas.lan", "home.arpa.home", "LOCALHOST."]
)
def test_home_names_are_refused_without_asking_dns(host):
    resolve = resolving(str(SITE))
    with pytest.raises(Refused):
        destination(host, 443, resolve=resolve)
    assert resolve.asked == 0


def test_a_name_set_up_for_home_is_refused():
    with pytest.raises(Refused):
        destination("tars.example.com", 443, home=frozenset({"tars.example.com"}), resolve=resolving(str(SITE)))


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1", "0x7f.1", "192.168.1.1", "::1", "[::1]", "::ffff:10.0.0.1"])
def test_home_addresses_are_refused(host):
    with pytest.raises(Refused):
        destination(host, 443, resolve=resolving(str(SITE)))


def test_a_public_name_for_a_home_address_is_refused():
    with pytest.raises(Refused):
        destination("192-168-1-1.nip.io", 443, resolve=resolving("192.168.1.1"))


def test_one_private_address_among_public_ones_is_enough_to_refuse():
    with pytest.raises(Refused):
        destination("wolt.com", 443, resolve=resolving(str(SITE), "10.0.0.1"))


def test_a_name_that_does_not_resolve():
    with pytest.raises(Refused) as refused:
        destination("nowhere.example", 443, resolve=resolving())
    assert refused.value.reply == UNREACHABLE


# Real SOCKS5 conversations


@pytest.fixture
def echo():
    """A server on loopback that sends back what it's sent: stands in for the site."""
    server = socket.create_server(("127.0.0.1", 0))

    def serve():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection:
                while data := connection.recv(4096):
                    connection.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


@pytest.fixture
def dialled(monkeypatch, echo):
    """The addresses the way out connected to; each connection really goes to the echo server."""
    calls = []

    def connected(addresses, port):
        calls.append((addresses, port))
        return socket.create_connection(("127.0.0.1", echo))

    monkeypatch.setattr(way_out, "connected", connected)
    return calls


def ask(port: int, host: str | bytes, to: int) -> tuple[socket.socket, int]:
    """A SOCKS5 CONNECT to `host`:`to` through the way out on `port`: the socket and the reply's code."""
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(bytes([5, 1, 0]))
    assert client.recv(2) == bytes([5, 0])
    if isinstance(host, bytes):
        target = bytes([1]) + host
    else:
        target = bytes([3, len(host)]) + host.encode()
    client.sendall(bytes([5, 1, 0]) + target + struct.pack("!H", to))
    head = way_out.exactly(client, 4)
    way_out.exactly(client, 4 if head[3] == 1 else 16)
    way_out.exactly(client, 2)
    return client, head[1]


def test_a_page_reaches_its_site_through_the_way_out(dialled):
    with WayOut(resolve=resolving(str(SITE))) as proxy:
        client, code = ask(proxy.port, "wolt.com", 443)
        with client:
            assert code == 0
            client.sendall(b"GET / HTTP/1.1\r\n\r\n")
            assert client.recv(100) == b"GET / HTTP/1.1\r\n\r\n"
    assert dialled == [([SITE], 443)]


def test_the_checked_address_is_the_one_connected_to(dialled):
    """A name that answers differently when asked again can't steer the connection home: it's asked once."""
    answers = iter([[SITE], [ipaddress.ip_address("192.168.1.1")]])

    def rebinding(name, port):
        return next(answers)

    with WayOut(resolve=rebinding) as proxy:
        client, code = ask(proxy.port, "rebind.example", 443)
        client.close()
    assert code == 0
    assert dialled == [([SITE], 443)]


@pytest.mark.parametrize(
    "host, to",
    [("router", 443), ("wolt.com", 22), (socket.inet_aton("127.0.0.1"), 443), (socket.inet_aton("10.1.2.3"), 80)],
)
def test_home_is_refused_on_the_wire(dialled, host, to):
    with WayOut(resolve=resolving(str(SITE))) as proxy:
        client, code = ask(proxy.port, host, to)
        client.close()
    assert code == NOT_ALLOWED
    assert dialled == []


def test_only_connect_is_spoken(dialled):
    with (
        WayOut(resolve=resolving(str(SITE))) as proxy,
        socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as client,
    ):
        client.sendall(bytes([5, 1, 0]))
        client.recv(2)
        client.sendall(bytes([5, 3, 0, 1]) + socket.inet_aton("93.184.215.14") + struct.pack("!H", 443))  # UDP
        assert client.recv(2) == bytes([5, way_out.NOT_SUPPORTED])
    assert dialled == []


def test_a_password_only_client_is_turned_away():
    with WayOut() as proxy, socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as client:
        client.sendall(bytes([5, 1, 2]))  # username and password only
        assert client.recv(2) == bytes([5, 0xFF])


def test_it_listens_on_loopback_only():
    with WayOut() as proxy:
        assert proxy.server_address[0] == "127.0.0.1"
