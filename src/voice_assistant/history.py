"""A sensor's past, as Home Assistant keeps it: its long-term statistics, an average, low and high for each hour (and
day, week, month), kept for as long as Home Assistant runs, unlike its state history, which goes after ten days.

The home tool's `history` action asks for a span and how finely (hass.py, Sensor), and gets back a short table the
model reads: one row a period, one column a reading (the device's own and its details), each an average with its
low and high, then each reading's average, lowest and highest over the whole span, and when, and the stretches with no
readings at all. A span too long for the period asked is told in bigger periods, so the table stays short enough to
read."""

from datetime import datetime, timedelta, tzinfo

# Home Assistant's periods, finest first, with about how long each is: the gaps and the coarsening go by them.
PERIODS = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=31),
}
# The most rows a table has: a span with more is told in the next period up.
MAX_ROWS = 48
# Without a "since", the last day.
DEFAULT_SPAN = timedelta(days=1)
# The most stretches without readings listed.
MAX_GAPS = 5


class WhenError(ValueError):
    """A since, until or every the history can't use: said back to the model, which can ask again."""


def when(text: str | None, tz: tzinfo | None = None) -> datetime | None:
    """A time the model wrote, as an aware datetime: "2026-10-30T22:00", "2026-10-30 22:00" or "2026-10-01" in local
    time, or with an offset of its own. None for none. `tz`: the zone it's in; None is this machine's, daylight
    saving and all (a summer date isn't in winter's offset)."""
    if not text or not str(text).strip():
        return None
    try:
        found = datetime.fromisoformat(str(text).strip())
    except ValueError:
        raise WhenError(f"{text!r} isn't a date or time: write it as 2026-10-30T22:00 or 2026-10-30") from None
    if found.tzinfo:
        return found
    return found.replace(tzinfo=tz) if tz else found.astimezone()


def span(
    since: str | None, until: str | None, every: str | None, now: datetime, tz: tzinfo | None = None
) -> tuple[datetime, datetime, str]:
    """The start, end and period to ask Home Assistant for: the last day by default, never past now, and in a period
    that gives at most MAX_ROWS rows (the one asked, or the next up)."""
    end = min(when(until, tz) or now, now)
    start = when(since, tz) or end - DEFAULT_SPAN
    if start >= end:
        raise WhenError(f"since ({start:%Y-%m-%d %H:%M}) must be before until ({end:%Y-%m-%d %H:%M})")
    if every and every not in PERIODS:
        raise WhenError(f"every must be one of {', '.join(PERIODS)}")
    names = list(PERIODS)
    for period in names[names.index(every) if every else 0 :]:
        if (end - start) / PERIODS[period] <= MAX_ROWS:
            return start, end, period
    return start, end, "month"


def _stamp(t: datetime, period: str) -> str:
    """When a row starts, as finely as its period: "Oct 30 10 PM", "Oct 30", "week of Oct 27", "Oct 2026"."""
    day = f"{t:%b} {t.day}"
    if period == "hour":
        return f"{day} {t:%I %p}".replace(" 0", " ")
    if period == "day":
        return f"{t:%a} {day}"
    if period == "week":
        return f"week of {day} {t.year}"
    return f"{t:%b %Y}"


def _number(x: float | None) -> str:
    if x is None:
        return "?"
    return f"{x:.0f}" if abs(x) >= 10 else f"{x:.1f}".removesuffix(".0")


class Reading:
    """One column: an entity's name and unit, and its rows as Home Assistant gave them."""

    def __init__(self, name: str, unit: str, rows: list[dict]):
        self.name, self.unit, self.rows = name, unit, rows

    @property
    def label(self) -> str:
        return f"{self.name} ({self.unit})" if self.unit else self.name


def table(
    device: str, readings: list[Reading], start: datetime, end: datetime, period: str, tz: tzinfo | None = None
) -> str:
    """What the model is told: the span, a row a period (average, low-high), the whole span's average, lowest and
    highest and when, and where there were no readings. Times are in `tz`, None for this machine's zone."""
    head = f"{device}: history from {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}, by {period}"
    # Home Assistant also gives the period that starts right at the end: "until March 1" isn't March.
    last = end.timestamp() * 1000
    readings = [Reading(r.name, r.unit, [row for row in r.rows if row["start"] < last]) for r in readings]
    readings = [r for r in readings if r.rows]
    if not readings:
        return f"{head}: no readings at all in that span (Home Assistant has none kept from then)"
    starts = sorted({row["start"] for r in readings for row in r.rows})
    by_start = [{row["start"]: row for row in r.rows} for r in readings]
    lines = [
        f"{head}. Each cell is the average, then the low-high, for that {period}.",
        " | ".join(["when", *(r.label for r in readings)]),
    ]
    for ms in starts:
        cells = []
        for rows in by_start:
            row = rows.get(ms)
            cells.append(
                f"{_number(row.get('mean'))} ({_number(row.get('min'))}-{_number(row.get('max'))})" if row else "-"
            )
        lines.append(" | ".join([_stamp(_at(ms, tz), period), *cells]))
    lines.append("Over the whole span:")
    for r in readings:
        means = [row["mean"] for row in r.rows if row.get("mean") is not None]
        lows = [row for row in r.rows if row.get("min") is not None]
        highs = [row for row in r.rows if row.get("max") is not None]
        parts = [f"average {_number(sum(means) / len(means))}"] if means else []
        if lows:
            low = min(lows, key=lambda row: row["min"])
            parts.append(f"lowest {_number(low['min'])} ({_stamp(_at(low['start'], tz), period)})")
        if highs:
            high = max(highs, key=lambda row: row["max"])
            parts.append(f"highest {_number(high['max'])} ({_stamp(_at(high['start'], tz), period)})")
        lines.append(f"- {r.label}: {', '.join(parts)}")
    if gaps := _gaps(starts, start, end, period, tz):
        lines.append(f"No readings: {'; '.join(gaps)}.")
    return "\n".join(lines)


def _at(ms: float, tz: tzinfo | None) -> datetime:
    """Home Assistant's milliseconds as a local time, in the offset of that day: a month starting in summer is still
    that month, not the last hour of the one before."""
    return datetime.fromtimestamp(ms / 1000, tz) if tz else datetime.fromtimestamp(ms / 1000).astimezone()


def _gaps(starts: list[int], start: datetime, end: datetime, period: str, tz: tzinfo | None) -> list[str]:
    """The stretches of a period or more with no row, from the span's start to its end, at most MAX_GAPS, the
    longest first. A month is taken as 31 days, so a gap needs most of one (a missing February counts)."""
    step = PERIODS[period]
    found = []
    covered = start  # readings up to here
    for t in [*(_at(ms, tz) for ms in starts), end]:
        if t - covered >= step * 0.8:
            found.append((t - covered, covered, t))
        covered = max(covered, t + step)
    found.sort(key=lambda g: g[0], reverse=True)
    said = [f"{_stamp(b, period)} to {_stamp(a, period)}" for _, b, a in found[:MAX_GAPS]]
    return said + ([f"and {len(found) - MAX_GAPS} shorter"] if len(found) > MAX_GAPS else [])
