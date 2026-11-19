import math
from datetime import timedelta

import pytest

from src import prioritize, workload
from src.model import Kind, Status, Task
from src.prioritize import (effective_deadline, exam_ramp_urgency, grade_impact,
                            horizon_decay, score_all, urgency)
from tests.conftest import at

NOW = at("2026-10-10 08:00")


def T(uid, due="__none__", days=None, hours=None, **kw):
    if days is not None or hours is not None:
        due = NOW + timedelta(days=days or 0, hours=hours or 0)
    elif due == "__none__":
        due = None
    elif isinstance(due, str):
        due = at(due)
    kw.setdefault("impact_override", 0.05)
    kw.setdefault("est_minutes", 60)
    return Task(uid=uid, source="manual", course_key="AAA101", title=uid,
                due_at=due, **kw)


def by_uid(scored):
    return {s.task.uid: s for s in scored}


def score_of(cfg, task, **kw):
    return score_all(cfg, [task], NOW, **kw)[0]


@pytest.mark.parametrize("sooner,later", [
    (1, 2), (1, 5), (2, 3), (3, 9), (5, 11), (8, 20), (12, 14), (14, 45), (30, 90)])
@pytest.mark.parametrize("est", [30, 600, 3000])
def test_sooner_due_never_scores_lower(cfg, sooner, later, est):
    a = T("a", days=sooner, est_minutes=est)
    b = T("b", days=later, est_minutes=est)
    for order in ([a, b], [b, a]):
        s = by_uid(score_all(cfg, order, NOW))
        assert s["a"].score >= s["b"].score, (sooner, later, est)


def test_sooner_due_close_deadlines_observation(cfg):
    a = T("a", hours=3, est_minutes=120)
    b = T("b", hours=3.5, est_minutes=120)
    s = by_uid(score_all(cfg, [a, b], NOW))
    assert s["a"].score > 0 and s["b"].score > 0


@pytest.mark.parametrize("est,days", [(60, 1), (600, 1), (60, 3), (900, 2), (60, 40)])
def test_higher_impact_never_scores_lower(cfg, est, days):
    lo = T("lo", days=days, est_minutes=est, impact_override=0.02)
    hi = T("hi", days=days, est_minutes=est, impact_override=0.04)
    for order in ([lo, hi], [hi, lo]):
        s = by_uid(score_all(cfg, order, NOW))
        assert s["hi"].score >= s["lo"].score, [t.uid for t in order]


def test_higher_impact_same_due_tight_slack_near_equal_impacts(cfg):
    lo = T("lo", days=1, est_minutes=600, impact_override=0.0500)
    hi = T("hi", days=1, est_minutes=600, impact_override=0.0501)
    for order in ([lo, hi], [hi, lo]):
        s = by_uid(score_all(cfg, order, NOW))
        assert s["hi"].score >= s["lo"].score


@pytest.mark.parametrize("status", [Status.SUBMITTED, Status.GRADED, Status.DROPPED])
def test_done_tasks_not_returned(cfg, status):
    ts = [T("open", days=2), T("done", days=1, status=status)]
    assert [s.task.uid for s in score_all(cfg, ts, NOW)] == ["open"]


def test_done_tasks_do_not_consume_capacity(cfg):
    base = score_of(cfg, T("x", days=1)).slack_hours
    both = by_uid(score_all(cfg, [T("x", days=1),
                                  T("d", days=1, est_minutes=600, status=Status.GRADED)], NOW))
    assert both["x"].slack_hours == pytest.approx(base)


def test_every_scored_task_has_reasons(cfg):
    ts = [T("dated", days=3), T("undated"), T("late", days=-2),
          T("exam", days=3, kind=Kind.EXAM, category="exams"),
          T("hard", days=3, hard_deadline=True, group_work=True,
            status=Status.IN_PROGRESS, category_drops=1, agent_weight=1.5,
            agent_note="because")]
    out = score_all(cfg, ts, NOW)
    assert len(out) == len(ts)
    assert all(s.reasons for s in out)
    r = by_uid(out)
    assert "no due date — backlog only" in r["undated"].reasons
    for frag in ("no late submissions", "group work", "already started"):
        assert frag in r["hard"].reasons
    assert any("agent weight" in x and "because" in x for x in r["hard"].reasons)
    assert any("category drops" in x for x in r["hard"].reasons)
    assert "exam" in r["exam"].reasons


def test_past_due_reason_and_boost(cfg):
    late = score_of(cfg, T("late", days=-1))
    assert "PAST DUE" in late.reasons
    assert any("BEHIND" in x for x in late.reasons)
    future = score_of(cfg, T("f", days=1))
    assert "PAST DUE" not in future.reasons


def test_impact_source_reasons(cfg):
    assert "impact from config/manual_assignments.yaml" in score_of(cfg, T("m")).reasons
    t = T("p"); t.source = "ls_prep"
    assert any("prep weight" in r for r in score_of(cfg, t).reasons)
    t = T("g"); t.source = "learningsuite"
    assert "impact from real gradebook weights" in score_of(cfg, t).reasons


