"""src/store.py: layering, locking, atomic saves, supersede, event log."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from src import store as store_mod
from src.model import Status
from src.store import Store, migrate, read_events
from tests.conftest import at
from tests.helpers import mk


@pytest.fixture
def path(tmp_path):
    return tmp_path / "store.json"


def new(path, origin="test"):
    return Store(path, origin)


def events(path):
    return [json.loads(l) for l in (path.parent / "events.jsonl").read_text().splitlines()]


def seeded(path, *tasks):
    st = new(path)
    st.upsert(list(tasks))
    st.save()
    return st


def v1():
    return {
        "tasks": {"a": {}, "b": {}},
        "log": [{"uid": "a", "minutes": 5}],
        "weird_extra_key": {"keep": "me"},
        "overrides": {
            "a": {"agent_note": "hard", "agent_weight": 2.0, "est_minutes": 90},
            "b": {"est_minutes": 45, "status": "submitted"},
            "c": {"agent_note": "only note"},
            "d": {"agent_weight": 1.5, "status": "in_progress"},
        }}


def test_migrate_moves_agent_entries_to_estimates():
    m = migrate(v1())
    assert m["version"] == 2
    assert m["estimates"]["a"] == {"agent_note": "hard", "agent_weight": 2.0,
                                   "est_minutes": 90}
    assert "a" not in m["overrides"]
    assert m["estimates"]["c"] == {"agent_note": "only note"}
    assert "c" not in m["overrides"]
    assert m["overrides"]["b"] == {"est_minutes": 45, "status": "submitted"}
    assert m["overrides"]["d"] == {"status": "in_progress"}
    assert m["estimates"]["d"] == {"agent_weight": 1.5}
    assert m["aliases"] == {} and m["sources"] == {}


def test_migrate_loses_no_key_and_is_idempotent():
    raw = v1()
    keys = set(raw)
    once = migrate(json.loads(json.dumps(raw)))
    assert keys <= set(once)
    assert once["weird_extra_key"] == {"keep": "me"} and once["log"] == raw["log"]
    twice = migrate(json.loads(json.dumps(once)))
    assert twice == once


def test_migrate_loses_no_override_field():
    raw = v1()
    before = {(u, k, json.dumps(v)) for u, ov in raw["overrides"].items()
              for k, v in ov.items()}
    m = migrate(json.loads(json.dumps(raw)))
    after = {(u, k, json.dumps(v)) for sect in ("overrides", "estimates")
             for u, ov in m[sect].items() for k, v in ov.items()}
    assert before == after


def test_load_migrates_v1_file(path):
    t = mk().to_dict()
    path.write_text(json.dumps({"tasks": {"canvas:1": t},
                                "overrides": {"canvas:1": {"agent_note": "n",
                                                           "est_minutes": 99}}}))
    st = new(path)
    assert st.estimates["canvas:1"]["est_minutes"] == 99
    assert st.tasks["canvas:1"].est_minutes == 99 and st.tasks["canvas:1"].agent_note == "n"
    assert not st.user_estimated("canvas:1")


def test_save_is_atomic_and_leaves_no_tmp(path):
    st = seeded(path, mk())
    assert json.loads(path.read_text())["version"] == 2
    assert not list(path.parent.glob("*.tmp"))


def test_failed_replace_keeps_old_file_valid(path, monkeypatch):
    st = seeded(path, mk())
    before = path.read_text()
    st.tasks["canvas:1"].title = "changed"

    def boom(*a):
        raise OSError("disk")
    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        st.save()
    assert path.read_text() == before
    json.loads(path.read_text())


def test_roundtrip(path):
    seeded(path, mk(), mk("canvas:2", title="HW 2"))
    st = new(path)
    assert set(st.tasks) == {"canvas:1", "canvas:2"}
    assert st.tasks["canvas:1"].due_at == at("2026-10-10 23:59")


def test_edit_persists_and_sequential_edits_both_survive(path):
    with Store.edit("a", path) as st:
        st.upsert([mk()])
    with Store.edit("b", path) as st:
        st.upsert([mk("canvas:2", title="HW 2")])
    assert set(new(path).tasks) == {"canvas:1", "canvas:2"}


def test_edit_holds_exclusive_lock(path):
    entered, release = threading.Event(), threading.Event()
    with Store.edit("a", path) as st:
        st.upsert([mk()])

        def other():
            with Store.edit("b", path) as s2:
                entered.set()
                s2.upsert([mk("canvas:2", title="HW 2")])
        th = threading.Thread(target=other)
        th.start()
        assert not entered.wait(0.4)
    th.join(5)
    assert entered.is_set() and not th.is_alive()
    assert set(new(path).tasks) == {"canvas:1", "canvas:2"}


def test_concurrent_edits_lose_no_updates(path):
    seeded(path, mk())

    def work():
        with Store.edit("t", path) as st:
            st.log_time("canvas:1", 1)
    ths = [threading.Thread(target=work) for _ in range(12)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(10)
    st = new(path)
    assert st.tasks["canvas:1"].spent_minutes == 12 and len(st.log) == 12


def test_edit_exception_does_not_save_and_releases_lock(path):
    seeded(path, mk())
    with pytest.raises(RuntimeError):
        with Store.edit("a", path) as st:
            st.log_time("canvas:1", 30)
            raise RuntimeError("x")
    assert new(path).tasks["canvas:1"].spent_minutes == 0
    with Store.edit("a", path) as st:
        st.log_time("canvas:1", 5)
    assert new(path).tasks["canvas:1"].spent_minutes == 5


def test_upsert_never_changes_spent_minutes(path):
    st = seeded(path, mk())
    st.log_time("canvas:1", 30)
    st.save()
    st = new(path)
    st.upsert([mk(spent_minutes=0)])
    assert st.tasks["canvas:1"].spent_minutes == 30
    st.upsert([mk(spent_minutes=999)])
    assert st.tasks["canvas:1"].spent_minutes == 30


def test_upsert_complete_marks_unseen_open_task_missing_and_logs(path):
    st = seeded(path, mk(), mk("canvas:2", title="HW 2"))
    r = st.upsert([mk()], complete={("canvas", "AAA101")})
    assert r["newly_missing"] == ["canvas:2"]
    assert st.tasks["canvas:2"].missing_since == datetime.now().date().isoformat()
    assert st.tasks["canvas:1"].missing_since is None
    assert "canvas:2" in r["missing"]
    st.save()
    ev = [e for e in events(path) if e["what"] == "missing_from_source"]
    assert [e["uid"] for e in ev] == ["canvas:2"]


def test_upsert_empty_complete_marks_nothing(path):
    st = seeded(path, mk(), mk("canvas:2", title="HW 2"))
    r = st.upsert([], complete=set())
    assert r["newly_missing"] == [] and r["missing"] == []
    assert all(t.missing_since is None for t in st.tasks.values())
    r = st.upsert([], complete=None)
    assert r["newly_missing"] == []


def test_upsert_complete_for_other_source_or_course_marks_nothing(path):
    st = seeded(path, mk())
    st.upsert([], complete={("learningsuite", "AAA101"), ("canvas", "BBB202")})
    assert st.tasks["canvas:1"].missing_since is None


def test_upsert_missing_skips_done_tasks(path):
    st = seeded(path, mk(status=Status.GRADED, score=9.0))
    st.upsert([], complete={("canvas", "AAA101")})
    assert st.tasks["canvas:1"].missing_since is None


def test_reappearing_task_clears_missing_since(path):
    st = seeded(path, mk())
    st.upsert([], complete={("canvas", "AAA101")})
    assert st.tasks["canvas:1"].missing_since
    st.upsert([mk()], complete={("canvas", "AAA101")})
    assert st.tasks["canvas:1"].missing_since is None
    st.save()
    assert "back_in_source" in {e["what"] for e in events(path)}


def test_kept_task_not_listed_as_missing(path):
    st = seeded(path, mk())
    st.upsert([], complete={("canvas", "AAA101")})
    st.keep("canvas:1")
    r = st.upsert([], complete={("canvas", "AAA101")})
    assert r["missing"] == []


def test_user_status_survives_upsert_until_source_reports_score(path):
    st = seeded(path, mk())
    st.set_status("canvas:1", "submitted")
    st.upsert([mk()])
    assert st.tasks["canvas:1"].status == Status.SUBMITTED
    st.save()
    st = new(path)
    st.upsert([mk()])
    assert st.tasks["canvas:1"].status == Status.SUBMITTED
    st.upsert([mk(status=Status.GRADED, score=8.0, points=10)])
    t = st.tasks["canvas:1"]
    assert t.status == Status.GRADED and t.score == 8.0


def test_user_in_progress_does_not_mask_source_submission(path):
    st = seeded(path, mk())
    st.set_status("canvas:1", "in_progress")
    st.upsert([mk(status=Status.SUBMITTED)])
    assert st.tasks["canvas:1"].status == Status.SUBMITTED


def test_clear_status_restores_source_and_keeps_estimates(path):
    st = seeded(path, mk(est_minutes=60))
    st.set_estimate("canvas:1", 20)
    st.set_status("canvas:1", "submitted")
    assert st.clear_status("canvas:1") is True
    t = st.tasks["canvas:1"]
    assert t.status == Status.NOT_STARTED and t.est_minutes == 20
    assert st.overrides["canvas:1"] == {"est_minutes": 20}
    assert st.clear_status("canvas:1") is False
    st.save()
    assert any(e.get("note") == "undo" for e in events(path))


def test_clear_status_removes_empty_override(path):
    st = seeded(path, mk())
    st.set_status("canvas:1", "submitted")
    st.clear_status("canvas:1")
    assert "canvas:1" not in st.overrides


def test_user_estimate_beats_agent_and_survives_upsert(path):
    st = seeded(path, mk(est_minutes=60))
    st.set_agent_estimate("canvas:1", {"est_minutes": 120, "agent_weight": 2.0,
                                       "agent_note": "big"}, "h1")
    t = st.tasks["canvas:1"]
    assert (t.est_minutes, t.agent_weight, t.agent_note) == (120, 2.0, "big")
    st.set_estimate("canvas:1", 30)
    assert st.tasks["canvas:1"].est_minutes == 30
    assert st.tasks["canvas:1"].agent_weight == 2.0
    st.set_agent_estimate("canvas:1", {"est_minutes": 200}, "h2")
    assert st.tasks["canvas:1"].est_minutes == 30
    st.upsert([mk(est_minutes=60)])
    assert st.tasks["canvas:1"].est_minutes == 30
    assert st.user_estimated("canvas:1")


def test_agent_estimate_survives_upsert_and_is_hash_recorded(path):
    st = seeded(path, mk(est_minutes=60))
    st.set_agent_estimate("canvas:1", {"est_minutes": 90}, "h1")
    st.upsert([mk(est_minutes=60)])
    assert st.tasks["canvas:1"].est_minutes == 90
    assert st.estimates["canvas:1"]["content_hash"] == "h1"
    assert not st.user_estimated("canvas:1")


def test_agent_estimates_ignore_non_schedulable(path):
    st = seeded(path, mk("ls:1", "learningsuite", est_minutes=0, schedulable=False))
    st.set_agent_estimate("ls:1", {"est_minutes": 100, "agent_weight": 3.0}, "h")
    t = st.tasks["ls:1"]
    assert t.est_minutes == 0 and t.agent_weight == 1.0


def test_supersede_carries_everything_and_aliases(path):
    st = seeded(path,
                mk("manual:q1", "manual", title="Quiz 1", est_minutes=30),
                mk("ls:q1", "learningsuite", title="Quiz 1 (real)", spent_minutes=10))
    st.log_time("manual:q1", 25)
    st.set_status("manual:q1", "in_progress")
    st.set_estimate("manual:q1", 45)
    st.set_agent_estimate("manual:q1", {"agent_note": "n"}, "h")
    assert st.supersede("manual:q1", "ls:q1") is True
    assert "manual:q1" not in st.tasks
    new_t = st.tasks["ls:q1"]
    assert new_t.spent_minutes == 35
    assert [e["uid"] for e in st.log] == ["ls:q1"] and st.log[0]["orig_uid"] == "manual:q1"
    assert st.overrides["ls:q1"] == {"status": "in_progress", "est_minutes": 45}
    assert new_t.status == Status.IN_PROGRESS and new_t.est_minutes == 45
    assert st.estimates["ls:q1"]["agent_note"] == "n"
    assert "manual:q1" not in st.overrides and "manual:q1" not in st.estimates
    assert st.aliases == {"manual:q1": "ls:q1"} and st.resolve("manual:q1") == "ls:q1"
    st.set_status("manual:q1", "submitted")
    assert st.tasks["ls:q1"].status == Status.SUBMITTED
    st.save()
    assert new(path).resolve("manual:q1") == "ls:q1"
    assert "superseded_placeholder" in {e["what"] for e in events(path)}


def test_supersede_existing_new_override_wins_but_old_unique_kept(path):
    st = seeded(path, mk("m:1", "manual"), mk("ls:1", "learningsuite"))
    st.set_status("m:1", "submitted")
    st.set_estimate("m:1", 10)
    st.set_estimate("ls:1", 99)
    st.supersede("m:1", "ls:1")
    assert st.overrides["ls:1"] == {"status": "submitted", "est_minutes": 99}


def test_supersede_refuses_bad_input(path):
    st = seeded(path, mk("m:1", "manual"))
    assert st.supersede("m:1", "ls:nope") is False
    assert st.supersede("m:1", "m:1") is False
    assert st.supersede("nope", "m:1") is False
    assert "m:1" in st.tasks


def test_alias_chain_resolves(path):
    st = new(path)
    st.aliases = {"a": "b", "b": "c"}
    assert st.resolve("a") == "c"
    st.aliases = {"a": "b", "b": "a"}
    st.resolve("a")


def test_every_mutation_logs_an_event(path):
    st = seeded(path, mk(), mk("canvas:2", title="HW 2"))
    path.parent.joinpath("events.jsonl").unlink()
    st.set_status("canvas:1", "in_progress")
    st.clear_status("canvas:1")
    st.set_estimate("canvas:1", 33)
    st.log_time("canvas:1", 7)
    st.set_agent_estimate("canvas:2", {"est_minutes": 77}, "h")
    st.keep("canvas:2")
    st.upsert([mk(due="2026-10-12 23:59"),
               mk("canvas:2", title="HW 2", status=Status.GRADED, score=9.0)])
    st.supersede("canvas:2", "canvas:1")
    st.save()
    ev = events(path)
    whats = [e["what"] for e in ev]
    for w in ("status", "estimate", "time_logged", "agent_estimate", "kept_despite_missing",
              "due_changed", "graded", "superseded_placeholder"):
        assert w in whats, w
    for e in ev:
        assert e["origin"] == "test" and e["uid"] and "what" in e
        assert {"old", "new", "ts"} <= set(e)
    st_ev = [e for e in ev if e["what"] == "status"]
    assert (st_ev[0]["old"], st_ev[0]["new"]) == ("not_started", "in_progress")
    assert st_ev[1]["note"] == "undo"
    est = next(e for e in ev if e["what"] == "estimate")
    assert (est["old"], est["new"], est["uid"]) == (60, 33, "canvas:1")
    due = next(e for e in ev if e["what"] == "due_changed")
    assert due["old"].startswith("2026-10-10") and due["new"].startswith("2026-10-12")
    g = next(e for e in ev if e["what"] == "graded")
    assert g["old"] is None and g["new"] == 9.0


def test_events_only_written_on_save(path):
    st = seeded(path, mk())
    ep = path.parent / "events.jsonl"
    n = len(ep.read_text().splitlines())
    st.set_status("canvas:1", "submitted")
    assert len(ep.read_text().splitlines()) == n
    st.save()
    assert len(ep.read_text().splitlines()) == n + 1
    st.save()
    assert len(ep.read_text().splitlines()) == n + 1


def test_events_file_next_to_store_and_read_events(path):
    seeded(path, mk())
    assert (path.parent / "events.jsonl").exists()
    ev = read_events(path=path)
    assert ev and ev[0]["what"] == "appeared"
    (path.parent / "events.jsonl").write_text("garbage\n" + json.dumps({"a": 1}) + "\n")
    assert read_events(path=path) == [{"a": 1}]


def test_closed_by_source_event(path):
    st = seeded(path, mk())
    r = st.upsert([mk(status=Status.SUBMITTED, done_via="exam_complete")])
    assert r["closed_by_source"] == [("HW 1", "AAA101", "exam_complete")]
    st.save()
    e = next(e for e in events(path) if e["what"] == "closed_by_source")
    assert e["note"] == "exam_complete"


def test_set_status_unknown_uid_raises(path):
    st = new(path)
    with pytest.raises(KeyError):
        st.set_status("nope", "submitted")
    with pytest.raises(ValueError):
        seeded(path, mk()).set_status("canvas:1", "bogus")


def test_catch_up_sets_aside_only_untouched_past_due_work(path):
    st = seeded(path,
                mk(uid="a", title="old essay", due="2026-10-01 23:59"),
                mk(uid="b", title="old quiz", due="2026-11-17 23:59"),
                mk(uid="c", title="started old lab", due="2026-10-05 23:59"),
                mk(uid="d", title="logged time on this", due="2026-10-06 23:59"),
                mk(uid="e", title="due today", due="2026-11-18 23:59"),
                mk(uid="f", title="due next week", due="2026-11-25 23:59"),
                mk(uid="g", title="no due date", due=None))
    st.set_status("c", "in_progress")
    st.log_time("d", 20)
    cutoff = at("2026-11-18 00:00")
    assert sorted(t.uid for t in st.overdue_before(cutoff)) == ["a", "b"]
    done = st.catch_up(cutoff)
    assert sorted(t.uid for t in done) == ["a", "b"]
    assert sorted(t.uid for t in st.open_tasks()) == ["c", "d", "e", "f", "g"]
    assert st.catch_up(cutoff) == []
    st.save()
    assert [e["what"] for e in events(path) if e["uid"] in ("a", "b")].count("status") == 2


def test_catch_up_is_undone_one_task_at_a_time(path):
    st = seeded(path, mk(uid="a", due="2026-10-01 23:59"), mk(uid="b", due="2026-10-02 23:59"))
    st.catch_up(at("2026-11-18 00:00"))
    assert st.clear_status("a") is True
    assert [t.uid for t in st.open_tasks()] == ["a"]
