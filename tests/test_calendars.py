"""src/calendars.py: reading linked calendars, your own busy times, and their effect."""
from __future__ import annotations

import stat
from datetime import date, timedelta

import pytest
import requests

from src import calendars, health
from src import store as store_mod
from src.prioritize import score_all
from src.timeline import free_minutes
from src.web import server
from tests.conftest import at
from tests.helpers import mk

SECRET = "https://calendar.example.com/private-8f3a1c/basic.ics"


def ics(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n" + "".join(
        "BEGIN:VEVENT\r\n" + e.strip().replace("\n", "\r\n") + "\r\nEND:VEVENT\r\n"
        for e in events) + "END:VCALENDAR\r\n"


def read(text, lo="2026-10-01 00:00", hi="2026-12-01 00:00"):
    from tests.conftest import DENVER
    return calendars.events(text, DENVER, at(lo), at(hi))


def spans(blocks):
    return [(b.start.strftime("%m-%d %H:%M"), b.end.strftime("%H:%M")) for b in blocks]


def test_utc_event_lands_in_local_time():
    got = read(ics("""
UID:a1
SUMMARY:Choir   practice
DTSTART:20261014T230000Z
DTEND:20261015T003000Z
"""))
    assert spans(got) == [("10-14 17:00", "18:30")]
    assert got[0].title == "Choir practice" and got[0].kind == "calendar"


def test_weekly_repeat_keeps_wall_clock_across_the_time_change():
    got = read(ics("""
UID:a2
SUMMARY:Lab shift
DTSTART;TZID=America/Denver:20261019T090000
DTEND;TZID=America/Denver:20261019T100000
RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261110T065959Z
"""))
    assert spans(got) == [("10-19 09:00", "10:00"), ("10-26 09:00", "10:00"),
                          ("11-02 09:00", "10:00"), ("11-09 09:00", "10:00")]


def test_repeat_in_another_zone_is_converted_each_time():
    got = read(ics("""
UID:a3
SUMMARY:Family call
DTSTART;TZID=America/New_York:20261025T190000
DURATION:PT45M
RRULE:FREQ=WEEKLY;COUNT=2
"""))
    assert spans(got) == [("10-25 17:00", "17:45"), ("11-01 17:00", "17:45")]


def test_skipped_and_moved_occurrences():
    got = read(ics("""
UID:a4
SUMMARY:Tutoring
DTSTART;TZID=America/Denver:20261005T160000
DTEND;TZID=America/Denver:20261005T170000
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=America/Denver:20261012T160000
""", """
UID:a4
SUMMARY:Tutoring (moved)
RECURRENCE-ID;TZID=America/Denver:20261019T160000
DTSTART;TZID=America/Denver:20261020T180000
DTEND;TZID=America/Denver:20261020T190000
"""))
    assert spans(got) == [("10-05 16:00", "17:00"), ("10-20 18:00", "19:00"),
                          ("10-26 16:00", "17:00")]
    assert got[1].title == "Tutoring (moved)"


def test_date_only_until_includes_that_day():
    got = read(ics("""
UID:a5
SUMMARY:Rehearsal
DTSTART:20261006T190000
DTEND:20261006T200000
RRULE:FREQ=DAILY;UNTIL=20261008
"""))
    assert spans(got) == [("10-06 19:00", "20:00"), ("10-07 19:00", "20:00"),
                          ("10-08 19:00", "20:00")]


def test_all_day_free_and_cancelled_events_do_not_block():
    got = read(ics("""
UID:b1
SUMMARY:Fall break
DTSTART;VALUE=DATE:20261015
DTEND;VALUE=DATE:20261017
""", """
UID:b2
SUMMARY:Reminder
DTSTART:20261015T150000Z
DTEND:20261015T160000Z
TRANSP:TRANSPARENT
""", """
UID:b3
SUMMARY:Called off
DTSTART:20261015T170000Z
DTEND:20261015T180000Z
STATUS:CANCELLED
""", """
UID:b4
SUMMARY:No length
DTSTART:20261015T190000Z
"""))
    assert got == []


