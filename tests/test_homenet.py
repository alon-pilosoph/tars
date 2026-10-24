"""What's at home, by name, by address, and by what a name resolves to."""

import ipaddress

import pytest

from voice_assistant.homenet import address, at_home, configured, host_of


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8080/", "http://127.1/", "http://0177.0.0.1/", "http://0x7f.1/", "http://[::1]/",
     "http://192.168.1.1/", "http://10.0.0.5:8123/", "http://100.64.1.1/", "http://tars:8080/", "https://pi.local/",
     "https://box.tail1.ts.net/", "https://router.lan/", "https://192.168.1.1\\@wolt.com/"],
)  # fmt: skip
def test_at_home(url):
    assert at_home(url, resolve=lambda host: ())


@pytest.mark.parametrize("url", ["https://wolt.com/he", "https://www.ksp.co.il/", "about:blank", "data:text/html,x",
                                 "https://evil.example\\@wolt.com/", "https://localhostess.com/"])  # fmt: skip
def test_not_at_home(url):
    assert not at_home(url, resolve=lambda host: (ipaddress.ip_address("93.184.216.34"),))


def test_a_public_name_for_a_home_address_is_at_home():
    resolve = {"192-168-1-1.nip.io": (ipaddress.ip_address("192.168.1.1"),)}.get
    assert at_home("http://192-168-1-1.nip.io/", resolve=lambda host: resolve(host, ()))


def test_the_houses_own_names_are_at_home():
    home = configured(["tars.example.org"], "http://homeassistant.example.org:8123")
    for url in ("https://tars.example.org/", "http://homeassistant.example.org:8123/api"):
        assert at_home(url, home, resolve=lambda host: ())


def test_a_browsers_reading_of_an_address():
    assert host_of("https://evil.example\\@wolt.com/x") == "evil.example"
    assert str(address("127.1")) == "127.0.0.1" and address("wolt.com") is None
