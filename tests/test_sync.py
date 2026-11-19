"""src/sync.py: merge() semantics, signature matching, placeholder supersede."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.model import Status
from src.store import Store
from src.sync import SourceResult, merge, match_placeholders, same_work, signature
from tests.helpers import mk


@pytest.fixture
def path(tmp_path):
    return tmp_path / "store.json"


def canvas_ok(*tasks, course="AAA101"):
    return SourceResult("canvas", True, list(tasks), {("canvas", course)}, "ok")


def canvas_failed():
    return SourceResult("canvas", False, message="ConnectionError: down")


def gb_result(*tasks, ok=True, course="AAA101"):
    return SourceResult("learningsuite", ok, list(tasks) if ok else [],
                        {("learningsuite", course)} if ok else set(), "gb")


def manual_result(*tasks, course="AAA101"):
    return SourceResult("manual", True, list(tasks), {("manual", course)}, "m")


def events(path):
    p = path.parent / "events.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_failed_source_leaves_tasks_untouched_not_missing(path):
    st = Store(path)
    merge(st, [canvas_ok(mk(), mk("canvas:2", title="HW 2"))])
    s = merge(st, [canvas_failed()])
    assert set(st.tasks) == {"canvas:1", "canvas:2"}
    assert all(t.missing_since is None for t in st.tasks.values())
    assert s["missing"] == [] and s["newly_missing"] == []


def test_ok_source_omitting_task_marks_it_missing(path):
    st = Store(path)
    merge(st, [canvas_ok(mk(), mk("canvas:2", title="HW 2"))])
    s = merge(st, [canvas_ok(mk())])
    assert st.tasks["canvas:2"].missing_since
    assert s["missing"] == ["canvas:2"]


def test_ok_source_for_other_course_does_not_mark_missing(path):
    st = Store(path)
    merge(st, [canvas_ok(mk())])
    merge(st, [canvas_ok(mk("canvas:9", course="BBB202"), course="BBB202")])
    assert st.tasks["canvas:1"].missing_since is None


def test_source_health_recorded_and_last_ok_kept_on_failure(path):
    st = Store(path)
    merge(st, [canvas_ok(mk())])
    first = st.sources["canvas"]
    assert first["ok"] is True and first["last_ok"] == first["at"]
    st.sources["canvas"]["last_ok"] = "2026-01-01T00:00:00+00:00"
    merge(st, [canvas_failed()])
    s = st.sources["canvas"]
    assert s["ok"] is False and s["message"].startswith("ConnectionError")
    assert s["last_ok"] == "2026-01-01T00:00:00+00:00"


def test_failed_source_never_recorded_ok_when_never_ok(path):
    st = Store(path)
    merge(st, [canvas_failed()])
    assert st.sources["canvas"]["last_ok"] is None


def test_merge_survives_empty_results(path):
    st = Store(path)
    assert merge(st, [])["added"] == 0


def test_merge_persists_through_edit(path):
    with Store.edit("sync", path) as st:
        merge(st, [canvas_ok(mk())])
    assert "canvas:1" in Store(path).tasks and "canvas" in Store(path).sources


def test_signature_pow_variants():
    a = signature("POW Quiz #9: Capital Budgeting Analysis")
    b = signature("POW 9 Acme Holdings")
    assert a == b == ("pow", "9")


@pytest.mark.parametrize("title,sig", [
    ("Ch 8 Reading Quiz", ("ch", "8")), ("Chapter 12 Quiz", ("ch", "12")),
    ("Midterm 2", ("midterm", "2")), ("Midterm Exam #1", ("midterm", "1")),
    ("Final Exam", ("final", "")), ("Random homework", None),
    ("POW", None)])
def test_signature_families(title, sig):
    assert signature(title) == sig


def test_signature_does_not_match_inside_words():
    assert signature("Finalize outline 3") is None
    assert signature("Powerpoint 4") is None


def test_ch_and_chapter_match():
    a = mk("m:1", "manual", title="Ch 8 Reading Quiz", due=None)
    b = mk("ls:1", "learningsuite", title="Chapter 8 Reading Quiz", due=None)
    assert same_work(a, b)


def test_ch8_does_not_match_ch9():
    a = mk("m:1", "manual", title="Ch 8", due="2026-10-10 23:59")
    b = mk("ls:1", "learningsuite", title="Ch 9", due="2026-10-10 23:59")
    assert not same_work(a, b)


def test_same_signature_far_due_dates_do_not_match():
    a = mk("m:1", "manual", title="POW 9 thing", due="2026-10-10 23:59")
    b = mk("ls:1", "learningsuite", title="POW Quiz #9: other", due="2026-10-15 23:59")
    assert not same_work(a, b)
    c = mk("ls:2", "learningsuite", title="POW Quiz #9: other", due="2026-10-11 12:00")
    assert same_work(a, c)


def test_signature_match_needs_due_dates():
    a = mk("m:1", "manual", title="POW 9 thing", due=None)
    b = mk("ls:1", "learningsuite", title="POW Quiz #9: other", due="2026-10-10 23:59")
    assert not same_work(a, b)


def test_practice_never_matched_by_signature():
    a = mk("m:1", "manual", title="POW 9 thing")
    b = mk("ls:1", "learningsuite", title="POW 9 Practice")
    assert not same_work(a, b)


def test_different_courses_never_match():
    a = mk("m:1", "manual", title="Ch 8 Quiz", course="AAA101")
    b = mk("ls:1", "learningsuite", title="Ch 8 Quiz", course="BBB202")
    assert not same_work(a, b)


def test_match_placeholders_picks_highest_impact():
    m = mk("m:1", "manual", title="Midterm 1")
    lo = mk("ls:1", "learningsuite", title="Midterm 1 (retake)", impact_override=0.01)
    hi = mk("ls:2", "learningsuite", title="Midterm 1 exam", impact_override=0.2)
    assert match_placeholders([m], [lo, hi]) == {"m:1": "ls:2"}
    assert match_placeholders([m], []) == {}


def setup_placeholder(path):
    st = Store(path)
    merge(st, [manual_result(mk("manual:mid", "manual", title="Midterm 1",
                                due="2026-10-20 18:00"))])
    st.set_status("manual:mid", "in_progress")
    st.log_time("manual:mid", 40)
    return st


def real_midterm():
    return mk("ls:mid", "learningsuite", title="Midterm 1 Exam", due="2026-10-20 23:59",
              impact_override=0.2, weight_source="gradebook")


def test_placeholder_not_superseded_when_gradebook_failed(path):
    st = setup_placeholder(path)
    s = merge(st, [gb_result(ok=False), manual_result(
        mk("manual:mid", "manual", title="Midterm 1", due="2026-10-20 18:00"))])
    assert "manual:mid" in st.tasks and not st.aliases
    assert s["placeholders_matched"] == 0 and s["superseded"] == []


def test_placeholder_not_superseded_when_gradebook_absent(path):
    st = setup_placeholder(path)
    merge(st, [manual_result(mk("manual:mid", "manual", title="Midterm 1",
                                due="2026-10-20 18:00"))])
    assert "manual:mid" in st.tasks


def test_placeholder_superseded_when_matched(path):
    st = setup_placeholder(path)
    s = merge(st, [gb_result(real_midterm()),
                   manual_result(mk("manual:mid", "manual", title="Midterm 1",
                                    due="2026-10-20 18:00"))])
    assert "manual:mid" not in st.tasks and "ls:mid" in st.tasks
    real = st.tasks["ls:mid"]
    assert real.status == Status.IN_PROGRESS
    assert real.spent_minutes == 40
    assert st.aliases["manual:mid"] == "ls:mid" and st.resolve("manual:mid") == "ls:mid"
    assert s["superseded"] == [("Midterm 1", "Midterm 1 Exam")]
    assert s["placeholders_matched"] == 1
    assert "manual:mid" not in s["missing"]
    merge(st, [gb_result(real_midterm()),
               manual_result(mk("manual:mid", "manual", title="Midterm 1",
                                due="2026-10-20 18:00"))])
    assert "manual:mid" not in st.tasks
    assert st.tasks["ls:mid"].spent_minutes == 40


def test_superseded_placeholder_not_reported_missing(path):
    st = setup_placeholder(path)
    s = merge(st, [gb_result(real_midterm()),
                   manual_result(mk("manual:mid", "manual", title="Midterm 1",
                                    due="2026-10-20 18:00"))])
    st.save()
    assert "manual:mid" not in s["newly_missing"]
    assert not [e for e in events(path)
                if e["what"] == "missing_from_source" and e["uid"] == "manual:mid"]


def test_orphaned_override_rehomed(path):
    st = Store(path)
    st.overrides["manual:mid"] = {"status": "submitted"}
    s = merge(st, [gb_result(real_midterm()),
                   manual_result(mk("manual:mid", "manual", title="Midterm 1",
                                    due="2026-10-20 18:00"))])
    assert "manual:mid" not in st.tasks
    assert st.overrides == {"ls:mid": {"status": "submitted"}}
    assert st.tasks["ls:mid"].status == Status.SUBMITTED
    assert st.aliases == {"manual:mid": "ls:mid"}
    st.save()
    assert "rehomed_orphan_override" in {e["what"] for e in events(path)}
    merge(st, [gb_result(real_midterm()),
               manual_result(mk("manual:mid", "manual", title="Midterm 1",
                                due="2026-10-20 18:00"))])
    assert st.overrides == {"ls:mid": {"status": "submitted"}}


def test_orphan_rehome_keeps_existing_override_on_real_row(path):
    st = Store(path)
    st.overrides["manual:mid"] = {"status": "submitted", "est_minutes": 10}
    st.overrides["ls:mid"] = {"est_minutes": 99}
    merge(st, [gb_result(real_midterm()),
               manual_result(mk("manual:mid", "manual", title="Midterm 1",
                                due="2026-10-20 18:00"))])
    assert st.overrides["ls:mid"] == {"status": "submitted", "est_minutes": 99}


def test_unmatched_placeholder_stays_and_is_not_missing(path):
    st = Store(path)
    other = mk("ls:q", "learningsuite", title="Quiz 7", due="2026-10-20 23:59")
    s = merge(st, [gb_result(other), manual_result(mk("manual:mid", "manual",
                                                      title="Midterm 1"))])
    assert {"manual:mid", "ls:q"} <= set(st.tasks)
    assert st.tasks["manual:mid"].missing_since is None


def test_no_learningsuite_courses_is_skipped_not_failed(cfg, monkeypatch):
    from src import sync
    monkeypatch.setattr(cfg, "ls_course_ids", lambda: {})
    r = sync._gradebook(cfg)
    assert not r.ok and r.message.startswith("skipped")