def test_only_events_touching_the_window_are_returned():
    text = ics("""
UID:c1
SUMMARY:Standup
DTSTART;TZID=America/Denver:20200106T080000
DTEND;TZID=America/Denver:20200106T083000
RRULE:FREQ=WEEKLY;BYDAY=MO
""")
    got = read(text, "2026-11-01 00:00", "2026-11-15 00:00")
    assert spans(got) == [("11-02 08:00", "08:30"), ("11-09 08:00", "08:30")]


def test_own_blocks_weekly_and_one_off(cfg):
    calendars.add_block({"name": " Gym ", "days": ["wed", "Mon"], "start": "17:00",
                         "end": "18:00"})
    calendars.add_block({"name": "Dentist", "date": "2026-10-13", "start": "10:00",
                         "end": "11:00"})
    busy = calendars.busy_by_day(cfg, date(2026, 10, 12), date(2026, 10, 18))
    assert {d.isoformat(): [b.title for b in v] for d, v in busy.items()} == {
        "2026-10-12": ["Gym"], "2026-10-13": ["Dentist"], "2026-10-14": ["Gym"]}
    saved = calendars.load()["blocks"]
    assert saved[0]["days"] == ["mon", "wed"] and saved[0]["name"] == "Gym"
    assert calendars.remove_block(saved[0]["id"]) is True
    assert calendars.remove_block(saved[0]["id"]) is False
    assert list(calendars.busy_by_day(cfg, date(2026, 10, 12), date(2026, 10, 18))) == [
        date(2026, 10, 13)]


@pytest.mark.parametrize("data,msg", [
    ({"days": ["mon"], "start": "09:00", "end": "10:00"}, "name"),
    ({"name": "x", "days": ["mon"], "start": "9", "end": "10:00"}, "start and an end"),
    ({"name": "x", "days": ["mon"], "start": "10:00", "end": "10:00"}, "end after"),
    ({"name": "x", "start": "09:00", "end": "10:00"}, "Pick a date"),
    ({"name": "x", "days": ["someday"], "start": "09:00", "end": "10:00"}, "Pick a date"),
    ({"name": "x", "date": "tomorrow", "start": "09:00", "end": "10:00"}, "Pick the date"),
])
def test_add_block_rejects_bad_input(cfg, data, msg):
    with pytest.raises(calendars.Invalid, match=msg):
        calendars.add_block(data)
    assert calendars.load()["blocks"] == []


@pytest.fixture
def feed(monkeypatch):
    """What the linked calendar's server answers; set .text or .error."""
    class Feed:
        text = ics("""
UID:d1
SUMMARY:Orchestra
DTSTART;TZID=America/Denver:20261013T190000
DTEND;TZID=America/Denver:20261013T210000
""")
        error = None
        asked = []

    def download(url, timeout):
        Feed.asked.append(url)
        if Feed.error:
            raise calendars.Invalid(Feed.error)
        return Feed.text

    Feed.asked = []
    monkeypatch.setattr(calendars, "_download", download)
    return Feed


def test_add_link_saves_privately_and_never_exposes_the_address(cfg, feed):
    link = calendars.add_link("  School  ", "webcal://calendar.example.com/private-8f3a1c/basic.ics")
    assert feed.asked == [SECRET] and link["name"] == "School"
    assert stat.S_IMODE(calendars._file().stat().st_mode) == 0o600
    public = calendars.links_public()
    assert public == [{"id": link["id"], "name": "School", "host": "calendar.example.com",
                       "ok": True, "error": "", "last_ok": public[0]["last_ok"]}]
    assert "private-8f3a1c" not in str(public)
    assert "private-8f3a1c" not in str(calendars.view(cfg)) + str(calendars.meta(cfg))
    busy = calendars.busy_by_day(cfg, date(2026, 10, 12), date(2026, 10, 18))
    assert [(b.title, b.note) for b in busy[date(2026, 10, 13)]] == [("Orchestra", "School")]


@pytest.mark.parametrize("url", ["", "http://calendar.example.com/a.ics", "calendar",
                                 "file:///etc/hosts", "https://"])
def test_add_link_wants_an_https_address(cfg, feed, url):
    with pytest.raises(calendars.Invalid, match="starts with https"):
        calendars.add_link("x", url)
    assert feed.asked == []


