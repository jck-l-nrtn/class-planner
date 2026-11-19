from datetime import date

from src.model import Kind, Status
from src.sources import ls_prep
from tests.conftest import at
from tests.test_ics_feed import ev, ics

SPEC = {"label": "Topic Overview", "match": "topic overview", "class_start": "10:00",
        "lead_days": 2, "est_minutes": 90, "weight": 0.075}


def d(day):
    return f";VALUE=DATE:202610{day:02d}"


def topic_events(n, name, first, last, uid):
    return [
        ev(f"{uid}a", f"Topic {n}. {name}", d(first)),
        ev(f"{uid}b", f"Begin this 'Topic Overview' for Topic {n}", d(first)),
        ev(f"{uid}c", f"Topic {n}. ...continued", d(last)),
        ev(f"{uid}d", f"COMPLETE this 'Topic Overview' for Topic {n}", d(last)),
    ]


def feed():
    return ics(*topic_events(1, "Intro to Family", 6, 8, "x"),
               *topic_events(10, "Big Thing", 13, 15, "y"))


def by_n(tasks):
    return {int(t.uid.rsplit("-", 1)[1]): t for t in tasks}


def test_groups_topics_and_dates(cfg):
    tasks = by_n(ls_prep.parse(feed(), "AAA101", cfg, SPEC, today=date(2026, 10, 1)))
    assert set(tasks) == {1, 10}
    t1, t10 = tasks[1], tasks[10]
    assert t1.title == "Topic Overview 1 — Intro to Family"
    assert t1.due_at == at("2026-10-08 10:00")
    assert t1.prep_by == at("2026-10-04 23:59")
    assert t10.due_at == at("2026-10-15 10:00")
    assert t10.prep_by == at("2026-10-11 23:59")
    assert "Oct 6, Oct 8" in t1.notes
    for t in tasks.values():
        assert t.kind == Kind.READING and t.hard_deadline and t.schedulable
        assert t.est_minutes == 90 and t.source == "ls_prep"
        assert t.prep_by <= t.due_at
        assert t.status == Status.NOT_STARTED


def test_weight_split_across_active_topics(cfg):
    tasks = ls_prep.parse(feed(), "AAA101", cfg, SPEC, today=date(2026, 10, 1))
    assert [t.impact_override for t in tasks] == [0.0375, 0.0375]


def test_expired_topic_dropped_and_weight_goes_to_remaining(cfg):
    tasks = by_n(ls_prep.parse(feed(), "AAA101", cfg, SPEC, today=date(2026, 10, 9)))
    t1, t10 = tasks[1], tasks[10]
    assert t1.status == Status.DROPPED and t1.is_done
    assert t1.impact_override == 0.0 and t1.prep_by is None
    assert "closed" in t1.notes
    assert t10.status == Status.NOT_STARTED and t10.impact_override == 0.075


def test_topic_due_today_is_not_expired(cfg):
    tasks = by_n(ls_prep.parse(feed(), "AAA101", cfg, SPEC, today=date(2026, 10, 8)))
    assert tasks[1].status == Status.NOT_STARTED


def test_all_expired_gives_zero_impact(cfg):
    tasks = ls_prep.parse(feed(), "AAA101", cfg, SPEC, today=date(2026, 12, 1))
    assert all(t.status == Status.DROPPED and t.impact_override == 0.0 for t in tasks)


def test_prep_by_never_after_due(cfg):
    spec = dict(SPEC, lead_days=0, class_start="00:30")
    t, = ls_prep.parse(ics(*topic_events(3, "Solo", 6, 6, "z")), "AAA101", cfg, spec,
                       today=date(2026, 10, 1))
    assert t.due_at == at("2026-10-06 00:30")
    assert t.prep_by == t.due_at


def test_topic_without_complete_event_uses_last_class_day(cfg):
    text = ics(ev("a", "Topic 4. Something", d(20)), ev("b", "Topic 4. ...continued", d(22)))
    t, = ls_prep.parse(text, "AAA101", cfg, SPEC, today=date(2026, 10, 1))
    assert t.due_at == at("2026-10-22 10:00")


def test_loose_phrasing_and_no_topics(cfg):
    text = ics(ev("a", "Topic 4. Name", d(20)),
               ev("b", "BEGIN this'Topic Overview' for Topic 4", d(20)),
               ev("c", "Complete this 'Topic Overview' for Topic 4.Watch the video", d(22)))
    t, = ls_prep.parse(text, "AAA101", cfg, SPEC, today=date(2026, 10, 1))
    assert t.due_at.day == 22
    assert ls_prep.parse(ics(ev("a", "HW 1", d(20))), "AAA101", cfg, SPEC,
                         today=date(2026, 10, 1)) == []


def test_default_url(cfg):
    assert ls_prep._default_url({"ls_course_id": "abc"}, cfg).endswith("courseID=abc")
    assert ls_prep._default_url({}, cfg) == ""
