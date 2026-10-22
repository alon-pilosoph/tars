"""Who's home: the routers signed into as their own pages do it and read, the rules that turn what they see into home
and out (never out on a router's silence), the board the other processes read, and what it changes: "who's home" in
the house, reminders for someone out going to their chat, and messages waiting for someone said once they're home."""

import hashlib
import json
import random
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from voice_assistant import check
from voice_assistant.__main__ import make_home
from voice_assistant.config import ConfigError, load_config
from voice_assistant.home import SETTINGS, Request
from voice_assistant.presence import (
    DEVICE,
    STALE_AFTER_S,
    Board,
    ChatRouting,
    People,
    State,
    Tracker,
    Watcher,
    arrivals,
)
from voice_assistant.reminders import (
    MESSAGE,
    REMINDER,
    SCHEDULED,
    TELEGRAM,
    TIMER,
    VOICE,
    NewReminder,
    Reminders,
    ReminderTools,
)
from voice_assistant.routers import (
    RETRY_REFUSED_S,
    G,
    K,
    N,
    Refused,
    RouterError,
    Seen,
    Srp,
    Technicolor,
    Xiaomi,
    _bytes,
    technicolor_devices,
    xiaomi_devices,
    xiaomi_password,
)

DATA = Path(__file__).parent / "presence_data"
NOW = 1_800_000_000.0
MIN = 60.0
CONFIG = Path(__file__).parents[1] / "config.toml"


def sha256(*parts: bytes) -> bytes:
    return hashlib.sha256(b"".join(parts)).digest()


# Technicolor Homeware: SRP as the gateway's own script does it, and its device page.


def test_srp_matches_the_gateways_own_script_byte_for_byte():
    """Including a salt and a B with leading zeros, which are hashed as sent, and a password that isn't ASCII."""
    vectors = json.loads((DATA / "srp_vectors.json").read_text())
    a = int.from_bytes(bytes(vectors["seed"][1:33]), "big")
    for c in vectors["cases"]:
        srp = Srp(c["user"], c["pw"], a=a)
        assert _bytes(srp.A).hex() == c["A"]
        assert srp.proof(bytes.fromhex(c["s"]), bytes.fromhex(c["B"])) == (
            bytes.fromhex(c["M"]),
            bytes.fromhex(c["HAMK"]),
        )


def test_a_homeware_page_of_cards_lists_who_is_online_wired_or_not():
    devices = technicolor_devices((DATA / "homeware-devices.html").read_text())
    assert devices == [
        ("ca:fe:00:00:00:01", "iPhone", True),
        ("88:1e:5a:00:00:02", "Alons-iPhone", False),  # an old entry: Offline
        ("28:49:e9:00:00:03", "Staceys-iphone", True),
        ("cc:d8:43:00:00:04", "MiWiFi-RD23", True),  # wired, with an IP address
        ("70:09:71:00:00:05", "Samsung", False),  # wired, and off: no IP address
    ]


def test_a_homeware_table_goes_by_each_rows_light():
    devices = technicolor_devices((DATA / "homeware-table.html").read_text())
    assert devices == [
        ("ca:fe:00:00:00:11", "pixel-7", True),
        ("ca:fe:00:00:00:12", "old-laptop", False),
        ("ca:fe:00:00:00:13", "nas", True),
    ]