def test_add_link_refuses_a_duplicate_and_a_failing_link(cfg, feed):
    calendars.add_link("A", SECRET)
    with pytest.raises(calendars.Invalid, match="already linked"):
        calendars.add_link("B", SECRET)
    feed.error = "The calendar could not be reached."
    with pytest.raises(calendars.Invalid, match="could not be reached"):
        calendars.add_link("C", SECRET + "?x=1")
    assert [link["name"] for link in calendars.load()["links"]] == ["A"]


def test_failed_refresh_keeps_the_last_good_copy(cfg, feed):
    link = calendars.add_link("School", SECRET)
    feed.error = "The calendar's server answered 404."
    assert calendars.refresh() == [{"name": "School", "ok": False,
                                    "error": "The calendar's server answered 404."}]
    row = calendars.links_public()[0]
    assert row["ok"] is False and row["last_ok"] and "404" in row["error"]
    assert date(2026, 10, 13) in calendars.busy_by_day(cfg, date(2026, 10, 12),
                                                       date(2026, 10, 18))
    feed.error = None
    assert calendars.refresh()[0]["ok"] is True
    assert calendars.links_public()[0]["ok"] is True
    assert calendars.refresh(max_age_minutes=15) == []
    assert calendars.remove_link(link["id"]) is True
    assert not calendars._cache(link["id"]).exists() and calendars.links_public() == []


def test_download_errors_never_repeat_the_link(monkeypatch):
    def boom(url, **kw):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
    monkeypatch.setattr(calendars.requests, "get", boom)
    with pytest.raises(calendars.Invalid) as e:
        calendars._download(SECRET, 5)
    assert "private-8f3a1c" not in str(e.value) and "example.com" not in str(e.value)


def test_a_web_page_is_not_accepted_as_a_calendar(monkeypatch):
    class Page:
        status_code = 200

        class raw:
            @staticmethod
            def read(n, decode_content=True):
                return b"<!doctype html><html>Sign in</html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(calendars.requests, "get", lambda url, **kw: Page())
    with pytest.raises(calendars.Invalid, match="web page, not a calendar"):
        calendars._download(SECRET, 5)


def test_an_event_over_midnight_counts_on_both_days(cfg, feed):
    feed.text = ics("""
UID:e1
SUMMARY:Night shift
DTSTART;TZID=America/Denver:20261013T200000
DTEND;TZID=America/Denver:20261014T080000
""")
    calendars.add_link("Work", SECRET)
    busy = calendars.busy_by_day(cfg, date(2026, 10, 12), date(2026, 10, 18))
    assert sorted(busy) == [date(2026, 10, 13), date(2026, 10, 14)]
    before = free_minutes(cfg, date(2026, 10, 14))
    assert free_minutes(cfg, date(2026, 10, 14), busy[date(2026, 10, 14)]) < before


def test_busy_time_shrinks_the_day_and_the_slack(cfg):
    day = date(2026, 10, 13)
    calendars.add_block({"name": "Shift", "date": day.isoformat(), "start": "14:00",
                         "end": "18:00"})
    busy = calendars.busy_by_day(cfg, day, day + timedelta(days=7))
    assert free_minutes(cfg, day) - free_minutes(cfg, day, busy[day]) == 240

    task = mk("canvas:1", due="2026-10-14 23:59", est_minutes=120)
    plain = score_all(cfg, [task], now=at("2026-10-13 06:00"))[0]
    with_cal = score_all(cfg, [task], now=at("2026-10-13 06:00"), external_by_day=busy)[0]
    assert plain.slack_hours - with_cal.slack_hours == pytest.approx(4.0)
    assert with_cal.urgency > plain.urgency


def test_busy_time_after_the_deadline_changes_nothing(cfg):
    calendars.add_block({"name": "Trip", "date": "2026-10-16", "start": "09:00",
                         "end": "17:00"})
    busy = calendars.busy_by_day(cfg, date(2026, 10, 13), date(2026, 10, 20))
    task = mk("canvas:1", due="2026-10-14 23:59", est_minutes=120)
    plain = score_all(cfg, [task], now=at("2026-10-13 06:00"))[0]
    with_cal = score_all(cfg, [task], now=at("2026-10-13 06:00"), external_by_day=busy)[0]
    assert with_cal.slack_hours == plain.slack_hours and with_cal.score == plain.score


