"""Devices whose address the router changed are found again: by their hardware address, or (a Tuya device) its id."""

from voice_assistant import androidtv, found
from voice_assistant.androidtv import AndroidTV
from voice_assistant.found import Seen, arp_bsd, arp_linux, find_by_mac, normal_mac


def test_where_a_device_was_found_is_kept_and_merged(tmp_path):
    seen = Seen(tmp_path / "voice_data" / "found.json")
    assert seen.get("androidtv:192.168.1.60") == {}
    seen.put("androidtv:192.168.1.60", address="192.168.1.60", mac="0c:dc:91:3d:ef:60")
    seen.put("androidtv:192.168.1.60", address="192.168.1.77", mac="")  # an empty value keeps what was known
    assert Seen(seen.path).get("androidtv:192.168.1.60") == {"address": "192.168.1.77", "mac": "0c:dc:91:3d:ef:60"}


def test_a_broken_file_is_as_good_as_none(tmp_path):
    (tmp_path / "found.json").write_text("{not json")
    assert Seen(tmp_path / "found.json").get("x") == {}


def test_hardware_addresses_are_read_from_either_arp_table():
    linux = (
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.1      0x1         0x2         d4:35:1d:4f:6c:67     *        wlan0\n"
        "192.168.1.209    0x1         0x2         0c:dc:91:3d:ef:60     *        wlan0\n"
        "192.168.1.50     0x1         0x0         00:00:00:00:00:00     *        wlan0\n"
    )
    assert arp_linux(linux, "192.168.1.209") == "0c:dc:91:3d:ef:60"
    assert arp_linux(linux, "192.168.1.50") is None  # incomplete: nobody answered
    assert arp_bsd("? (192.168.1.209) at c:dc:91:3d:ef:60 on en0 ifscope [ethernet]") == "0c:dc:91:3d:ef:60"
    assert arp_bsd("? (192.168.1.50) -- no entry") is None
    assert normal_mac("0C:DC:91:3D:EF:60") == "0c:dc:91:3d:ef:60"


def test_a_box_is_found_by_its_hardware_address_among_those_with_the_port_open():
    macs = {"192.168.1.20": "aa:bb:cc:00:00:01", "192.168.1.77": "0c:dc:91:3d:ef:60"}
    swept = []

    def open_on(hosts, port):
        swept.append((len(hosts), port))
        return list(macs)

    assert find_by_mac("0C:DC:91:3D:EF:60", 5555, "192.168.1.209", open_on, macs.get) == "192.168.1.77"
    assert swept == [(254, 5555)]  # the whole /24, and only that port
    assert find_by_mac("de:ad:be:ef:00:00", 5555, "192.168.1.209", open_on, macs.get) is None
    assert find_by_mac("0c:dc:91:3d:ef:60", 5555, "not-an-address", open_on, macs.get) is None


def test_a_box_that_moved_is_looked_for_by_its_hardware_address_and_remembered(tmp_path, monkeypatch):
    seen = Seen(tmp_path / "found.json")
    seen.put("androidtv:192.168.1.60", address="192.168.1.60", mac="0c:dc:91:3d:ef:60")
    monkeypatch.setattr(androidtv, "reachable", lambda host, port, timeout_s: host == "192.168.1.77")
    monkeypatch.setattr(androidtv, "find_by_mac", lambda mac, port, near: "192.168.1.77")
    monkeypatch.setattr(androidtv, "mac_of", lambda ip: "0c:dc:91:3d:ef:60")
    box = AndroidTV("192.168.1.60", tmp_path / "adbkey", seen=seen)
    box._locate()
    box._remember()
    assert box._host == "192.168.1.77"
    assert seen.get("androidtv:192.168.1.60") == {"address": "192.168.1.77", "mac": "0c:dc:91:3d:ef:60"}
    assert AndroidTV("192.168.1.60", tmp_path / "adbkey", seen=seen)._host == "192.168.1.77"  # where it was last


def test_a_box_that_answers_where_it_was_isnt_looked_for(tmp_path, monkeypatch):
    seen = Seen(tmp_path / "found.json")
    seen.put("androidtv:192.168.1.60", address="192.168.1.60", mac="0c:dc:91:3d:ef:60")
    monkeypatch.setattr(androidtv, "reachable", lambda host, port, timeout_s: True)
    monkeypatch.setattr(androidtv, "find_by_mac", lambda *a: (_ for _ in ()).throw(AssertionError("looked for")))
    box = AndroidTV("192.168.1.60", tmp_path / "adbkey", seen=seen)
    box._locate()
    assert box._host == "192.168.1.60"


def test_the_sweep_knocks_on_one_port_and_only_says_where_it_was_open():
    assert found._open_on(["127.0.0.1"], 1) == []  # nothing listens on port 1
