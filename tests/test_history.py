"""A sensor's history: the span and period asked for, the table the model reads, and the home tool going through
it like a status, against a pretend Home Assistant's statistics."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from evals.house import TOKEN, URL, PretendHA, entities
from voice_assistant import history
from voice_assistant.config import HomeConfig
from voice_assistant.hass import HomeAssistant, entity_device
from voice_assistant.home import SETTINGS, HomeTools, Request

NOW = datetime(2026, 10, 31, 10, 30, tzinfo=UTC)


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def test_without_a_since_it_s_the_last_day_by_the_hour():
    start, end, period = history.span(None, None, None, NOW, UTC)
    assert (start, end, period) == (NOW - timedelta(days=1), NOW, "hour")


def test_times_without_an_offset_are_local_and_a_date_is_its_midnight():
    start, end, period = history.span("2026-10-30T22:00", "2026-10-31", None, NOW, UTC)
    assert start == datetime(2026, 10, 30, 22, tzinfo=UTC) and end == datetime(2026, 10, 31, tzinfo=UTC)
    assert period == "hour"


def test_a_span_too_long_for_its_period_is_told_in_bigger_ones():
    assert history.span("2026-10-01", None, "hour", NOW, UTC)[2] == "day"
    assert history.span("2025-01-01", None, None, NOW, UTC)[2] == "month"
    assert history.span("2026-09-01", None, "week", NOW, UTC)[2] == "week"


def test_never_past_now_and_what_it_can_t_use_is_said():
    assert history.span(None, "2027-01-01", None, NOW, UTC)[1] == NOW
    with pytest.raises(history.WhenError, match="2026-10-30T22:00"):
        history.span("last night", None, None, NOW, UTC)
    with pytest.raises(history.WhenError, match="before"):
        history.span("2026-10-31T11:00", "2026-10-31T09:00", None, NOW, UTC)
    with pytest.raises(history.WhenError, match="hour, day, week, month"):
        history.span(None, None, "fortnight", NOW, UTC)


def rows(start: datetime, values: list[float | None], step=timedelta(hours=1)) -> list[dict]:
    """A row a period from `start`; None leaves that period out, as Home Assistant does with no readings."""
    return [
        {"start": ms(start + step * i), "mean": v, "min": v - 1, "max": v + 2}
        for i, v in enumerate(values)
        if v is not None
    ]


def test_the_table_has_a_row_a_period_the_whole_span_and_where_there_was_nothing():
    start = datetime(2026, 10, 30, 22, tzinfo=UTC)
    co2 = history.Reading("Carbon dioxide", "ppm", rows(start, [600, 900, 1200, None, None, 800]))
    pm = history.Reading("PM2.5", "µg/m³", rows(start, [2.5, 3, 4, None, None, 3]))
    said = history.table("air quality", [co2, pm], start, start + timedelta(hours=6), "hour", UTC)
    lines = said.splitlines()
    assert lines[0].startswith("air quality: history from 2026-10-30 22:00 to 2026-10-31 04:00, by hour")
    assert lines[1] == "when | Carbon dioxide (ppm) | PM2.5 (µg/m³)"
    assert lines[2] == "Oct 30 10 PM | 600 (599-602) | 2.5 (1.5-4.5)"
    assert lines[4] == "Oct 31 12 AM | 1200 (1199-1202) | 4 (3-6)"
    assert "- Carbon dioxide (ppm): average 875, lowest 599 (Oct 30 10 PM), highest 1202 (Oct 31 12 AM)" in lines
    assert lines[-1] == "No readings: Oct 31 1 AM to Oct 31 3 AM."


def test_a_reading_missing_from_a_row_is_a_dash_and_none_at_all_is_said():
    start = datetime(2026, 10, 30, tzinfo=UTC)
    a = history.Reading("A", "", rows(start, [1, 2], timedelta(days=1)))
    b = history.Reading("B", "", rows(start, [5], timedelta(days=1)))
    said = history.table("x", [a, b], start, start + timedelta(days=2), "day", UTC)
    assert "Sat Oct 31 | 2 (1-4) | -" in said and "No readings" not in said
    nothing = history.table("x", [history.Reading("A", "", [])], start, start + timedelta(days=2), "day", UTC)
    assert nothing.endswith("no readings at all in that span (Home Assistant has none kept from then)")


def test_the_longest_gaps_are_listed_first_and_the_rest_counted():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    values = [1, None, 1, None, None, 1, None, 1, None, 1, None, 1, None, 1, None, None, None, 1]
    said = history.table(
        "x", [history.Reading("A", "", rows(start, values))], start, start + timedelta(hours=18), "hour", UTC
    )
    gaps = said.splitlines()[-1]
    assert gaps.startswith("No readings: Jan 1 2 PM to Jan 1 5 PM; Jan 1 3 AM to Jan 1 5 AM;")
    assert gaps.endswith("and 2 shorter.")


AIR = {"name": "air quality", "entity": "sensor.air_quality", "details": ["sensor.co2", "binary_sensor.window"]}


def air(statistics: dict) -> tuple[HomeTools, PretendHA]:
    states = entities({}) | {
        "sensor.air_quality": ("17", {"friendly_name": "Air quality index"}),
        "sensor.co2": ("620", {"friendly_name": "Carbon dioxide", "unit_of_measurement": "ppm"}),
        "binary_sensor.window": ("off", {"device_class": "window"}),
    }
    ha = PretendHA(states)
    ha.statistics = statistics
    device = entity_device(AIR["name"], AIR, HomeAssistant(URL, TOKEN, ha.transport, ha.command))
    return HomeTools(HomeConfig(url=URL), {AIR["name"]: device}), ha


def call(tools: HomeTools, request: Request | None = None, **settings) -> str:
    args = {"device": "air quality", "action": "history", **dict.fromkeys(SETTINGS), **settings}
    return tools.call(args, request or Request())


def test_the_home_tool_reads_a_sensor_s_history_and_its_numeric_details_at_once():
    night = datetime.now().astimezone().replace(hour=22, minute=0, second=0, microsecond=0) - timedelta(days=1)
    tools, ha = air({"sensor.air_quality": rows(night, [17, 22]), "sensor.co2": rows(night, [900, 1100])})
    since = night.replace(tzinfo=None).isoformat()
    until = (night + timedelta(hours=2)).replace(tzinfo=None).isoformat()
    said = call(tools, Request(may_act=lambda _: False), since=since, until=until)  # never waits for the turn
    assert "when | Air quality index | Carbon dioxide (ppm)" in said and "| 17 (16-19) | 900 (899-902)" in said
    asked = ha.asked[0]
    assert asked["statistic_ids"] == ["sensor.air_quality", "sensor.co2"] and asked["period"] == "hour"
    assert ha.calls == []  # nothing changed


def test_history_goes_through_after_a_web_search_and_its_errors_are_said():
    tools, ha = air({})
    searched = Request(read_the_web=True)
    assert "no readings at all" in call(tools, searched)
    assert call(tools, since="yesterday").startswith("error: 'yesterday' isn't a date or time")
    ha.down = True
    assert call(tools).startswith("error: Home Assistant didn't answer")


def test_a_device_without_history_says_so():
    tools, _ = air({})
    door = entity_device("window", {"entity": "binary_sensor.window"}, None)
    assert door.actions == ("status",) and "history" not in door.what()
    assert "history" in tools.devices["air quality"].what() and "last night" in tools.rules()


def test_summer_and_winter_each_keep_their_own_offset():
    israel = ZoneInfo("Asia/Jerusalem")
    assert history.when("2024-07-01T22:00", israel).utcoffset() == timedelta(hours=3)
    assert history.when("2024-12-01T22:00", israel).utcoffset() == timedelta(hours=2)
    months = [datetime(2024, m, 1, tzinfo=israel) for m in (3, 4, 10, 11)]  # either side of both changes
    reading = history.Reading("A", "", [{"start": ms(t), "mean": 1, "min": 1, "max": 1} for t in months])
    said = history.table("x", [reading], months[0], months[-1] + timedelta(days=30), "month", israel)
    assert [line.split(" |")[0] for line in said.splitlines()[2:6]] == ["Mar 2024", "Apr 2024", "Oct 2024", "Nov 2024"]


def test_a_period_starting_right_at_the_end_isn_t_in_it():
    start = datetime(2026, 10, 30, tzinfo=UTC)
    reading = history.Reading("A", "", rows(start, [1, 2, 3], timedelta(days=1)))
    said = history.table("x", [reading], start, start + timedelta(days=2), "day", UTC)
    assert "Sat Oct 31" in said and "Sun Nov 1" not in said