def test_view_is_one_week_with_every_block_and_the_days_hours(cfg):
    today = cfg.now().date()
    calendars.add_block({"name": "Shift", "date": today.isoformat(), "start": "14:00",
                         "end": "15:00"})
    v = calendars.view(cfg)
    assert len(v["days"]) == 7 and v["days"][0]["date"] == today.isoformat()
    kinds = {b["kind"] for d in v["days"] for b in d["blocks"]}
    assert {"busy", "class", "meal", "travel"} <= kinds
    shift = next(b for b in v["days"][0]["blocks"] if b["title"] == "Shift")
    assert shift["id"] == calendars.load()["blocks"][0]["id"] and shift["from"] == ""
    m = calendars.meta(cfg)
    assert (m["wake"], m["sleep"], v["start"]) == ("07:00", "23:00", today.isoformat())
    assert v["days"][0]["free_minutes"] == free_minutes(
        cfg, today, calendars.busy_by_day(cfg, today)[today])


def test_status_report_lists_linked_calendars(cfg, feed, tmp_path):
    calendars.add_link("School", SECRET)
    feed.error = "The calendar did not answer in time."
    calendars.refresh()
    rows = [r for r in health.report(cfg, store_mod.Store(tmp_path / "data" / "store.json"))
            if r["name"].startswith("cal:")]
    assert len(rows) == 1 and rows[0]["label"] == "Calendar · School"
    assert rows[0]["ok"] is False and "did not answer" in rows[0]["detail"]


def test_calendar_actions_over_the_api(cfg, feed):
    assert server.calendar_action({"action": "add_block", "name": "Gym", "days": ["mon"],
                                   "start": "17:00", "end": "18:00"}) == {"ok": True}
    bad = server.calendar_action({"action": "add_block", "name": "Gym"})
    assert bad["ok"] is False and "start and an end" in bad["error"]
    assert server.calendar_action({"action": "add_link", "name": "School",
                                   "url": SECRET}) == {"ok": True}
    block, link = calendars.load()["blocks"][0], calendars.load()["links"][0]
    feed.error = "The calendar could not be reached."
    assert server.calendar_action({"action": "refresh"}) == {
        "ok": False, "error": "School: The calendar could not be reached."}
    assert server.calendar_action({"action": "remove_block", "id": block["id"]}) == {"ok": True}
    assert server.calendar_action({"action": "remove_block", "id": block["id"]})["ok"] is False
    assert server.calendar_action({"action": "remove_link", "id": link["id"]}) == {"ok": True}
    assert server.calendar_action({"action": "nope"})["ok"] is False
    assert calendars.load() == {"links": [], "blocks": [], "started": True}


def test_the_page_asks_how_to_start_until_something_is_chosen(cfg, feed):
    assert calendars.meta(cfg)["started"] is False
    assert server.calendar_action({"action": "start"}) == {"ok": True}
    assert calendars.meta(cfg)["started"] is True


def test_linking_a_calendar_counts_as_started(cfg, feed):
    link = calendars.add_link("School", SECRET)
    assert calendars.meta(cfg)["started"] is True
    calendars.remove_link(link["id"])
    assert calendars.meta(cfg)["started"] is True


def test_any_day_week_or_month_can_be_asked_for(cfg, planner):
    today = cfg.now().date()
    past = (today - timedelta(days=10)).isoformat()
    assert server.calendar_range(past, "1") == calendars.view(cfg, today - timedelta(days=10), 1)
    assert [len(server.calendar_range(past, n)["days"]) for n in ("1", "7", "42", "900", "x")] == [
        1, 7, 42, 42, 7]
    assert server.calendar_range("not a date", "7")["start"] == today.isoformat()
    assert server.calendar_range("2099-01-01", "7")["start"] == (
        today + timedelta(days=calendars.HORIZON_DAYS)).isoformat()
    assert server.calendar_range("1999-01-01", "7")["start"] == (
        today - timedelta(days=400)).isoformat()