class FakeHomeware:
    """A gateway's sign-in and device page: the server's half of SRP, with its own salt (a leading zero) and B."""

    def __init__(self, user="admin", password="hunter2!", page=None):
        self.user, self.salt = user, bytes.fromhex("00" + "5a" * 15)
        x = int.from_bytes(sha256(self.salt, sha256(f"{user}:{password}".encode())), "big")
        self.v = pow(G, x, N)
        self.page = page or (DATA / "homeware-devices.html").read_text()
        self.token, self.stale_tokens, self.signed_in, self.sign_ins, self.requests = "t1", 0, False, 0, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        path = request.url.path
        if path == "/login.lp":
            return httpx.Response(
                200, text=f'<meta name = "CSRFtoken" content = "{self.token}"><input id="srp_password">'
            )
        if path == DEVICE_PAGE:
            return httpx.Response(200, text=self.page if self.signed_in else '<input id="srp_password">')
        assert path == "/authenticate" and request.headers["X-Requested-With"] == "XMLHttpRequest"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if self.stale_tokens or form["CSRFtoken"] != self.token:
            self.stale_tokens = max(0, self.stale_tokens - 1)
            return httpx.Response(403)
        if "I" in form:
            if form["I"] != self.user:
                return httpx.Response(200, json={"error": {"failure": 1}})
            self.a_pub, self.b = int(form["A"], 16), random.randrange(1, N)
            self.b_sent = ((K * self.v + pow(G, self.b, N)) % N).to_bytes(256, "big")
            return httpx.Response(200, json={"s": self.salt.hex(), "B": self.b_sent.hex()})
        u = int.from_bytes(sha256(self.a_pub.to_bytes(256, "big"), self.b_sent), "big")
        key = sha256(_bytes(pow(self.a_pub * pow(self.v, u, N), self.b, N)))
        hn_xor_hg = bytes(p ^ q for p, q in zip(sha256(_bytes(N)), sha256(_bytes(G)), strict=True))
        a_sent = _bytes(self.a_pub)
        m = sha256(hn_xor_hg, sha256(self.user.encode()), self.salt, a_sent, self.b_sent, key)
        if bytes.fromhex(form["M"]) != m:
            return httpx.Response(200, json={"error": {"failure": 1, "waitTime": 3, "wrongCount": 1}})
        self.signed_in, self.sign_ins = True, self.sign_ins + 1
        return httpx.Response(200, json={"M": sha256(a_sent, m, key).hex().upper()})


DEVICE_PAGE = "/modals/device-modal.lp"


def homeware(gateway: FakeHomeware, password="hunter2!", user="admin") -> Technicolor:
    return Technicolor("main", "192.168.1.1", user, password, transport=httpx.MockTransport(gateway))


def test_it_signs_into_a_homeware_gateway_and_lists_whats_connected_now():
    gateway = FakeHomeware()
    seen = homeware(gateway).connected()
    assert {s.name for s in seen} == {"iPhone", "Staceys-iphone", "MiWiFi-RD23"}
    assert gateway.sign_ins == 1


def test_a_stale_token_is_fetched_again_once_as_the_page_does():
    gateway = FakeHomeware()
    gateway.stale_tokens = 1
    assert len(homeware(gateway).connected()) == 3


def test_when_the_session_ends_it_signs_in_again():
    gateway = FakeHomeware()
    router = homeware(gateway)
    router.connected()
    gateway.signed_in = False  # the gateway restarted
    assert len(router.connected()) == 3 and gateway.sign_ins == 2


def test_a_wrong_password_is_refused_and_not_tried_again_for_a_while(monkeypatch):
    gateway = FakeHomeware()
    router = homeware(gateway, password="wrong")
    with pytest.raises(Refused, match="refused the password"):
        router.connected()
    asked = gateway.requests
    with pytest.raises(Refused, match="trying again"):
        router.connected()  # Homeware locks sign-in out after a few wrong ones
    assert gateway.requests == asked
    clock = router._refused_at + RETRY_REFUSED_S + 1
    monkeypatch.setattr("voice_assistant.routers.time.monotonic", lambda: clock)
    with pytest.raises(Refused):
        router.connected()
    assert gateway.requests > asked


def test_a_wrong_user_is_refused_too():
    with pytest.raises(Refused, match="user"):
        homeware(FakeHomeware(), user="root").connected()


