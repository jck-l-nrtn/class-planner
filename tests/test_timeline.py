from datetime import date, timedelta

from src import timeline
from src.model import Block, Interval
from tests.conftest import at

MON = date(2026, 10, 5)
TUE = date(2026, 10, 6)
FRI = date(2026, 10, 9)
SAT = date(2026, 10, 10)


def blk(s, e, kind="standing", loc="", movable=False, title="x"):
    return Block(start=at(s), end=at(e), title=title, kind=kind, location=loc,
                 movable=movable)


def span(items):
    return [(i.start.strftime("%H:%M"), i.end.strftime("%H:%M")) for i in items]


def test_expand_recurring(cfg):
    assert [b.title for b in timeline.expand_recurring(cfg, MON)] == ["AAA 101"]
    assert [b.title for b in timeline.expand_recurring(cfg, FRI)] == ["AAA 101", "Work"]
    assert timeline.expand_recurring(cfg, SAT) == []
    b, = timeline.expand_recurring(cfg, MON)
    assert (b.start, b.end, b.kind, b.course_key, b.location) == (
        at("2026-10-05 09:00"), at("2026-10-05 09:50"), "class", "AAA101", "campus")


def test_expand_recurring_skip_dates_and_example(cfg):
    cfg.commitments["recurring"][0]["skip_dates"] = [date(2026, 10, 5)]
    cfg.commitments["recurring"].append(
        {"name": "EXAMPLE thing", "days": ["mon"], "start": "08:00", "end": "09:00"})
    assert timeline.expand_recurring(cfg, MON) == []


def test_expand_one_offs(cfg):
    cfg.commitments["one_offs"] = [{"name": "Dentist", "date": date(2026, 10, 10),
                                    "start": "10:00", "end": "11:00"}]
    b, = timeline.expand_one_offs(cfg, SAT)
    assert b.title == "Dentist" and b.minutes == 60
    assert timeline.expand_one_offs(cfg, MON) == []


def test_free_intervals_empty_day(cfg):
    free = timeline.free_intervals(cfg, SAT, [])
    assert span(free) == [("07:00", "22:15")]
    assert free[0].minutes == 915


def test_free_intervals_splits_and_clips(cfg):
    busy = [blk("2026-10-10 06:00", "2026-10-10 08:00"),
            blk("2026-10-10 12:00", "2026-10-10 13:00"),
            blk("2026-10-10 12:30", "2026-10-10 14:00"),
            blk("2026-10-10 22:00", "2026-10-10 23:30")]
    assert span(timeline.free_intervals(cfg, SAT, busy)) == [
        ("08:00", "12:00"), ("14:00", "22:00")]


def test_free_intervals_fully_busy_and_adjacent(cfg):
    assert timeline.free_intervals(cfg, SAT, [blk("2026-10-10 06:00", "2026-10-10 23:30")]) == []
    adj = [blk("2026-10-10 09:00", "2026-10-10 10:00"),
           blk("2026-10-10 10:00", "2026-10-10 11:00")]
    assert span(timeline.free_intervals(cfg, SAT, adj)) == [
        ("07:00", "09:00"), ("11:00", "22:15")]


def test_free_intervals_ignores_other_day_blocks(cfg):
    other = [blk("2026-10-11 12:00", "2026-10-11 13:00")]
    assert span(timeline.free_intervals(cfg, SAT, other)) == [("07:00", "22:15")]


def test_travel_to_campus_and_home(cfg):
    fixed = timeline.expand_recurring(cfg, MON)
    tr = timeline.insert_travel(cfg, fixed, MON)
    assert [(t.title, t.start.strftime("%H:%M"), t.end.strftime("%H:%M")) for t in tr] == [
        ("Travel → campus", "08:40", "09:00"),
        ("Travel → home", "09:50", "10:05"),
    ]
    assert all(t.kind == "travel" for t in tr)


def test_travel_between_locations(cfg):
    fixed = timeline.expand_recurring(cfg, FRI)
    tr = timeline.insert_travel(cfg, fixed, FRI)
    assert [(t.title, t.start.strftime("%H:%M"), t.end.strftime("%H:%M")) for t in tr] == [
        ("Travel → campus", "08:40", "09:00"),
        ("Travel → work", "12:35", "13:00"),
        ("Travel → home", "17:00", "17:20"),
    ]


def test_travel_skipped_when_it_does_not_fit(cfg):
    early = [blk("2026-10-10 07:05", "2026-10-10 08:00", loc="campus")]
    tr = timeline.insert_travel(cfg, early, SAT)
    assert [t.title for t in tr] == ["Travel → home"]


def test_travel_none_for_empty_or_home_only(cfg):
    assert timeline.insert_travel(cfg, [], SAT) == []
    assert timeline.insert_travel(cfg, [blk("2026-10-10 10:00", "2026-10-10 11:00")], SAT) == []


def test_meals_at_preferred_times_on_empty_day(cfg):
    meals = timeline.insert_meals(cfg, [], SAT)
    assert [(m.title, m.start.strftime("%H:%M"), m.minutes) for m in meals] == [
        ("Breakfast", "07:30", 25), ("Lunch", "12:30", 30), ("Dinner", "18:15", 45)]
    assert all(m.kind == "meal" for m in meals)


def test_meal_flex_shifts_to_nearest_free_slot(cfg):
    busy = [blk("2026-10-10 11:45", "2026-10-10 13:00")]
    meals = {m.title: m for m in timeline.insert_meals(cfg, busy, SAT)}
    assert meals["Lunch"].start == at("2026-10-10 13:00")


