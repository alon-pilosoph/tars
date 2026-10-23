"""Who's home: the routers signed into as their own pages do it and read, the rules that turn what they see into home
and out (never out on a router's silence, a clock's jump or a page this can't read), the board the other processes
read, and what it changes: "who's home" in the house, reminders for someone out going to their chat, and messages
waiting for someone said once they're home."""

import hashlib
import json
import random
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from voice_assistant.__main__ import make_home
from voice_assistant.config import ROUTER_KINDS, ConfigError, PresenceConfig, load_config
from voice_assistant.home import SETTINGS, Request
from voice_assistant.presence import (
    DEVICE,
    STALE_AFTER_S,
    Board,
    People,
    Presence,
    RouterStatus,
    State,
    Tracker,
    Watcher,
    make_presence,
)
from voice_assistant.reminders import (
    MESSAGE,
    REMINDER,
    SCHEDULED,
    TELEGRAM,
    TIMER,
    VOICE,
    ChatWhenOut,
    NewReminder,
    Reminders,
    ReminderTools,
    on_arrival,
)
from voice_assistant.routers import (
    DEVICE_PAGE,
    MAKERS,
    RETRY_REFUSED_S,
    SRP_G,
    SRP_K,
    SRP_N,
    Listed,
    Refused,
    Router,
    RouterError,
    Seen,
    Srp,
    make,
    srp_bytes,
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


def test_every_router_kind_in_the_settings_has_its_class():
    assert set(ROUTER_KINDS) == set(MAKERS)


# Technicolor Homeware: SRP as the gateway's own script does it, and its device page.


def test_srp_matches_the_gateways_own_script_byte_for_byte():
    """Including a salt and a B with leading zeros, which are hashed as sent, and a password that isn't ASCII."""
    vectors = json.loads((DATA / "srp_vectors.json").read_text())
    a = int.from_bytes(bytes(vectors["seed"][1:33]), "big")
    for c in vectors["cases"]:
        srp = Srp(c["user"], c["pw"], a=a)
        assert srp_bytes(srp.A).hex() == c["A"]
        assert srp.proof(bytes.fromhex(c["s"]), bytes.fromhex(c["B"])) == (
            bytes.fromhex(c["M"]),
            bytes.fromhex(c["HAMK"]),
        )


def test_a_homeware_page_of_cards_lists_who_is_online_wired_or_not():
    assert technicolor_devices((DATA / "homeware-devices.html").read_text()) == [
        Listed("ca:fe:00:00:00:01", "iPhone", True),
        Listed("88:1e:5a:00:00:02", "Alons-iPhone", False),  # an old entry: Offline
        Listed("28:49:e9:00:00:03", "Staceys-iphone", True),
        Listed("cc:d8:43:00:00:04", "MiWiFi-RD23", True),  # wired, with an IP address
        Listed("70:09:71:00:00:05", "Samsung", False),  # wired, and off: no IP address
    ]


def test_a_homeware_table_goes_by_each_rows_light():
    assert technicolor_devices((DATA / "homeware-table.html").read_text()) == [
        Listed("ca:fe:00:00:00:11", "pixel-7", True),
        Listed("ca:fe:00:00:00:12", "old-laptop", False),
        Listed("ca:fe:00:00:00:13", "nas", True),
    ]


class FakeHomeware:
    """A gateway's sign-in and device page: the server's half of SRP, with its own salt (a leading zero) and B."""

    def __init__(self, user="admin", password="hunter2!", page=None):
        self.user, self.salt = user, bytes.fromhex("00" + "5a" * 15)
        x = int.from_bytes(sha256(self.salt, sha256(f"{user}:{password}".encode())), "big")
        self.v = pow(SRP_G, x, SRP_N)
        self.page = page or (DATA / "homeware-devices.html").read_text()
        self.token, self.stale_tokens, self.signed_in, self.sign_ins, self.requests = "t1", 0, False, 0, 0
        self.signed_out_as = None  # how an ended session answers: None (the sign-in form), or a status code
        self.forgets = False  # a session cookie that never sticks
        self.wrong_proofs = 0  # something that doesn't know the password answers for the gateway this many times

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        path = request.url.path
        if path == "/login.lp":
            return httpx.Response(
                200, text=f'<meta name = "CSRFtoken" content = "{self.token}"><input id="srp_password">'
            )
        if path == DEVICE_PAGE:
            if self.signed_in and not self.forgets:
                return httpx.Response(200, text=self.page)
            if self.signed_out_as:
                return httpx.Response(self.signed_out_as, headers={"Location": "/login.lp"})
            return httpx.Response(200, text='<input id="srp_password">')
        assert path == "/authenticate" and request.headers["X-Requested-With"] == "XMLHttpRequest"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if self.stale_tokens or form["CSRFtoken"] != self.token:
            self.stale_tokens = max(0, self.stale_tokens - 1)
            return httpx.Response(403)
        if "I" in form:
            if form["I"] != self.user:
                return httpx.Response(200, json={"error": {"failure": 1}})
            self.a_pub, self.b = int(form["A"], 16), random.randrange(1, SRP_N)
            self.b_sent = ((SRP_K * self.v + pow(SRP_G, self.b, SRP_N)) % SRP_N).to_bytes(256, "big")
            return httpx.Response(200, json={"s": self.salt.hex(), "B": self.b_sent.hex()})
        u = int.from_bytes(sha256(self.a_pub.to_bytes(256, "big"), self.b_sent), "big")
        key = sha256(srp_bytes(pow(self.a_pub * pow(self.v, u, SRP_N), self.b, SRP_N)))
        hn_xor_hg = bytes(p ^ q for p, q in zip(sha256(srp_bytes(SRP_N)), sha256(srp_bytes(SRP_G)), strict=True))
        a_sent = srp_bytes(self.a_pub)
        m = sha256(hn_xor_hg, sha256(self.user.encode()), self.salt, a_sent, self.b_sent, key)
        if bytes.fromhex(form["M"]) != m:
            return httpx.Response(200, json={"error": {"failure": 1, "waitTime": 3, "wrongCount": 1}})
        if self.wrong_proofs:
            self.wrong_proofs -= 1
            return httpx.Response(200, json={"M": "00" * 32})
        self.signed_in, self.sign_ins = True, self.sign_ins + 1
        return httpx.Response(200, json={"M": sha256(a_sent, m, key).hex().upper()})


def homeware(gateway: FakeHomeware, password="hunter2!", user="admin") -> Router:
    spec = {"kind": "technicolor", "address": "192.168.1.1", "user": user}
    return make(spec, password, httpx.MockTransport(gateway))


def test_it_signs_into_a_homeware_gateway_and_lists_whats_connected_now():
    gateway = FakeHomeware()
    assert {s.name for s in homeware(gateway).connected()} == {"iPhone", "Staceys-iphone", "MiWiFi-RD23"}
    assert gateway.sign_ins == 1


def test_a_stale_token_is_fetched_again_once_as_the_page_does():
    gateway = FakeHomeware()
    gateway.stale_tokens = 1
    assert len(homeware(gateway).connected()) == 3


@pytest.mark.parametrize("signed_out_as", [None, 302, 401])
def test_when_the_session_ends_it_signs_in_again_quietly(signed_out_as):
    """The sign-in form, a redirect to it, or a refusal: some firmware answer an ended session each way."""
    gateway = FakeHomeware()
    router = homeware(gateway)
    router.connected()
    gateway.signed_in, gateway.signed_out_as = False, signed_out_as  # the gateway restarted
    assert len(router.connected()) == 3 and gateway.sign_ins == 2


def test_a_device_list_that_still_asks_to_sign_in_is_a_router_error_not_a_crash():
    gateway = FakeHomeware()
    gateway.forgets = True
    with pytest.raises(RouterError, match="still asks to sign in"):
        homeware(gateway).connected()


def test_a_page_with_no_devices_on_it_is_one_this_doesnt_understand_not_nobody_home():
    with pytest.raises(RouterError, match="lists no devices"):
        homeware(FakeHomeware(page="<html><body>Gateway</body></html>")).connected()


def test_a_proof_that_doesnt_match_closes_the_session_and_the_next_round_starts_afresh():
    gateway = FakeHomeware()
    gateway.wrong_proofs = 1
    router = homeware(gateway)
    with pytest.raises(RouterError, match="proof didn't match"):
        router.connected()
    assert len(router.connected()) == 3 and gateway.sign_ins == 1


def test_a_wrong_password_is_refused_and_not_tried_again_for_a_while(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("voice_assistant.routers.time.monotonic", lambda: clock[0])
    gateway = FakeHomeware()
    router = homeware(gateway, password="wrong")
    with pytest.raises(Refused, match="refused the password"):
        router.connected()
    asked = gateway.requests
    with pytest.raises(Refused, match="trying again"):
        router.connected()  # Homeware locks sign-in out after a few wrong ones
    assert gateway.requests == asked
    clock[0] += RETRY_REFUSED_S + 1
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
        make({"kind": "technicolor", "address": "192.168.1.1"}, "x", httpx.MockTransport(down)).connected()


# Xiaomi (MiWiFi): its hashed password, and its list of who's connected.


class FakeXiaomi:
    KEY, DEVICE_ID = "a2ffa5c9be07488bbb04a3a47d3c5f6a", "ca:fe:00:00:00:99"

    def __init__(self, password="hunter2!"):
        self.password, self.tokens, self.sign_ins, self.answer = password, [], 0, None
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
        if self.answer is not None:
            return httpx.Response(200, json=self.answer)
        if token != self.tokens[-1]:
            return httpx.Response(200, json={"code": 401, "msg": "Invalid token"})
        return httpx.Response(200, json={"code": 0, "list": self.devices})


def xiaomi(server: FakeXiaomi, password="hunter2!") -> Router:
    return make({"kind": "xiaomi", "address": "192.168.1.160"}, password, httpx.MockTransport(server))


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


@pytest.mark.parametrize(
    "answer, why",
    [
        ({"code": 1523, "msg": "no permission"}, "still asks to sign in"),  # signing in again doesn't help
        ({"code": 0}, "has no list"),  # no list at all is an answer this doesn't understand, not nobody connected
        ({"code": 0, "list": ["junk"]}, "doesn't understand"),
        ([1, 2], "doesn't understand"),
    ],
)
def test_a_xiaomi_answer_this_doesnt_understand_is_a_router_error_never_a_crash(answer, why):
    server = FakeXiaomi()
    server.answer = answer
    with pytest.raises(RouterError, match=why):
        xiaomi(server).connected()


def test_an_access_point_with_nobody_on_it_is_an_answer():
    server = FakeXiaomi()
    server.devices = []
    assert xiaomi(server).connected() == []


def test_a_xiaomi_that_turns_the_password_down_is_refused():
    with pytest.raises(Refused):
        xiaomi(FakeXiaomi(), password="wrong").connected()


def test_the_xiaomi_list_marks_anything_offline():
    answer = {"code": 0, "list": [{"mac": "AA:00:00:00:00:01", "online": "0"}, {"mac": "AA:00:00:00:00:02"}]}
    assert xiaomi_devices(answer) == [Listed("aa:00:00:00:00:01", "", False), Listed("aa:00:00:00:00:02", "", True)]


# The rules: home at once, out only after away_after unseen while every router answered, not known at first.


PEOPLE = People({"Alon": ["CA-FE-00-00-00-01"], "Stacey": ["Staceys-iphone", "92:e9:e8:00:00:02"]})
ALON_PHONE, STACEY_PHONE = Seen("ca:fe:00:00:00:01", "iPhone"), Seen("28:49:e9:00:00:03", "Staceys-iphone")
BOTH, ALON = {"alon", "stacey"}, {"alon"}


def tracker(last=None) -> Tracker:
    return Tracker(PEOPLE.names, 10 * MIN, 0.0, last)


def test_a_phone_is_known_by_its_address_however_written_or_by_the_name_a_router_lists_it_under():
    """A router can keep an old address under a phone's name: the name finds it."""
    assert PEOPLE.whose([ALON_PHONE, STACEY_PHONE, Seen("ff:00:00:00:00:00", "tv")]) == BOTH
    assert PEOPLE.whose([Seen("92:e9:e8:00:00:02", "")]) == {"stacey"}


def test_nobody_is_known_until_the_routers_answer_and_starting_up_isnt_an_arrival():
    t = tracker()
    assert t.states["alon"].home is None
    assert t.update(ALON, True, 0, NOW) == []  # home, but not "came home": TARS just started
    assert t.states["alon"] == State(True, None, NOW)
    t.update(ALON, True, 9 * MIN, NOW + 9 * MIN)
    assert t.states["stacey"].home is None  # not seen yet, and not for long enough to say she's out
    t.update(ALON, True, 10 * MIN, NOW + 10 * MIN)
    assert t.states["stacey"] == State(False, None, None)  # out, since some time before TARS was watching


def test_out_after_away_after_unseen_dated_when_last_seen_and_back_at_once():
    t = tracker()
    t.update(BOTH, True, 0, NOW)
    assert t.update(ALON, True, 9 * MIN, NOW + 9 * MIN) == []  # 9 minutes unseen: maybe a phone asleep
    assert t.update(ALON, True, 10 * MIN, NOW + 10 * MIN) == [("stacey", False)]
    assert t.states["stacey"] == State(False, NOW, NOW)  # out since about when she was last seen
    assert t.update(BOTH, True, 30 * MIN, NOW + 30 * MIN) == [("stacey", True)]
    assert t.states["stacey"] == State(True, NOW + 30 * MIN, NOW + 30 * MIN)


def test_a_router_that_doesnt_answer_never_sends_anyone_out():
    t = tracker()
    t.update(BOTH, True, 0, NOW)
    for minute in range(1, 30):
        assert t.update(ALON, False, minute * MIN, NOW + minute * MIN) == []  # her phone may be on the silent one
    assert t.states["stacey"].home is True
    # Unseen counts only from when every router answered again: ten whole minutes after the last silent round.
    assert t.update(ALON, True, 38 * MIN, NOW + 38 * MIN) == []
    assert t.update(ALON, True, 39 * MIN, NOW + 39 * MIN) == [("stacey", False)]


def test_the_clock_setting_itself_after_a_boot_doesnt_send_anyone_out():
    """A Pi has no clock of its own while it's off, so the time of day jumps when it sets itself: minutes are counted
    on a clock that doesn't."""
    t = tracker()
    t.update(BOTH, True, 0, NOW)
    assert t.update(ALON, True, 30.0, NOW + 3 * 24 * 3600) == []
    assert t.states["stacey"].home is True


def test_after_a_restart_it_starts_from_what_was_last_known():
    """Someone out when TARS restarted who comes home soon after has come home: their messages are said."""
    t = tracker({"stacey": State(False, NOW - 3600, NOW - 3600), "alon": State(True, NOW - 7200, NOW - 60)})
    assert t.update(BOTH, True, 0, NOW) == [("stacey", True)]
    assert t.states["alon"] == State(True, NOW - 7200, NOW)  # still home, since when they came home


# The board: what the assistant writes, and every process reads.


@pytest.fixture
def board(log):
    return Board(log.store, PEOPLE, clock=lambda: NOW)


def written(board, now=NOW, **states):
    board.write({p: State(*s) for p, s in states.items()}, [RouterStatus("192.168.1.1", "technicolor", now, 9)], now)


def test_the_board_says_who_is_home_and_since_when(board):
    written(board, alon=(True, None, NOW), stacey=(False, NOW - 3600, NOW - 3600))
    assert board.is_out("Stacey") and board.is_home("alon") and not board.is_out("nobody")
    summary = board.summary()
    assert summary.startswith("Alon: home; Stacey: out (since about ") and summary.endswith(")")
    assert board.routers() == {"192.168.1.1": RouterStatus("192.168.1.1", "technicolor", NOW, 9)}


def test_a_board_the_assistant_stopped_writing_knows_nothing(log):
    written(Board(log.store, PEOPLE), now=NOW - STALE_AFTER_S - 1, stacey=(False, None, None))
    stale = Board(log.store, PEOPLE, clock=lambda: NOW)
    assert not stale.is_out("stacey") and stale.summary() == "Alon: not known yet; Stacey: not known yet"


def house(board, tmp_path):
    return make_home(load_config(CONFIG, local=False), tmp_path, presence=Presence(PEOPLE, board, [], [], 10, 30))


def ask(device: str) -> dict:
    return {"device": device, "action": "status", **dict.fromkeys(SETTINGS)}


def test_who_is_home_is_a_status_only_device_in_the_house(board, tmp_path):
    written(board, alon=(True, None, NOW), stacey=(None, None, None))
    tools = house(board, tmp_path)
    assert list(tools.devices) == [DEVICE]
    assert tools.call(ask(DEVICE), Request()) == "who's home: Alon: home; Stacey: not known yet"
    assert tools.call({**ask(DEVICE), "action": "on"}, Request()).startswith("error: the who's home can do: status")
    assert "going by their phone" in tools.rules()


def test_a_database_that_fails_is_who_is_home_unknown_not_the_whole_house_failing(board, monkeypatch, tmp_path):
    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    tools = house(board, tmp_path)
    monkeypatch.setattr(board.store, "rows", locked)
    assert tools.call(ask("everything"), Request()) == (
        "who's home: unknown (who's home can't be read just now (database is locked))"
    )


# The watcher: one round asks every router, never raises, and says who came and went.


class FakeRouter:
    kind = "technicolor"

    def __init__(self, address, *rounds):
        self.address, self._rounds = address, list(rounds)

    def connected(self):
        answer = self._rounds.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def watcher(log, routers, at: list[float], on_change=None, left_out=0) -> Watcher:
    """Rounds at the times of day `at`, the monotonic clock keeping pace."""
    walls, monotonic = iter(at), iter(t - NOW for t in at)
    board = Board(log.store, PEOPLE, clock=lambda: at[-1])
    tracker_ = Tracker(PEOPLE.names, 10 * MIN, 0.0)
    return Watcher(routers, PEOPLE, tracker_, board, 30, on_change, walls.__next__, monotonic.__next__, left_out)


def test_a_round_asks_every_router_writes_the_board_and_says_who_came_and_went(log, capsys):
    changes = []
    main = FakeRouter("192.168.1.1", [ALON_PHONE, STACEY_PHONE], [ALON_PHONE], [ALON_PHONE, STACEY_PHONE])
    ap = FakeRouter("192.168.1.160", [], [], RouterError("no answer from 192.168.1.160: ConnectTimeout"))
    w = watcher(log, [main, ap], [NOW, NOW + 11 * MIN, NOW + 12 * MIN], lambda *c: changes.append(c))
    assert w.round() == []
    assert w.round() == [("stacey", False)]
    assert w.round() == [("stacey", True)]  # a router down can't hide that she's back
    assert changes == [("Stacey", False), ("Stacey", True)]
    statuses = w.board.routers()
    assert statuses["192.168.1.160"].error.startswith("no answer") and statuses["192.168.1.1"].connected == 2
    assert "(presence: Stacey went out)" in capsys.readouterr().out


def test_a_bug_in_one_routers_code_is_that_router_not_answering_and_the_watching_goes_on(log):
    broken = FakeRouter("192.168.1.160", AttributeError("'list' object has no attribute 'get'"))
    w = watcher(log, [FakeRouter("192.168.1.1", [ALON_PHONE]), broken], [NOW])
    assert w.round() == []
    assert w.board.routers()["192.168.1.160"].error == "AttributeError: 'list' object has no attribute 'get'"


def test_a_router_failing_is_said_once_not_every_round(log, capsys):
    down = RouterError("no answer from 192.168.1.1: ConnectError")
    w = watcher(log, [FakeRouter("192.168.1.1", down, down, [ALON_PHONE])], [NOW, NOW + 30, NOW + 60])
    for _ in range(3):
        w.round()
    out = capsys.readouterr().out
    assert out.count("can't say who's connected") == 1 and "answers again" in out


def test_a_router_left_out_for_want_of_its_password_keeps_anyone_from_being_marked_out(log):
    rounds = [NOW, NOW + 20 * MIN, NOW + 40 * MIN]
    w = watcher(log, [FakeRouter("192.168.1.1", *([[ALON_PHONE]] * 3))], rounds, left_out=1)
    for _ in range(3):
        assert w.round() == []
    assert w.tracker.states["stacey"].home is None  # her phone may be on the router TARS can't ask


def test_without_a_router_to_ask_nobody_can_be_waited_for_by_their_phone(log):
    spec = {"kind": "technicolor", "address": "192.168.1.1", "secret": "ROUTER_PASSWORD"}
    cfg = PresenceConfig(enabled=True, people={"Stacey": ["staceys-iphone"]}, routers=[spec])
    without = make_presence(cfg, log.store, lambda name: None)
    assert without.phones == [] and without.left_out == ["the router at 192.168.1.1: no ROUTER_PASSWORD in .env"]
    assert make_presence(cfg, log.store, {"ROUTER_PASSWORD": "x"}.get).phones == ["Stacey"]


# What it changes: reminders for someone out go to their chat; messages waiting for someone are said once home.


@pytest.fixture
def reminders(log):
    return Reminders(log.store)


def due_now(reminders, **kw):
    kw = {"kind": REMINDER, "text": "call the bank", "for_name": "stacey", **kw}
    return reminders.add(NewReminder(due=NOW + 1, **kw), VOICE, now=NOW)


def test_a_reminder_for_someone_out_with_a_chat_goes_to_their_chat_once(board, reminders):
    written(board, stacey=(False, NOW - 3600, NOW - 3600))
    to_chat = ChatWhenOut(reminders, board.is_out, ["Stacey", "Alon"])
    rid = due_now(reminders)
    assert to_chat(reminders.get(rid)) and reminders.get(rid)["channel"] == TELEGRAM
    assert not to_chat(reminders.get(rid))


def test_one_telegram_hands_back_stays_aloud_rather_than_bouncing_between_them(board, reminders):
    """Her chat turned the bot down for good: Telegram says it aloud after all, and it isn't sent back."""
    written(board, stacey=(False, NOW - 3600, NOW - 3600))
    to_chat = ChatWhenOut(reminders, board.is_out, ["Stacey"])
    rid = due_now(reminders)
    assert to_chat(reminders.get(rid)) and reminders.say_aloud(rid)
    assert not to_chat(reminders.get(rid)) and reminders.get(rid)["channel"] == VOICE


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
    assert not ChatWhenOut(reminders, board.is_out, chats)(reminders.get(rid))
    assert reminders.get(rid)["channel"] == VOICE


def message_for_when_back(reminders, for_name="stacey") -> int:
    return reminders.add(NewReminder(MESSAGE, "dinner's at eight", for_name, "alon", due=None), VOICE, [for_name])


def test_a_message_waiting_for_someone_is_due_soon_after_their_phone_comes_home(reminders):
    rid = message_for_when_back(reminders)
    assert reminders.due(NOW + 3600) == []
    on_arrival(reminders, grace_s=60)("Stacey", True)
    r = reminders.get(rid)
    assert r["status"] == SCHEDULED and r["next_at"] is not None
    assert [r["id"] for r in reminders.due(r["next_at"])] == [rid]
    assert reminders.held_for("stacey") == [r]  # and if she speaks to TARS first, it's said then


def test_going_out_or_coming_home_again_doesnt_reschedule_it(reminders):
    rid = message_for_when_back(reminders)
    changed = on_arrival(reminders, grace_s=60)
    changed("stacey", False)
    assert reminders.get(rid)["next_at"] is None
    changed("stacey", True)
    first = reminders.get(rid)["next_at"]
    assert reminders.arrived("stacey", first + 600) == []  # only what it scheduled now
    assert reminders.get(rid)["next_at"] == first


def test_someone_known_only_by_their_phone_can_be_left_a_message_for_when_theyre_back(reminders):
    args = {"kind": MESSAGE, "text": "call me", "for": "Stacey", "when_back": True}
    _, said = ReminderTools(reminders, phones=lambda: ["Stacey"]).call("remind", args, now=NOW)
    assert said.startswith("set for when Stacey is next heard or comes home")
    _, refused = ReminderTools(reminders).call("remind", args, now=NOW)
    assert refused.startswith("error: TARS doesn't know Stacey's voice yet")
    _, refused = ReminderTools(reminders, phones=lambda: ["Alon"]).call("remind", args, now=NOW)
    assert refused.startswith("error: TARS doesn't know Stacey's voice or phone yet")


def test_a_message_for_when_someone_is_back_who_is_home_already_is_said_in_a_minute(reminders):
    """Waiting for her to go out and come back in would be waiting for nothing."""
    tools = ReminderTools(reminders, phones=lambda: ["Stacey"], is_home=lambda who: who == "Stacey")
    args = {"kind": MESSAGE, "text": "call me", "for": "Stacey", "when_back": True}
    change, said = tools.call("remind", args, now=NOW)
    assert change.reminder.due == NOW + 60 and change.aloud
    assert said.startswith("set for ") and "aloud at home" in said


# Its settings.


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
            '[presence]\nevery_s = 300\n[presence.people]\nAlon = ["ca:fe:00:00:01"]\nStacey = []\n'
            '[[presence.routers]]\nkind = "netgear"\naddress = "192.168.1.1"\nsecret = "lowercase"\nname = "main"\n'
            '[[presence.routers]]\nkind = "xiaomi"\naddress = "192.168.1.1"\nsecret = "XIAOMI_PASSWORD"\n',
        )
    message = str(e.value.code)
    for problem in (
        "'ca:fe:00:00:01' isn't a hardware address",
        "'Stacey' needs their phone's address or name",
        "kind must be technicolor or xiaomi",
        "needs secret, the name of its password in .env",
        "has no setting name",
        "two routers have the address 192.168.1.1",
        "every_s must be between 5 and 120",
    ):
        assert problem in message


def test_the_settings_in_the_docs_load(tmp_path):
    cfg = presence_config(
        tmp_path,
        "[presence]\nenabled = true\n"
        '[presence.people]\nAlon = ["88-1E-5A-00-00-01", "Alons-iPhone"]\nStacey = ["28:49:e9:00:00:02"]\n'
        '[[presence.routers]]\nkind = "technicolor"\naddress = "192.168.1.1"\nuser = "admin"\n'
        'secret = "ROUTER_PASSWORD"\n'
        '[[presence.routers]]\nkind = "xiaomi"\naddress = "192.168.1.160"\nsecret = "XIAOMI_PASSWORD"\n',
    )
    assert cfg.presence.enabled and len(cfg.presence.routers) == 2


def test_a_name_with_dashes_is_a_name_and_an_address_with_dashes_is_checked_as_one(tmp_path):
    with pytest.raises(ConfigError, match="'aa-bb-cc-dd-ee' isn't a hardware address"):
        presence_config(tmp_path, '[presence.people]\nAlon = ["Alons-iPhone", "aa-bb-cc-dd-ee"]\n')