def test_a_router_that_doesnt_answer_is_a_router_error():
    def down(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(RouterError, match="no answer from 192.168.1.1"):
        Technicolor("main", "192.168.1.1", "admin", "x", transport=httpx.MockTransport(down)).connected()


# Xiaomi (MiWiFi): its hashed password, and its list of who's connected.


class FakeXiaomi:
    KEY, DEVICE_ID = "a2ffa5c9be07488bbb04a3a47d3c5f6a", "ca:fe:00:00:00:99"

    def __init__(self, password="hunter2!"):
        self.password, self.tokens, self.sign_ins = password, [], 0
        self.devices = [
            {"mac": "CA:FE:00:00:00:01", "name": "iPhone", "online": 1, "type": 2},
            {"mac": "92:E9:E8:00:00:02", "name": "", "oname": "Staceys-iPhone", "online": 1, "type": 1},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/xqsystem/init_info"):
            return httpx.Response(200, json={"code": 0, "newEncryptMode": 1})
        if path.endswith("/luci/web"):
            return httpx.Response(
                200, text=f"var Encrypt = {{ key: '{self.KEY}', }}; var deviceId = '{self.DEVICE_ID}';"
            )
        if path.endswith("/xqsystem/login"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form["nonce"].startswith(f"0_{self.DEVICE_ID}_") and form["logtype"] == "2"
            if form["password"] != xiaomi_password(self.password, self.KEY, form["nonce"]):
                return httpx.Response(200, json={"code": 401, "msg": "not auth"})
            self.sign_ins += 1
            self.tokens.append(f"tok{self.sign_ins}")
            return httpx.Response(200, json={"code": 0, "token": self.tokens[-1]})
        token = path.split(";stok=")[1].split("/")[0]
        assert path.endswith("/api/misystem/devicelist")
        if token != self.tokens[-1]:
            return httpx.Response(200, json={"code": 401, "msg": "Invalid token"})
        return httpx.Response(200, json={"code": 0, "list": self.devices})


def xiaomi(server: FakeXiaomi, password="hunter2!") -> Xiaomi:
    return Xiaomi("ap", "192.168.1.160", "admin", password, transport=httpx.MockTransport(server))


def test_it_signs_into_a_xiaomi_and_lists_whats_connected():
    assert xiaomi(FakeXiaomi()).connected() == [
        Seen("ca:fe:00:00:00:01", "iPhone"),
        Seen("92:e9:e8:00:00:02", "Staceys-iPhone"),
    ]


def test_an_expired_xiaomi_token_means_signing_in_again():
    server = FakeXiaomi()
    router = xiaomi(server)
    router.connected()
    server.tokens.append("someone-else-signed-in")
    assert len(router.connected()) == 2 and server.sign_ins == 2


def test_a_xiaomi_that_turns_the_password_down_is_refused():
    with pytest.raises(Refused):
        xiaomi(FakeXiaomi(), password="wrong").connected()


def test_the_xiaomi_list_leaves_out_anything_marked_offline():
    answer = {"code": 0, "list": [{"mac": "AA:00:00:00:00:01", "online": "0"}, {"mac": "AA:00:00:00:00:02"}]}
    assert xiaomi_devices(answer) == [Seen("aa:00:00:00:00:02", "")]


# The rules: home at once, out only after away_after unseen while every router answered, not known at first.


PEOPLE = People({"Alon": ["ca:fe:00:00:00:01"], "Stacey": ["Staceys-iphone", "92:e9:e8:00:00:02"]})
ALON_PHONE, STACEY_PHONE = Seen("ca:fe:00:00:00:01", "iPhone"), Seen("28:49:e9:00:00:03", "Staceys-iphone")


def tracker() -> Tracker:
    return Tracker(PEOPLE, 10 * MIN, NOW)


def test_a_phone_is_known_by_its_address_or_by_the_name_a_router_lists_it_under():
    """A router can keep an old address under a phone's name: the name finds it."""
    assert PEOPLE.whose([ALON_PHONE, STACEY_PHONE, Seen("ff:00:00:00:00:00", "tv")]) == {"alon", "stacey"}
    assert PEOPLE.whose([Seen("92:e9:e8:00:00:02", "")]) == {"stacey"}


def test_nobody_is_known_until_the_routers_answer_and_starting_up_isnt_an_arrival():
    t = tracker()
    assert t.states["alon"].home is None
    assert t.update([ALON_PHONE], True, NOW) == []  # home, but not "came home": TARS just started
    assert t.states["alon"] == State(True, None, NOW)
    t.update([ALON_PHONE], True, NOW + 9 * MIN)
    assert t.states["stacey"].home is None  # not seen yet, and not for long enough to say she's out
    t.update([ALON_PHONE], True, NOW + 10 * MIN)
    assert t.states["stacey"] == State(False, None, None)  # out, since some time before TARS was watching


def test_out_after_away_after_unseen_dated_when_last_seen_and_back_at_once():
    t = tracker()
    t.update([ALON_PHONE, STACEY_PHONE], True, NOW)
    assert t.update([ALON_PHONE], True, NOW + 9 * MIN) == []  # 9 minutes unseen: maybe a phone asleep
    assert t.update([ALON_PHONE], True, NOW + 10 * MIN) == [("stacey", False)]
    assert t.states["stacey"] == State(False, NOW, NOW)  # out since about when she was last seen
    assert t.update([ALON_PHONE, STACEY_PHONE], True, NOW + 30 * MIN) == [("stacey", True)]
    assert t.states["stacey"] == State(True, NOW + 30 * MIN, NOW + 30 * MIN)


def test_a_router_that_doesnt_answer_never_sends_anyone_out():
    t = tracker()
    t.update([ALON_PHONE, STACEY_PHONE], True, NOW)
    for minute in range(1, 30):
        assert t.update([ALON_PHONE], False, NOW + minute * MIN) == []  # her phone may be on the silent router
    assert t.states["stacey"].home is True
    # Unseen counts only from when every router answered again: ten whole minutes after the last silent round.
    assert t.update([ALON_PHONE], True, NOW + 38 * MIN) == []
    assert t.update([ALON_PHONE], True, NOW + 39 * MIN) == [("stacey", False)]


# The board: what the assistant writes, and every process reads.


@pytest.fixture
def board(log):
    return Board(log.store, PEOPLE, clock=lambda: NOW)


def written(board, now=NOW, **states):
    board.write({p: State(*s) for p, s in states.items()}, {"main": {"address": "192.168.1.1", "ok_at": now}}, now)


def test_the_board_says_who_is_home_and_since_when(board):
    written(board, alon=(True, None, NOW), stacey=(False, NOW - 3600, NOW - 3600))
    assert board.is_out("Stacey") and not board.is_out("alon") and not board.is_out("nobody")
    summary = board.summary()
    assert summary.startswith("Alon: home; Stacey: out (since about ") and summary.endswith(")")


def test_a_board_the_assistant_stopped_writing_knows_nothing(log):
    written(Board(log.store, PEOPLE), now=NOW - STALE_AFTER_S - 1, stacey=(False, None, None))
    stale = Board(log.store, PEOPLE, clock=lambda: NOW)
    assert not stale.is_out("stacey") and stale.summary() == "Alon: not known yet; Stacey: not known yet"


def test_who_is_home_is_a_status_only_device_in_the_house(board, tmp_path):
    written(board, alon=(True, None, NOW), stacey=(None, None, None))
    tools = make_home(load_config(CONFIG, local=False), tmp_path, presence=board, devices=False)
    assert list(tools.devices) == [DEVICE]
    ask = {"device": DEVICE, "action": "status", **dict.fromkeys(SETTINGS)}
    assert tools.call(ask, Request()) == "who's home: Alon: home; Stacey: not known yet"
    assert tools.call({**ask, "action": "on"}, Request()).startswith("error: the who's home can do: status")
    assert "going by their phone" in tools.rules()


# What it changes: reminders for someone out go to their chat; messages waiting for someone are said once home.


@pytest.fixture
def reminders(log):
    return Reminders(log.store)


def due_now(reminders, **kw):
    kw = {"kind": REMINDER, "text": "call the bank", "for_name": "stacey", **kw}
    return reminders.add(NewReminder(due=NOW + 1, **kw), VOICE, now=NOW)


def test_a_reminder_for_someone_out_with_a_chat_goes_to_their_chat(board, reminders):
    written(board, stacey=(False, NOW - 3600, NOW - 3600))
    route = ChatRouting(board, reminders, ["Stacey", "Alon"])
    rid = due_now(reminders)
    assert route(reminders.get(rid)) and reminders.get(rid)["channel"] == TELEGRAM
    assert not route(reminders.get(rid))  # once


@pytest.mark.parametrize(
    "state, kw, chats",
    [
        ((None, None, None), {}, ["Stacey"]),  # not known isn't out: said aloud, as before
        ((True, None, NOW), {}, ["Stacey"]),
        ((False, None, None), {}, ["Alon"]),  # no chat of her own
        ((False, None, None), {"kind": TIMER, "text": None}, ["Stacey"]),  # a timer rings in the kitchen
        ((False, None, None), {"for_name": None}, ["Stacey"]),  # for whoever's there
    ],
)
def test_otherwise_it_is_said_aloud_as_before(board, reminders, state, kw, chats):
    written(board, stacey=state)
    rid = due_now(reminders, **kw)
    assert not ChatRouting(board, reminders, chats)(reminders.get(rid))
    assert reminders.get(rid)["channel"] == VOICE


def test_a_message_waiting_for_someone_is_due_soon_after_their_phone_comes_home(reminders):
    rid = reminders.add(
        NewReminder(MESSAGE, "dinner's at eight", "stacey", "alon", due=None), VOICE, ["stacey"], now=NOW
    )
    assert reminders.due(NOW + 3600) == []
    arrivals(reminders, grace_s=60)("Stacey", True)
    r = reminders.get(rid)
    assert r["status"] == SCHEDULED and r["next_at"] is not None
    assert [r["id"] for r in reminders.due(r["next_at"])] == [rid]
    assert reminders.held_for("stacey") == [r]  # and if she speaks to TARS first, it's said then


def test_going_out_or_coming_home_again_doesnt_reschedule_it(reminders):
    rid = reminders.add(NewReminder(MESSAGE, "dinner's at eight", "stacey", due=None), VOICE, ["stacey"], now=NOW)
    on_change = arrivals(reminders, grace_s=60)
    on_change("stacey", False)
    assert reminders.get(rid)["next_at"] is None
    on_change("stacey", True)
    first = reminders.get(rid)["next_at"]
    on_change("stacey", True)
    assert reminders.get(rid)["next_at"] == first


def test_someone_known_only_by_their_phone_can_be_left_a_message_for_when_theyre_back(reminders):
    tools = ReminderTools(reminders, voices=list, phones=lambda: ["Stacey"])
    args = {"kind": MESSAGE, "text": "call me", "for": "Stacey", "when_back": True}
    _, said = tools.call("remind", args, now=NOW)
    assert said.startswith("set for when Stacey is next heard or comes home")
    _, refused = ReminderTools(reminders).call("remind", args, now=NOW)
    assert refused.startswith("error: TARS doesn't know Stacey's voice yet")


# The watcher: one round asks every router, never raises, and says who came and went.


class FakeRouter:
    kind = "technicolor"

    def __init__(self, name, *rounds):
        self.name, self.address, self._rounds = name, f"10.0.0.{len(name)}", list(rounds)

    def connected(self):
        answer = self._rounds.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_a_round_asks_every_router_writes_the_board_and_says_who_came_and_went(log, capsys):
    clock = iter([NOW, NOW + 11 * MIN, NOW + 12 * MIN])
    changes = []
    router = FakeRouter("main", [ALON_PHONE, STACEY_PHONE], [ALON_PHONE], [ALON_PHONE, STACEY_PHONE])
    other = FakeRouter("ap", [], [], RouterError("no answer from 10.0.0.2: ConnectTimeout"))
    board = Board(log.store, PEOPLE, clock=lambda: NOW + 12 * MIN)
    watcher = Watcher(
        [router, other], Tracker(PEOPLE, 10 * MIN, NOW), board, 30, lambda *c: changes.append(c), clock.__next__
    )
    assert watcher.round() == []
    assert watcher.round() == [("stacey", False)]
    assert watcher.round() == [("stacey", True)]  # a router down can't hide that she's back
    assert changes == [("Stacey", False), ("Stacey", True)]
    assert board.routers()["ap"]["error"].startswith("no answer") and board.routers()["main"]["connected"] == 2
    assert "(presence: Stacey went out)" in capsys.readouterr().out


def test_a_router_failing_is_said_once_not_every_round(log, capsys):
    down = RouterError("no answer from 10.0.0.4: ConnectError")
    router = FakeRouter("main", down, down, [ALON_PHONE])
    watcher = Watcher([router], Tracker(PEOPLE, 10 * MIN, NOW), Board(log.store, PEOPLE), 30, None, lambda: NOW)
    for _ in range(3):
        watcher.round()
    out = capsys.readouterr().out
    assert out.count("can't say who's connected") == 1 and "answers again" in out


# Its settings, and its checks.


def presence_config(tmp_path, body: str):
    """config.toml, with this machine's [presence] in a config.local.toml beside it, as at home."""
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.read_text())
    (tmp_path / "config.local.toml").write_text(body)
    return load_config(path)


def test_the_settings_say_whats_wrong(tmp_path):
    with pytest.raises(ConfigError) as e:
        presence_config(
            tmp_path,
            '[presence.people]\nAlon = ["ca:fe:00:00:01"]\nStacey = []\n'
            '[[presence.routers]]\nkind = "netgear"\naddress = "192.168.1.1"\nsecret = "lowercase"\nport = 80\n',
        )
    message = str(e.value.code)
    for problem in (
        "'ca:fe:00:00:01' isn't a hardware address",
        "'Stacey' needs their phone's address or name",
        "kind must be technicolor or xiaomi",
        "needs secret, the name of its password in .env",
        "has no setting port",
    ):
        assert problem in message


def test_the_settings_in_the_docs_load(tmp_path):
    cfg = presence_config(
        tmp_path,
        "[presence]\nenabled = true\n"
        '[presence.people]\nAlon = ["ca:fe:00:00:00:01"]\nStacey = ["staceys-iphone", "ca:fe:00:00:00:02"]\n'
        '[[presence.routers]]\nkind = "technicolor"\naddress = "192.168.1.1"\nuser = "admin"\n'
        'secret = "ROUTER_PASSWORD"\n'
        '[[presence.routers]]\nkind = "xiaomi"\naddress = "192.168.1.160"\nsecret = "XIAOMI_PASSWORD"\n',
    )
    assert cfg.presence.enabled and len(cfg.presence.routers) == 2


def test_check_signs_into_each_router_and_says_whose_phone_it_sees(tmp_path):
    cfg = presence_config(
        tmp_path,
        '[presence]\nenabled = true\npeople = { Alon = ["ca:fe:00:00:00:01"] }\n'
        '[[presence.routers]]\nkind = "technicolor"\naddress = "192.168.1.1"\nsecret = "ROUTER_PASSWORD"\n'
        '[[presence.routers]]\nkind = "xiaomi"\naddress = "192.168.1.160"\nsecret = "XIAOMI_PASSWORD"\n',
    )
    servers = {"technicolor": FakeHomeware(), "xiaomi": FakeXiaomi()}

    def make_router(spec, password):
        cls = Technicolor if spec["kind"] == "technicolor" else Xiaomi
        return cls(
            spec["kind"], spec["address"], "admin", password, transport=httpx.MockTransport(servers[spec["kind"]])
        )

    results = check.routers_now(cfg, {"ROUTER_PASSWORD": "hunter2!"}.get, make_router)
    assert [(r.name, r.status) for r in results] == [
        ("Router 192.168.1.1", check.OK),
        ("Router 192.168.1.160", check.FAIL),
    ]
    assert results[0].detail == "3 devices connected; sees the phone of Alon"
    assert results[1].detail == "no XIAOMI_PASSWORD in .env"


def test_the_status_page_goes_by_how_the_routers_last_answered_the_assistant(tmp_path):
    cfg = presence_config(
        tmp_path,
        '[presence]\nenabled = true\npeople = { Alon = ["ca:fe:00:00:00:01"] }\n'
        '[[presence.routers]]\nkind = "technicolor"\naddress = "192.168.1.1"\nsecret = "ROUTER_PASSWORD"\n',
    )
    folder = tmp_path / "events"
    assert [r.status for r in check.routers_last(cfg, folder, NOW)] == [check.WARN, check.OK]  # never asked yet
    from voice_assistant.store import Store

    board = Board(Store(folder), People(cfg.presence.people))
    board.write(
        {"alon": State(True, None, NOW)}, {"main": {"address": "192.168.1.1", "ok_at": NOW, "connected": 9}}, NOW
    )
    results = check.routers_last(cfg, folder, NOW + 30)
    assert [(r.status, r.detail) for r in results] == [
        (check.OK, "9 devices connected, just now"),
        (check.OK, "Alon: home"),
    ]
    assert check.routers_last(load_config(CONFIG, local=False), folder).status == check.SKIP


def test_a_router_left_out_for_want_of_its_password_keeps_anyone_from_being_marked_out(log):
    router = FakeRouter("main", *([[ALON_PHONE]] * 3))
    watcher = Watcher(
        [router],
        Tracker(PEOPLE, 10 * MIN, NOW),
        Board(log.store, PEOPLE),
        30,
        None,
        iter([NOW, NOW + 20 * MIN, NOW + 40 * MIN]).__next__,
        left_out=1,
    )
    for _ in range(3):
        assert watcher.round() == []
    assert watcher.tracker.states["stacey"].home is None  # her phone may be on the router TARS can't ask
