"""The street address nearest a point, from OpenStreetMap's Nominatim: where a phone is when it's away from every
place Home Assistant has a zone for ([[home.devices]] address = "openstreetmap" on a device tracker).

It sends the point, and only the point, to nominatim.openstreetmap.org, so it's off unless a device asks for it.
Nominatim's usage policy asks for at most one request a second and a User-Agent that says who's asking; a point
within SAME_PLACE_M of the last one asked about is answered from memory.
"""

import math
import threading
import time
from collections.abc import Callable

import httpx

OPENSTREETMAP = "openstreetmap"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "TARS home assistant (self-hosted)"
TIMEOUT_S = 5.0
MIN_GAP_S = 1.1  # Nominatim's policy: one request a second at most
SAME_PLACE_M = 50.0
LANGUAGES = "en,he"  # English names where the map has them, else the local ones


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """How far apart two points (latitude, longitude) are, in metres."""
    (lat1, lon1), (lat2, lon2) = (tuple(map(math.radians, p)) for p in (a, b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6_371_000 * 2 * math.asin(math.sqrt(h))


def worded(address: dict) -> str:
    """Nominatim's address parts in a few words: "12 Herzl Street, Netanya"; "" if it has neither street nor town."""
    street = address.get("road") or address.get("pedestrian") or address.get("footway") or ""
    if street.isdigit():  # a street the map knows only by its council code ("2379"), not a name anyone says
        street = ""
    number = address.get("house_number") or ""
    area = address.get("neighbourhood") or address.get("suburb") or ""
    town = address.get("city") or address.get("town") or address.get("village") or address.get("municipality") or ""
    first = f"{number} {street}".strip() if street else area
    return ", ".join(p for p in (first, town) if p)


class Addresses:
    """Asks Nominatim for the address nearest a point, one request at a time and no faster than its policy allows."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}, transport=transport)
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._asked: float | None = None
        self._last: tuple[tuple[float, float], str] | None = None

    def near(self, latitude: float, longitude: float) -> str:
        """The nearest address in a few words, or "" if Nominatim doesn't answer or knows nothing there."""
        point = (latitude, longitude)
        with self._lock:
            if self._last and metres(self._last[0], point) <= SAME_PLACE_M:
                return self._last[1]
            if self._asked is not None and (wait := MIN_GAP_S - (self._clock() - self._asked)) > 0:
                self._sleep(wait)
            self._asked = self._clock()
            try:
                response = self._http.get(
                    REVERSE_URL,
                    params={"lat": latitude, "lon": longitude, "format": "jsonv2", "zoom": 18},
                    headers={"Accept-Language": LANGUAGES},
                )
                response.raise_for_status()
                said = worded(response.json().get("address") or {})
            except (httpx.HTTPError, ValueError) as e:
                print(f"(places: no address from OpenStreetMap: {type(e).__name__})")
                return ""
            self._last = (point, said)
            return said


_shared: Addresses | None = None
_making = threading.Lock()


def shared() -> Addresses:
    """The one Addresses every device asks through, so together they keep to Nominatim's pace."""
    global _shared
    with _making:
        if _shared is None:
            _shared = Addresses()
        return _shared
