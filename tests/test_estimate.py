"""src/estimate.py: which tasks get (re-)estimated, and range checks on answers."""
from __future__ import annotations

import pytest

from src.estimate import check, pending
from src.model import Status
from src.store import Store
from tests.helpers import mk


class FakeContent:
    def __init__(self, recs=None):
        self.recs = recs or {}

    def get_task(self, uid):
        return self.recs.get(uid)


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "store.json")
    st.upsert([mk("canvas:1", title="HW 1", due="2026-10-10 23:59"),
               mk("canvas:2", title="HW 2", due="2026-10-09 23:59"),
               mk("ls:att", "learningsuite", title="Day 1", est_minutes=0,
                  schedulable=False)])
    return st


def uids(rows):
    return [r["uid"] for r in rows]


def test_never_estimated_is_pending_sorted_by_due(store):
    rows = pending(store, FakeContent({"canvas:1": {"hash": "h", "description": "d"}}))
    assert uids(rows) == ["canvas:2", "canvas:1"]
    assert rows[1]["description"] == "d" and rows[1]["content_hash"] == "h"
    assert rows[0]["current"] == 60


def test_non_schedulable_never_pending(store):
    assert "ls:att" not in uids(pending(store, FakeContent()))
    assert "ls:att" not in uids(pending(store, FakeContent(), redo=True))


def test_user_estimate_never_pending_even_with_redo(store):
    store.set_estimate("canvas:1", 25)
    assert "canvas:1" not in uids(pending(store, FakeContent()))
    assert "canvas:1" not in uids(pending(store, FakeContent(), redo=True))


def test_user_estimate_beats_existing_agent_estimate(store):
    store.set_agent_estimate("canvas:1", {"est_minutes": 80}, "old")
    store.set_estimate("canvas:1", 25)
    assert "canvas:1" not in uids(pending(store, FakeContent({"canvas:1": {"hash": "new"}})))


def test_matching_hash_not_pending(store):
    store.set_agent_estimate("canvas:1", {"est_minutes": 80}, "h1")
    rows = pending(store, FakeContent({"canvas:1": {"hash": "h1"}}))
    assert "canvas:1" not in uids(rows)


def test_changed_hash_pending(store):
    store.set_agent_estimate("canvas:1", {"est_minutes": 80}, "h1")
    rows = pending(store, FakeContent({"canvas:1": {"hash": "h2", "description": "new"}}))
    assert "canvas:1" in uids(rows)


def test_redo_makes_estimated_pending(store):
    store.set_agent_estimate("canvas:1", {"est_minutes": 80}, "h1")
    c = FakeContent({"canvas:1": {"hash": "h1"}})
    assert "canvas:1" not in uids(pending(store, c))
    assert "canvas:1" in uids(pending(store, c, redo=True))


def test_legacy_estimate_without_hash_counts_as_current(store):
    store.estimates["canvas:1"] = {"est_minutes": 80}
    assert "canvas:1" not in uids(pending(store, FakeContent({"canvas:1": {"hash": "x"}})))
    assert "canvas:1" in uids(pending(store, FakeContent({"canvas:1": {"hash": "x"}}),
                                      redo=True))


def test_done_tasks_not_pending(store):
    store.upsert([mk("canvas:1", status=Status.GRADED, score=9.0)])
    assert "canvas:1" not in uids(pending(store, FakeContent()))


def test_current_reports_source_estimate_not_agent_one(store):
    store.set_agent_estimate("canvas:1", {"est_minutes": 80}, "h1")
    row = next(r for r in pending(store, FakeContent(), redo=True) if r["uid"] == "canvas:1")
    assert row["current"] == 60


def test_missing_content_record_ok(store):
    rows = pending(store, FakeContent())
    assert all(r["description"] == "" and r["content_hash"] is None for r in rows)


@pytest.mark.parametrize("est", [4, 721, 0, -30, 10_000, None, "60", [60]])
def test_check_rejects_out_of_range_estimate(est):
    fields, problems = check({"uid": "u", "est_minutes": est, "agent_weight": 1.0})
    assert "est_minutes" not in fields
    assert len(problems) == 1 and "u" in problems[0]


@pytest.mark.parametrize("est,want", [(5, 5), (720, 720), (90, 90), (45.9, 45)])
def test_check_accepts_in_range(est, want):
    fields, problems = check({"uid": "u", "est_minutes": est})
    assert fields["est_minutes"] == want and problems == []


@pytest.mark.parametrize("w,want", [(0.0, 0.1), (-3, 0.1), (0.05, 0.1), (9, 5.0),
                                    (5.0, 5.0), (1.5, 1.5), (1, 1.0)])
def test_check_clamps_weight(w, want):
    fields, _ = check({"uid": "u", "est_minutes": 30, "agent_weight": w})
    assert fields["agent_weight"] == want and isinstance(fields["agent_weight"], float)


def test_check_weight_absent_or_junk_ignored():
    assert "agent_weight" not in check({"uid": "u", "est_minutes": 30})[0]
    assert "agent_weight" not in check({"uid": "u", "est_minutes": 30,
                                        "agent_weight": "high"})[0]


def test_check_truncates_note():
    fields, _ = check({"uid": "u", "est_minutes": 30, "agent_note": "x" * 1000})
    assert len(fields["agent_note"]) == 300
    assert "agent_note" not in check({"uid": "u", "est_minutes": 30, "agent_note": ""})[0]


def test_bad_estimate_cannot_reach_a_schedule(store):
    fields, _ = check({"uid": "canvas:1", "est_minutes": 5000, "agent_weight": 99,
                       "agent_note": "n"})
    store.set_agent_estimate("canvas:1", fields, "h")
    t = store.tasks["canvas:1"]
    assert t.est_minutes == 60 and t.agent_weight == 5.0