def weekly(cfg):
    b = calendars.add_block({"name": "Gym", "days": ["mon", "wed"], "start": "17:00",
                             "end": "18:00"})
    return b, lambda: {d.isoformat(): [(x.title, x.start.strftime("%H:%M"), x.end.strftime("%H:%M"))
                                       for x in v]
                       for d, v in sorted(calendars.busy_by_day(
                           cfg, date(2026, 10, 12), date(2026, 10, 18)).items())}


def test_deleting_one_day_of_a_weekly_entry(cfg):
    b, week = weekly(cfg)
    calendars.skip_block(b["id"], "2026-10-12")
    assert week() == {"2026-10-14": [("Gym", "17:00", "18:00")]}
    assert calendars.load()["blocks"][0]["skip"] == ["2026-10-12"]
    one = calendars.add_block({"name": "Dentist", "date": "2026-10-13", "start": "10:00",
                               "end": "11:00"})
    calendars.skip_block(one["id"], "2026-10-13")
    assert [x["name"] for x in calendars.load()["blocks"]] == ["Gym"]
    with pytest.raises(calendars.Invalid, match="already gone"):
        calendars.skip_block(one["id"], "2026-10-13")


def test_changing_one_day_of_a_weekly_entry(cfg):
    b, week = weekly(cfg)
    calendars.detach_block(b["id"], "2026-10-12", {"date": "2026-10-13", "start": "18:00",
                                                   "end": "19:30"})
    assert week() == {"2026-10-13": [("Gym", "18:00", "19:30")],
                      "2026-10-14": [("Gym", "17:00", "18:00")]}


def test_changing_every_week_and_turning_one_time_into_weekly(cfg):
    b, week = weekly(cfg)
    calendars.skip_block(b["id"], "2026-10-14")
    calendars.update_block(b["id"], {"start": "06:00", "end": "07:00", "days": ["tue", "wed"]})
    assert week() == {"2026-10-13": [("Gym", "06:00", "07:00")]}
    calendars.update_block(b["id"], {"name": "Swim"})
    assert calendars.load()["blocks"][0]["name"] == "Swim"
    calendars.update_block(b["id"], {"date": "2026-10-16"})
    assert week() == {"2026-10-16": [("Swim", "06:00", "07:00")]}
    assert "skip" not in calendars.load()["blocks"][0] and "days" not in calendars.load()["blocks"][0]
    calendars.update_block(b["id"], {"days": ["fri"]})
    assert calendars.load()["blocks"][0]["days"] == ["fri"]
    with pytest.raises(calendars.Invalid, match="end after"):
        calendars.update_block(b["id"], {"start": "09:00", "end": "08:00"})
    assert calendars.load()["blocks"][0]["start"] == "06:00"


def test_undo_puts_the_earlier_entries_back(cfg):
    b, week = weekly(cfg)
    before = calendars.load()["blocks"]
    assert server.calendar_action({"action": "remove_block", "id": b["id"]}) == {"ok": True}
    assert week() == {}
    assert server.calendar_action({"action": "set_blocks", "blocks": before}) == {"ok": True}
    assert calendars.load()["blocks"] == before
    bad = server.calendar_action({"action": "set_blocks", "blocks": [{"name": ""}]})
    assert bad["ok"] is False and calendars.load()["blocks"] == before


def test_edit_actions_over_the_api(cfg):
    b, week = weekly(cfg)
    act = server.calendar_action
    assert act({"action": "update_block", "id": b["id"], "start": "16:00", "end": "17:00",
                "days": ["mon"]}) == {"ok": True}
    assert act({"action": "detach_block", "id": b["id"], "on": "2026-10-12", "date": "2026-10-12",
                "start": "20:00", "end": "21:00"}) == {"ok": True}
    assert week() == {"2026-10-12": [("Gym", "20:00", "21:00")]}
    assert act({"action": "skip_block", "id": "nope", "on": "2026-10-12"})["ok"] is False