def test_effective_deadline():
    assert effective_deadline(T("a", days=5)) == NOW + timedelta(days=5)
    assert effective_deadline(T("a", days=5, prep_by=NOW + timedelta(days=2))) == NOW + timedelta(days=2)
    assert effective_deadline(T("a", days=2, prep_by=NOW + timedelta(days=5))) == NOW + timedelta(days=2)
    assert effective_deadline(T("a", prep_by=NOW + timedelta(days=1))) == NOW + timedelta(days=1)
    assert effective_deadline(T("a")) is None


def test_prep_by_drives_ranking_and_slack(cfg):
    prep = T("prep", days=20, prep_by=NOW + timedelta(days=2), est_minutes=300)
    twin = T("twin", days=2, est_minutes=300)
    plain = T("plain", days=20, est_minutes=300)
    sp, st, sl = (score_of(cfg, x) for x in (prep, twin, plain))
    assert sp.slack_hours == pytest.approx(st.slack_hours)
    assert sp.slack_hours < sl.slack_hours
    assert sp.score > sl.score
    assert any("prep target" in r for r in sp.reasons)


def test_missing_prep_target_is_not_past_due(cfg):
    t = T("p", days=3, prep_by=NOW - timedelta(days=1))
    s = score_of(cfg, t)
    assert "PAST DUE" not in s.reasons
    assert s.slack_hours < 0


def test_prep_by_used_in_horizon_decay(cfg):
    far = T("far", days=60)
    prepped = T("pp", days=60, prep_by=NOW + timedelta(days=1))
    assert horizon_decay(cfg, prepped, NOW) > horizon_decay(cfg, far, NOW)


@pytest.mark.parametrize("days_out", [0.1, 1, 2.5, 4.9])
def test_exam_in_ramp_window_urgency_at_least_030(cfg, days_out):
    exam = T("e", days=days_out, kind=Kind.EXAM, category="exams", est_minutes=10)
    s = score_of(cfg, exam)
    assert s.urgency >= 0.30


def test_exam_ramp_reason_and_monotone(cfg):
    ugs = [exam_ramp_urgency(cfg, T("e", days=d, kind=Kind.EXAM), NOW)
           for d in (4.9, 4, 3, 2, 1, 0.1)]
    assert ugs == sorted(ugs) and ugs[0] >= 0.30 and ugs[-1] <= 1.0
    s = score_of(cfg, T("e", days=4.9, kind=Kind.EXAM, category="exams", est_minutes=10))
    assert any("exam prep window open" in r for r in s.reasons)


def test_exam_ramp_outside_window_or_not_exam(cfg):
    assert exam_ramp_urgency(cfg, T("e", days=6, kind=Kind.EXAM), NOW) == 0.0
    assert exam_ramp_urgency(cfg, T("e", days=-1, kind=Kind.EXAM), NOW) == 0.0
    assert exam_ramp_urgency(cfg, T("e", days=2), NOW) == 0.0
    assert exam_ramp_urgency(cfg, T("e", kind=Kind.EXAM), NOW) == 0.0


def test_exam_ramp_uses_course_setting(cfg):
    t = T("e", days=6, kind=Kind.EXAM)
    t.course_key = "BBB202"
    assert exam_ramp_urgency(cfg, t, NOW) >= 0.30


def test_undated_returned_but_not_first(cfg):
    ts = [T("undated", impact_override=0.3),
          T("urgent", days=1, est_minutes=300, impact_override=0.05),
          T("later", days=5)]
    out = score_all(cfg, ts, NOW)
    assert {s.task.uid for s in out} == {"undated", "urgent", "later"}
    assert out[0].task.uid != "undated"
    u = by_uid(out)["undated"]
    assert math.isinf(u.slack_hours)
    assert u.score > 0


def test_undated_alone_is_returned(cfg):
    out = score_all(cfg, [T("u")], NOW)
    assert len(out) == 1 and out[0].task.uid == "u"


def test_unschedulable_zero_minutes_does_not_reduce_slack(cfg):
    target = T("t", days=2, est_minutes=120)
    alone = score_of(cfg, target).slack_hours
    att = [T(f"att{i}", days=1, est_minutes=0, schedulable=False) for i in range(5)]
    out = by_uid(score_all(cfg, att + [target], NOW))
    assert out["t"].slack_hours == pytest.approx(alone)


def test_unschedulable_with_default_minutes_does_not_reduce_slack(cfg):
    target = T("t", days=2, est_minutes=120)
    alone = score_of(cfg, target).slack_hours
    att = T("att", days=1, schedulable=False)
    out = by_uid(score_all(cfg, [att, target], NOW))
    assert out["t"].slack_hours == pytest.approx(alone)


def test_higher_priority_work_reduces_slack_of_later_task(cfg):
    alone = score_of(cfg, T("b", days=2, est_minutes=120)).slack_hours
    out = by_uid(score_all(cfg, [T("a", days=1, est_minutes=120),
                                 T("b", days=2, est_minutes=120)], NOW))
    assert out["b"].slack_hours == pytest.approx(alone - 2.0)
    assert out["a"].slack_hours == pytest.approx(
        score_of(cfg, T("a", days=1, est_minutes=120)).slack_hours)