def test_meal_flex_steps_are_15_minutes(cfg):
    busy = [blk("2026-10-10 12:30", "2026-10-10 12:45")]
    lunch = [m for m in timeline.insert_meals(cfg, busy, SAT) if m.title == "Lunch"][0]
    assert (lunch.start - at("2026-10-10 12:30")).total_seconds() % 900 == 0
    assert abs((lunch.start - at("2026-10-10 12:30")).total_seconds()) == 900


def test_meals_do_not_collide_with_each_other(cfg):
    cfg.commitments["meals"] = [
        {"name": "a", "at": "12:00", "minutes": 30, "flex": 60},
        {"name": "b", "at": "12:00", "minutes": 30, "flex": 60}]
    a, b = timeline.insert_meals(cfg, [], SAT)
    assert not a.overlaps(b)


def test_unplaced_meals_reported(cfg):
    unplaced = []
    busy = [blk("2026-10-10 05:00", "2026-10-10 23:59")]
    meals = timeline.insert_meals(cfg, busy, SAT, unplaced)
    assert meals == [] and unplaced == ["breakfast", "lunch", "dinner"]


def test_unplaced_only_lists_failures(cfg):
    unplaced = []
    busy = [blk("2026-10-10 10:30", "2026-10-10 14:30")]
    meals = timeline.insert_meals(cfg, busy, SAT, unplaced)
    assert unplaced == ["lunch"]
    assert [m.title for m in meals] == ["Breakfast", "Dinner"]


def test_meals_without_unplaced_list_do_not_raise(cfg):
    busy = [blk("2026-10-10 05:00", "2026-10-10 23:59")]
    assert timeline.insert_meals(cfg, busy, SAT) == []


def test_zero_flex_means_exact_time_only(cfg):
    cfg.commitments["meals"] = [{"name": "lunch", "at": "12:30", "minutes": 30}]
    unplaced = []
    busy = [blk("2026-10-10 12:40", "2026-10-10 13:00")]
    assert timeline.insert_meals(cfg, busy, SAT, unplaced) == []
    assert unplaced == ["lunch"]


def test_build_day_monday(cfg):
    fixed, free = timeline.build_day(cfg, MON)
    kinds = [b.kind for b in fixed]
    assert kinds.count("class") == 1 and kinds.count("travel") == 2
    assert [b.title for b in fixed if b.kind == "meal"] == ["Breakfast", "Lunch", "Dinner"]
    assert fixed == sorted(fixed, key=lambda b: b.start)
    for g in free:
        assert not any(g.start < b.end and b.start < g.end for b in fixed)
    blocked = sum(b.minutes for b in fixed)
    assert sum(g.minutes for g in free) + blocked == 915


def test_build_day_reports_unplaced(cfg):
    out = []
    ext = [blk("2026-10-10 05:00", "2026-10-10 23:59")]
    timeline.build_day(cfg, SAT, ext, out)
    assert out == ["breakfast", "lunch", "dinner"]


def test_capacity_single_day(cfg):
    assert timeline.capacity_minutes(cfg, at("2026-10-10 00:00"), at("2026-10-10 23:59")) == 815


def test_capacity_partial_day_clips(cfg):
    assert timeline.capacity_minutes(cfg, at("2026-10-10 08:00"), at("2026-10-10 09:00")) == 60


def test_capacity_across_days(cfg):
    got = timeline.capacity_minutes(cfg, at("2026-10-10 12:00"), at("2026-10-11 12:00"))
    assert got == 815


def test_capacity_multi_day_equals_sum_of_days(cfg):
    start, until = at("2026-10-05 00:00"), at("2026-10-08 23:59")
    total = sum(timeline.capacity_minutes(cfg, at(f"2026-10-0{d} 00:00"),
                                          at(f"2026-10-0{d} 23:59")) for d in (5, 6, 7, 8))
    assert timeline.capacity_minutes(cfg, start, until) == total


def test_capacity_until_not_after_start_is_zero(cfg):
    t = at("2026-10-10 10:00")
    assert timeline.capacity_minutes(cfg, t, t) == 0
    assert timeline.capacity_minutes(cfg, t, t - timedelta(days=3)) == 0


def test_capacity_respects_external_busy(cfg):
    base = timeline.capacity_minutes(cfg, at("2026-10-10 00:00"), at("2026-10-10 23:59"))
    ext = {SAT: [blk("2026-10-10 14:00", "2026-10-10 16:00")]}
    assert timeline.capacity_minutes(cfg, at("2026-10-10 00:00"), at("2026-10-10 23:59"), ext) == base - 120


def test_capacity_ends_before_wake_is_zero(cfg):
    assert timeline.capacity_minutes(cfg, at("2026-10-10 01:00"), at("2026-10-10 06:00")) == 0


def test_dst_fall_back_day_window(cfg):
    day = date(2026, 11, 1)
    free = timeline.free_intervals(cfg, day, [])
    assert span(free) == [("07:00", "22:15")]
    assert free[0].start.utcoffset() == timedelta(hours=-7)
    assert timeline.capacity_minutes(cfg, at("2026-11-01 00:00"), at("2026-11-01 23:59")) == 815


def test_dst_spring_forward_day_window(cfg):
    day = date(2026, 3, 8)
    free = timeline.free_intervals(cfg, day, [])
    assert span(free) == [("07:00", "22:15")]
    assert free[0].start.utcoffset() == timedelta(hours=-6)
    assert timeline.capacity_minutes(cfg, at("2026-03-08 00:00"), at("2026-03-08 23:59")) == 815


def test_dst_capacity_across_fall_back_midnight(cfg):
    assert timeline.capacity_minutes(cfg, at("2026-10-31 22:00"), at("2026-11-01 08:00")) == 50


def test_dst_interval_minutes_are_wall_clock():
    i = Interval(at("2026-11-01 00:00"), at("2026-11-01 12:00"))
    assert i.minutes == 720