@pytest.fixture
def planner(tmp_path, monkeypatch, cfg):
    from src import daily
    path = tmp_path / "data" / "store.json"

    class TmpStore(store_mod.Store):
        def __init__(self, p=None, origin="cli"):
            super().__init__(p or path, origin)

    out = tmp_path / "out"
    out.mkdir()
    sent = []

    def answer(prompt, schema, model, timeout, which=""):
        sent.append(prompt)
        return {"day_summary": "x", "ranked": [], "later": []}

    for mod in (daily, server):
        monkeypatch.setattr(mod, "Store", TmpStore)
        monkeypatch.setattr(mod, "OUT_DIR", out)
    monkeypatch.setattr(server, "Config", lambda: cfg)
    monkeypatch.setattr(daily, "ContentStore", lambda: None)
    monkeypatch.setattr(daily.journal, "recent", lambda **kw: [])
    monkeypatch.setattr(daily.agent, "run_json", answer)
    due = (cfg.now() + timedelta(days=1)).strftime("%Y-%m-%d 23:59")
    with store_mod.Store.edit("seed", path) as st:
        st.upsert([mk("canvas:1", title="HW 1", due=due, est_minutes=120)])
    return sent


def test_the_plan_is_told_less_free_time_but_no_event_names(cfg, planner):
    from src import daily
    today = cfg.now().date()
    assert daily.run(cfg, out=lambda *a: None) == 0
    before = daily.read_plan(today)["free_minutes"]
    calendars.add_block({"name": "Zumba with Priya", "date": today.isoformat(),
                         "start": "19:00", "end": "21:00"})
    assert daily.run(cfg, out=lambda *a: None) == 0
    after = daily.read_plan(today)["free_minutes"]
    assert after < before and after == free_minutes(
        cfg, today, calendars.busy_by_day(cfg, today)[today])
    assert f"About {after} minutes" in planner[1]
    assert "Zumba" not in planner[1] and "Priya" not in planner[1]


def test_app_state_carries_the_calendar_and_todays_free_time(cfg, planner):
    today = cfg.now().date()
    plain = server.state()
    calendars.add_block({"name": "Shift", "date": today.isoformat(), "start": "19:00",
                         "end": "21:00"})
    busy = server.state()
    assert busy["free_today"] < plain["free_today"]
    assert busy["free_today"] == calendars.view(cfg)["days"][0]["free_minutes"]
    assert [b["name"] for b in busy["calendar"]["blocks"]] == ["Shift"]
    slack = lambda s: s["tasks"][0]["slack_hours"]
    assert slack(busy) <= slack(plain)


WEEK = '''timezone: America/Denver
term: {start: 2026-09-01, end: 2026-12-18}
day:
  wake: "07:00"
  sleep: "23:00"
recurring:
  # --- Classes ---
  - {name: "AAA 101", kind: class, course: AAA101,
     days: [mon, wed, fri], start: "09:00", end: "09:50", location: campus}

  - {name: "Work", kind: work, days: [fri],
     start: "13:00", end: "17:00", location: work}   # shift
  - name: Choir
    kind: standing
    days:
      - tue
    start: "19:00"
    end: "20:00"   # hall B
meals:
  - {name: breakfast, at: "07:30", minutes: 25, flex: 45}
  - {name: lunch,     at: "12:30", minutes: 30, flex: 90}   # usually later
one_offs: []
'''


@pytest.fixture
def week(cfg, tmp_path):
    from src import settings_edit
    path = tmp_path / "config" / "commitments.yaml"
    path.write_text(WEEK)
    settings_edit._undo.clear()
    return path


def rows():
    from src import settings_edit
    return {r["name"]: r for r in settings_edit.week_rows()}


def was(name):
    r = rows()[name]
    return {"name": r["name"], "start": r["start"], "end": r["end"]}


def test_moving_a_class_every_week_edits_only_that_entry(week):
    assert server.calendar_action({"action": "week_update", "i": 0, "was": was("AAA 101"),
                                   "days": ["tue", "thu"], "start": "10:00",
                                   "end": "11:15"}) == {"ok": True}
    assert rows()["AAA 101"] == {"i": 0, "name": "AAA 101", "kind": "class",
                                 "days": ["tue", "thu"], "start": "10:00", "end": "11:15",
                                 "skip": [], "until": ""}
    text = week.read_text()
    assert "# --- Classes ---" in text and "# shift" in text and "# hall B" in text
    assert text.replace('days: [tue, thu], start: "10:00", end: "11:15"',
                        'days: [mon, wed, fri], start: "09:00", end: "09:50"') == WEEK
    assert server.calendar_action({"action": "week_update", "i": 2, "was": was("Choir"),
                                   "days": ["tue", "sun"], "end": "20:30"}) == {"ok": True}
    assert rows()["Choir"]["days"] == ["tue", "sun"] and rows()["Choir"]["end"] == "20:30"
    assert "# hall B" in week.read_text()