def test_calibration_scales_need(cfg):
    base = score_of(cfg, T("x", days=2, est_minutes=120)).slack_hours
    cal = score_of(cfg, T("x", days=2, est_minutes=120),
                   calibration={"AAA101:homework": 2.0}).slack_hours
    assert cal == pytest.approx(base - 2.0)


def test_spent_minutes_reduce_need(cfg):
    base = score_of(cfg, T("x", days=2, est_minutes=120)).slack_hours
    part = score_of(cfg, T("x", days=2, est_minutes=120, spent_minutes=60)).slack_hours
    assert part == pytest.approx(base + 1.0)


def test_sorted_descending(cfg):
    ts = [T(f"t{i}", days=i, impact_override=0.01 * i) for i in range(1, 8)] + [T("u")]
    out = score_all(cfg, ts, NOW)
    assert [s.score for s in out] == sorted((s.score for s in out), reverse=True)


def test_multipliers_apply(cfg):
    base = score_of(cfg, T("x", days=1, est_minutes=600)).score
    assert score_of(cfg, T("x", days=1, est_minutes=600, hard_deadline=True)).score == pytest.approx(base * 1.3)
    assert score_of(cfg, T("x", days=1, est_minutes=600, group_work=True)).score == pytest.approx(base * 1.2)
    assert score_of(cfg, T("x", days=1, est_minutes=600, category_drops=1)).score == pytest.approx(base * 0.85)
    assert score_of(cfg, T("x", days=1, est_minutes=600, agent_weight=2.0)).score == pytest.approx(base * 2)


def test_grade_impact_override_wins(cfg):
    t = T("x", impact_override=0.123)
    assert grade_impact(cfg, t, {}, {}) == 0.123
    assert grade_impact(cfg, T("x", impact_override=0.0), {}, {}) == 0.0


def test_grade_impact_points_share(cfg):
    a = T("a", impact_override=None, points=10); a.category = "homework"
    b = T("b", impact_override=None, points=30); b.category = "homework"
    pts = prioritize._category_points([a, b])
    cnt = prioritize._category_counts([a, b])
    assert grade_impact(cfg, a, pts, cnt) == pytest.approx(0.30 * 0.25)
    assert grade_impact(cfg, b, pts, cnt) == pytest.approx(0.30 * 0.75)


def test_grade_impact_equal_split_without_points(cfg):
    ts = [T(f"h{i}", impact_override=None) for i in range(3)]
    pts, cnt = prioritize._category_points(ts), prioritize._category_counts(ts)
    assert grade_impact(cfg, ts[0], pts, cnt) == pytest.approx(0.30 / 3)


def test_urgency_bounds_and_shape(cfg):
    assert urgency(cfg, -50) == 1.0
    assert urgency(cfg, 0) == 1.0
    assert urgency(cfg, 40) == 0.05
    assert urgency(cfg, 1e9) == 0.05
    assert urgency(cfg, math.inf) == 0.05
    assert urgency(cfg, 20) == pytest.approx(1 - 0.5 * 0.95)
    vals = [urgency(cfg, h) for h in range(-10, 60)]
    assert all(0.05 <= v <= 1.0 for v in vals)
    assert vals == sorted(vals, reverse=True)


def test_horizon_decay_shape(cfg):
    assert horizon_decay(cfg, T("u"), NOW) == 0.15
    assert horizon_decay(cfg, T("a", days=5), NOW) == 1.0
    assert horizon_decay(cfg, T("a", days=10), NOW) == 1.0
    assert horizon_decay(cfg, T("a", days=24), NOW) == pytest.approx(0.5)
    assert horizon_decay(cfg, T("a", days=-3), NOW) == 1.0


def test_horizon_decay_monotone_non_increasing(cfg):
    vals = [horizon_decay(cfg, T("a", days=d), NOW) for d in range(-5, 200, 3)]
    assert vals == sorted(vals, reverse=True)
    assert all(0 < v <= 1 for v in vals)


def test_workload_factor_for_precedence():
    merged = {"A:homework": 1.5, "__course__A": 0.8}
    assert workload.factor_for(merged, "A", "homework") == 1.5
    assert workload.factor_for(merged, "A", "exams") == 0.8
    assert workload.factor_for(merged, "B", "homework") == 1.0
    assert workload.factor_for({}, "A", "homework") == 1.0


def test_workload_apply_logged_wins_over_benchmark():
    logged = {"A:homework": 1.4}
    merged = workload.apply({"A": 0.7, "B": 1.2}, logged)
    assert "__course__A" not in merged
    assert merged["__course__B"] == 1.2
    assert merged["A:homework"] == 1.4
    assert logged == {"A:homework": 1.4}
    assert workload.factor_for(merged, "A", "exams") == 1.0
    assert workload.factor_for(merged, "B", "exams") == 1.2


def test_workload_apply_empty():
    assert workload.apply({}, {}) == {}
