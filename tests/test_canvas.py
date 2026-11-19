import pytest

from src.model import Kind, Status
from src.sources import canvas
from src.sources.canvas import _parse_dt, _status, classify, compute_impacts
from tests.conftest import DENVER, at


@pytest.mark.parametrize("name,group,subs,expected", [
    ("Chapter 10 HW", "Homework Assignments", [], Kind.HOMEWORK),
    ("Final Exam", "Quizzes", [], Kind.QUIZ),
    ("Midterm 1", "Other", [], Kind.EXAM),
    ("Midterm 1", "", [], Kind.EXAM),
    ("Lab 4", "Assignments", [], Kind.PROJECT),
    ("Reading Quizzes", "", [], Kind.QUIZ),
    ("Reading Quiz 4", "Reading", [], Kind.READING),
    ("Final Project", "", [], Kind.PROJECT),
    ("Final Project", "Misc", [], Kind.PROJECT),
    ("Chapter 3", "", ["online_quiz"], Kind.QUIZ),
    ("Chapter 3", "", [], Kind.READING),
    ("Problem Set 2", "", [], Kind.HOMEWORK),
    ("Something odd", "", [], Kind.HOMEWORK),
    ("Chapter 3", "Participation", [], Kind.READING),
])
def test_classify(name, group, subs, expected):
    assert classify(name, group, subs) == expected


@pytest.mark.parametrize("name,group,expected", [
    ("Homework 3", "Exams", Kind.EXAM),
    ("Homework 3", "Projects", Kind.PROJECT),
    ("Chapter 1", "Tests", Kind.EXAM),
])
def test_classify_plural_group_names(name, group, expected):
    assert classify(name, group, []) == expected


def test_classify_none_group_and_subs():
    assert classify("Quiz 1", None, None) == Kind.QUIZ


def A(id, pts, gid=None, **kw):
    return {"id": id, "points_possible": pts, "assignment_group_id": gid, **kw}


def test_weighted_groups_mode():
    course = {"apply_assignment_group_weights": True}
    groups = [{"id": 1, "group_weight": 30}, {"id": 2, "group_weight": 70}]
    asg = [A(1, 10, 1), A(2, 30, 1), A(3, 100, 2)]
    imp = compute_impacts(course, groups, asg)
    assert imp[1] == pytest.approx(0.075)
    assert imp[2] == pytest.approx(0.225)
    assert imp[3] == pytest.approx(0.70)
    assert sum(imp.values()) == pytest.approx(1.0)


def test_weighted_groups_normalizes_weights_not_summing_to_100():
    course = {"apply_assignment_group_weights": True}
    groups = [{"id": 1, "group_weight": 40}, {"id": 2, "group_weight": 40}]
    imp = compute_impacts(course, groups, [A(1, 5, 1), A(2, 5, 2)])
    assert imp == {1: pytest.approx(0.5), 2: pytest.approx(0.5)}


def test_weighted_groups_omitted_and_unpublished_excluded():
    course = {"apply_assignment_group_weights": True}
    groups = [{"id": 1, "group_weight": 100}]
    asg = [A(1, 10, 1), A(2, 10, 1, omit_from_final_grade=True),
           A(3, 10, 1, published=False)]
    imp = compute_impacts(course, groups, asg)
    assert imp[1] == pytest.approx(1.0)
    assert imp[2] == 0.0 and imp[3] == 0.0


def test_weighted_group_with_zero_points_is_zero_not_crash():
    course = {"apply_assignment_group_weights": True}
    imp = compute_impacts(course, [{"id": 1, "group_weight": 100}], [A(1, 0, 1)])
    assert imp[1] == 0.0


def test_straight_points_mode_ignores_groups():
    course = {"apply_assignment_group_weights": False}
    groups = [{"id": 1, "group_weight": 99}]
    asg = [A(1, 350, 1), A(2, 650, 1), A(3, 0, 1)]
    imp = compute_impacts(course, groups, asg)
    assert imp[1] == pytest.approx(0.35) and imp[2] == pytest.approx(0.65)
    assert imp[3] == 0.0


def test_straight_points_omit_unpublished_not_in_denominator():
    imp = compute_impacts({}, [], [A(1, 50), A(2, 50, omit_from_final_grade=True),
                                   A(3, 900, published=False), A(4, 50)])
    assert imp[1] == pytest.approx(0.5) and imp[4] == pytest.approx(0.5)
    assert imp[2] == 0.0 and imp[3] == 0.0