def test_skipping_one_day_of_a_class_or_shift(week, cfg):
    from src import config
    from src.timeline import expand_recurring
    act = server.calendar_action
    assert act({"action": "week_skip", "i": 0, "was": was("AAA 101"), "on": "2026-10-14"}) == {"ok": True}
    assert act({"action": "week_skip", "i": 0, "was": was("AAA 101"), "on": "2026-10-12"}) == {"ok": True}
    assert act({"action": "week_skip", "i": 2, "was": was("Choir"), "on": "2026-10-13"}) == {"ok": True}
    assert rows()["AAA 101"]["skip"] == ["2026-10-12", "2026-10-14"]
    assert rows()["Choir"]["skip"] == ["2026-10-13"]
    fresh = config.Config()
    names = lambda d: [b.title for b in expand_recurring(fresh, d)]
    assert names(date(2026, 10, 12)) == [] and names(date(2026, 10, 13)) == []
    assert names(date(2026, 10, 16)) == ["AAA 101", "Work"]
    assert "# shift" in week.read_text() and "# hall B" in week.read_text()


def test_removing_an_entry_and_undoing_it(week):
    act = server.calendar_action
    assert act({"action": "week_delete", "i": 1, "was": was("Work")}) == {"ok": True}
    assert list(rows()) == ["AAA 101", "Choir"] and "# shift" not in week.read_text()
    assert act({"action": "week_undo"}) == {"ok": True}
    assert week.read_text() == WEEK
    assert act({"action": "week_undo"})["ok"] is False


def test_a_stale_page_cannot_change_the_wrong_entry(week):
    act = server.calendar_action
    stale = was("Work")
    assert act({"action": "week_delete", "i": 0, "was": was("AAA 101")}) == {"ok": True}
    bad = act({"action": "week_update", "i": 1, "was": stale, "start": "14:00"})
    assert bad["ok"] is False and "changed since" in bad["error"]
    bad = act({"action": "week_update", "i": 0, "was": was("Work"), "start": "18:00"})
    assert bad["ok"] is False and rows()["Work"]["start"] == "13:00"


def test_changing_one_day_of_a_class_skips_it_and_adds_a_one_time_entry(week):
    act = server.calendar_action
    assert act({"action": "week_detach", "i": 0, "was": was("AAA 101"), "on": "2026-10-14",
                "date": "2026-10-15", "start": "14:00", "end": "14:50"}) == {"ok": True}
    assert rows()["AAA 101"]["skip"] == ["2026-10-14"]
    assert [(b["name"], b["date"], b["start"]) for b in calendars.load()["blocks"]] == [
        ("AAA 101", "2026-10-15", "14:00")]
    assert act({"action": "week_undo", "blocks": []}) == {"ok": True}
    assert week.read_text() == WEEK and calendars.load()["blocks"] == []
    bad = act({"action": "week_detach", "i": 0, "was": was("AAA 101"), "on": "2026-10-14",
               "start": "15:00", "end": "14:00"})
    assert bad["ok"] is False and week.read_text() == WEEK


def test_the_calendar_knows_which_weekly_entry_each_block_is(week, cfg):
    from src import config
    v = calendars.view(config.Config(), date(2026, 10, 16), 1)
    assert [(b["title"], b["rec"]) for b in v["days"][0]["blocks"] if b["rec"] is not None] == [
        ("AAA 101", 0), ("Work", 1)]
    assert [r["name"] for r in calendars.meta(config.Config())["week"]] == ["AAA 101", "Work", "Choir"]


