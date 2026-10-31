"""The camera device: "show" sends a snapshot and "watch" a live link, both to whoever asked, through the turn's
Request.emit; it changes nothing in the house. Against a pretend Home Assistant: its camera_proxy for the still, and
an injected websocket for the signed live link."""

import httpx
import pytest

from voice_assistant.config import HomeConfig, _home_problems
from voice_assistant.conversations import FILE, LINK, SentItem
from voice_assistant.hass import Camera, HomeAssistant
from voice_assistant.home import HomeError, Request

URL, TOKEN = "http://homeassistant.local:8123", "test-token"
JPEG = b"\xff\xd8\xff\xe0" + b"pretend-jpeg" + b"\xff\xd9"


def ha(snapshot: bytes | None = JPEG, sign: object = "ok", status: int = 200) -> HomeAssistant:
    """A Home Assistant whose camera_proxy gives `snapshot` (or `status` when it's an error), and whose websocket
    signs a path when `sign` is "ok", gives nothing when None, or raises when it's an exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/camera_proxy/"):
            if status != 200:
                return httpx.Response(status, text="no")
            return httpx.Response(200, content=snapshot or b"", headers={"content-type": "image/jpeg"})
        if request.url.path.startswith("/api/states/"):
            return httpx.Response(200, json={"state": "idle", "attributes": {}})
        return httpx.Response(200, json={})

    def command(message: dict) -> object:
        if isinstance(sign, Exception):
            raise sign
        if sign is None:
            return {}
        return {"path": f"{message['path']}?authSig=signed-{message['expires']}"}

    return HomeAssistant(URL, TOKEN, httpx.MockTransport(handler), command=command)


def camera(spec: dict | None = None, **ha_kwargs) -> Camera:
    spec = {"name": "nursery", "entity": "camera.nursery", **(spec or {})}
    return Camera(spec["name"], spec, ha(**ha_kwargs))


def _unreachable() -> HomeAssistant:
    """A Home Assistant whose camera state is unavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": "unavailable", "attributes": {}})

    return HomeAssistant(URL, TOKEN, httpx.MockTransport(handler), command=lambda m: {})


class Collected:
    """A turn that keeps what a device emits, standing in for the reply's sent list."""

    def __init__(self):
        self.items: list[SentItem] = []
        self.request = Request(emit=self.items.append)


def test_show_sends_a_snapshot_as_an_image_to_whoever_asked():
    cam, turn = camera(), Collected()
    result = cam.call("show", {}, turn.request)
    assert result == "sent: a snapshot of the nursery is on the TARS page"
    (item,) = turn.items
    assert item.kind == FILE and item.mime == "image/jpeg"
    assert item.file_bytes == JPEG and item.file_name == "nursery.jpg"


def test_watch_sends_a_signed_live_link_that_expires():
    cam, turn = camera(spec={"watch_seconds": 120}), Collected()
    result = cam.call("watch", {}, turn.request)
    assert result == "sent: a link to watch the nursery live is on the TARS page"
    (item,) = turn.items
    assert item.kind == LINK
    assert item.url == f"{URL}/api/camera_proxy_stream/camera.nursery?authSig=signed-120"
    assert "2 minutes" in (item.description or "")


def test_watch_uses_the_default_window_when_none_is_set():
    cam, turn = camera(), Collected()
    cam.call("watch", {}, turn.request)
    assert turn.items[0].url.endswith("authSig=signed-600")


def test_show_fails_cleanly_when_the_camera_has_no_picture():
    cam, turn = camera(status=404), Collected()
    with pytest.raises(HomeError):
        cam.call("show", {}, turn.request)
    assert turn.items == []


def test_watch_fails_when_home_assistant_signs_nothing():
    cam, turn = camera(sign=None), Collected()
    with pytest.raises(HomeError):
        cam.call("watch", {}, turn.request)


def test_a_camera_needs_somewhere_to_send_to():
    cam = camera()
    with pytest.raises(HomeError):
        cam.call("show", {}, Request())  # no emit: a --check run, nowhere for the picture to go


def test_status_reads_the_camera_state():
    assert camera().status() == "nursery: idle; ask to show or watch it"


def test_status_says_when_the_camera_is_unreachable():
    cam = Camera("nursery", {"name": "nursery", "entity": "camera.nursery"}, _unreachable())
    assert cam.status() == "nursery: Home Assistant can't reach it"


def test_what_describes_both_actions_and_the_window():
    assert "snapshot" in camera().what() and "watch" in camera().what() and "10 minutes" in camera().what()


def test_the_camera_reads_nothing_and_changes_nothing():
    assert Camera.actions == ("status", "show", "watch")


# --- config validation ---------------------------------------------------------------------------------------------


def problems(device: dict, url: str = URL) -> list[str]:
    return _home_problems(HomeConfig(url=url, devices=[device]))


def test_a_good_camera_passes():
    assert problems({"name": "nursery", "kind": "camera", "entity": "camera.nursery", "watch_seconds": 300}) == []


def test_a_camera_needs_a_camera_entity():
    assert any("needs entity, a camera.*" in p for p in problems({"name": "n", "kind": "camera", "entity": "light.x"}))


def test_a_camera_needs_the_home_url():
    assert any("set [home] url" in p for p in problems({"name": "n", "kind": "camera", "entity": "camera.n"}, url=""))


def test_a_cameras_watch_window_is_bounded():
    bad = problems({"name": "n", "kind": "camera", "entity": "camera.n", "watch_seconds": 5})
    assert any("watch_seconds must be" in p for p in bad)


def test_a_camera_has_no_stray_settings():
    bad = problems({"name": "n", "kind": "camera", "entity": "camera.n", "colour": "blue"})
    assert any("has no setting colour" in p for p in bad)