def test_straight_points_all_zero():
    assert compute_impacts({}, [], [A(1, 0)]) == {1: 0.0}


def test_status():
    assert _status({}) == Status.NOT_STARTED
    assert _status({"submission": None}) == Status.NOT_STARTED
    assert _status({"submission": {"workflow_state": "unsubmitted"}}) == Status.NOT_STARTED
    assert _status({"submission": {"submitted_at": "2026-10-01T00:00:00Z"}}) == Status.SUBMITTED
    assert _status({"submission": {"workflow_state": "pending_review"}}) == Status.SUBMITTED
    assert _status({"submission": {"workflow_state": "graded", "score": 5}}) == Status.GRADED
    assert _status({"submission": {"workflow_state": "graded", "score": None}}) == Status.NOT_STARTED
    assert _status({"submission": {"workflow_state": "graded", "score": 0}}) == Status.GRADED


def test_status_submitted_at_wins_over_graded():
    s = _status({"submission": {"workflow_state": "graded", "score": 9,
                                "submitted_at": "2026-10-01T00:00:00Z"}})
    assert s == Status.SUBMITTED


def test_parse_dt():
    assert _parse_dt(None, DENVER) is None
    assert _parse_dt("", DENVER) is None
    dt = _parse_dt("2026-10-05T05:59:00Z", DENVER)
    assert dt == at("2026-10-04 23:59") and dt.tzinfo is not None
    assert _parse_dt("2026-11-02T06:59:00Z", DENVER) == at("2026-11-01 23:59")


class FakeClient:
    def __init__(self, base, token): pass

    def courses(self):
        return [{"id": 111, "name": "Alpha", "course_code": "AAA",
                 "apply_assignment_group_weights": True},
                {"id": 999, "name": "Unmapped", "course_code": "ZZZ"}]

    def assignment_groups(self, cid):
        assert cid == 111
        return [{"id": 1, "name": "Homework", "group_weight": 40},
                {"id": 2, "name": "Exams", "group_weight": 60}]

    def assignments(self, cid):
        return [
            {"id": 10, "name": "HW 1", "assignment_group_id": 1, "points_possible": 10,
             "due_at": "2026-10-06T05:59:00Z", "lock_at": "2026-10-06T05:59:00Z",
             "unlock_at": "2026-09-20T06:00:00Z", "html_url": "http://c/10",
             "submission": {"workflow_state": "graded", "score": 9}},
            {"id": 11, "name": "HW 2", "assignment_group_id": 1, "points_possible": 30,
             "due_at": "2026-10-13T05:59:00Z", "lock_at": "2026-10-20T05:59:00Z",
             "group_category_id": 5, "submission": None},
            {"id": 12, "name": "Midterm", "assignment_group_id": 2, "points_possible": 100,
             "due_at": None},
        ]


def test_fetch_with_fake_client(cfg, monkeypatch):
    monkeypatch.setattr(canvas, "CanvasClient", FakeClient)
    tasks, seen = canvas.fetch(cfg)
    assert seen == [{"canvas_id": 111, "name": "Alpha", "code": "AAA", "mapped_to": "AAA101"},
                    {"canvas_id": 999, "name": "Unmapped", "code": "ZZZ", "mapped_to": None}]
    by = {t.uid: t for t in tasks}
    assert set(by) == {"canvas:10", "canvas:11", "canvas:12"}
    t10, t11, t12 = by["canvas:10"], by["canvas:11"], by["canvas:12"]
    assert t10.course_key == "AAA101" and t10.kind == Kind.HOMEWORK
    assert t10.due_at == at("2026-10-05 23:59")
    assert t10.available_from == at("2026-09-20 00:00")
    assert t10.hard_deadline is True and t11.hard_deadline is False
    assert t11.group_work is True and t10.group_work is False
    assert t10.status == Status.GRADED and t10.score == 9
    assert t11.status == Status.NOT_STARTED and t11.score is None
    assert t10.category == "Homework" and t10.est_minutes == 60
    assert t12.kind == Kind.EXAM and t12.due_at is None and t12.est_minutes == 240
    assert t10.impact_override == pytest.approx(0.4 * 10 / 40)
    assert t11.impact_override == pytest.approx(0.4 * 30 / 40)
    assert t12.impact_override == pytest.approx(0.6)
    assert t10.url == "http://c/10"