def test_this_and_following_for_one_of_your_weekly_entries(cfg):
    b, week_of = weekly(cfg)
    calendars.split_block(b["id"], "2026-10-14", {"start": "18:00", "end": "19:00", "days": ["wed"]})
    assert week_of() == {"2026-10-12": [("Gym", "17:00", "18:00")],
                         "2026-10-14": [("Gym", "18:00", "19:00")]}
    old, new = calendars.load()["blocks"]
    assert old["until"] == "2026-10-13" and new["from"] == "2026-10-14" and new["days"] == ["wed"]
    calendars.split_block(new["id"], "2026-10-14")
    assert [x["id"] for x in calendars.load()["blocks"]] == [b["id"]]
    calendars.split_block(b["id"], "2026-10-12")
    assert week_of() == {} and calendars.load()["blocks"][0]["until"] == "2026-10-11"


def test_this_and_following_for_a_class(week):
    from src import config
    from src.timeline import expand_recurring
    act = server.calendar_action
    assert act({"action": "week_split", "i": 0, "was": was("AAA 101"), "on": "2026-10-14",
                "start": "14:00", "end": "14:50", "days": ["mon", "wed"]}) == {"ok": True}
    assert rows()["AAA 101"]["until"] == "2026-10-13"
    assert [(b["name"], b["days"], b["start"], b["from"]) for b in calendars.load()["blocks"]] == [
        ("AAA 101", ["mon", "wed"], "14:00", "2026-10-14")]
    fresh = config.Config()
    names = lambda d: [b.title for b in expand_recurring(fresh, d)]
    assert names(date(2026, 10, 12)) == ["AAA 101"] and names(date(2026, 10, 14)) == []
    assert act({"action": "week_undo", "blocks": []}) == {"ok": True}
    assert week.read_text() == WEEK and calendars.load()["blocks"] == []
    assert act({"action": "week_split", "i": 1, "was": was("Work"), "on": "2026-10-16"}) == {"ok": True}
    assert rows()["Work"]["until"] == "2026-10-15" and calendars.load()["blocks"] == []
    assert "# shift" in week.read_text()


def meals():
    from src import settings_edit
    return {r["name"]: r for r in settings_edit.meal_rows()}


def meal_was(name):
    r = meals()[name]
    return {"name": r["name"], "start": r["start"], "end": r["end"]}


def test_meals_can_be_moved_skipped_and_removed(week):
    from src import config
    from src.timeline import build_day
    act = server.calendar_action
    assert meals()["Lunch"] == {"i": 1, "name": "Lunch", "start": "12:30", "end": "13:00",
                                "skip": [], "until": ""}
    assert act({"action": "week_update", "section": "meals", "i": 1, "was": meal_was("Lunch"),
                "start": "13:00", "end": "13:45"}) == {"ok": True}
    assert (meals()["Lunch"]["start"], meals()["Lunch"]["end"]) == ("13:00", "13:45")
    assert 'at: "13:00", minutes: 45, flex: 90}   # usually later' in week.read_text()
    assert act({"action": "week_skip", "section": "meals", "i": 1, "was": meal_was("Lunch"),
                "on": "2026-10-13"}) == {"ok": True}
    titles = lambda d: [b.title for b in build_day(config.Config(), d)[0] if b.kind == "meal"]
    assert titles(date(2026, 10, 13)) == ["Breakfast"] and titles(date(2026, 10, 14)) == [
        "Breakfast", "Lunch"]
    assert act({"action": "week_split", "section": "meals", "i": 0, "was": meal_was("Breakfast"),
                "on": "2026-10-14", "start": "08:00", "end": "08:20"}) == {"ok": True}
    assert titles(date(2026, 10, 14)) == ["Lunch"]
    tail = calendars.load()["blocks"][0]
    assert (tail["name"], len(tail["days"]), tail["start"], tail["from"]) == (
        "Breakfast", 7, "08:00", "2026-10-14")
    assert act({"action": "week_delete", "section": "meals", "i": 1,
                "was": meal_was("Lunch")}) == {"ok": True}
    assert list(meals()) == ["Breakfast"] and "one_offs: []" in week.read_text()


def test_meal_blocks_carry_their_position(week):
    from src import config
    v = calendars.view(config.Config(), date(2026, 10, 13), 1)
    assert [(b["title"], b["meal"]) for b in v["days"][0]["blocks"] if b["kind"] == "meal"] == [
        ("Breakfast", 0), ("Lunch", 1)]
    assert [m["name"] for m in calendars.meta(config.Config())["meals"]] == ["Breakfast", "Lunch"]
