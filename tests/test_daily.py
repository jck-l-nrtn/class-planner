"""src/daily.py pure functions: select(), validate(), nearest_days()."""
from __future__ import annotations

import pytest

from src import daily
from src.daily import CAND_KEEP, CAND_MAX, IMMINENT_DAYS, nearest_days, select, validate


def row(uid, days_out=10, prep=None, remaining=60, title=None, course="AAA101", **kw):
    r = {"uid": uid, "title": title or f"Task {uid}", "course": course,
         "days_out": days_out, "prep_days_out": prep, "remaining": remaining,
         "impact_pct": 1.0, "detail": "x", "reasons": []}
    r.update(kw)
    return r


def ranked(uid, tier="important", minutes=30, rank=1, title=None):
    return {"rank": rank, "tier": tier, "uid": uid, "title": title or f"Task {uid}",
            "suggested_minutes": minutes, "action": "do it", "urgency": "because"}


@pytest.mark.parametrize("d,p,want", [
    (5, None, 5), (None, None, None), (5, 2, 2), (1, 3, 1), (None, 0, 0),
    (-2, None, -2), (None, 4, 4)])
def test_nearest_days(d, p, want):
    assert nearest_days({"days_out": d, "prep_days_out": p}) == want


def test_select_small_list_unchanged():
    rows = [row(f"u{i}") for i in range(5)]
    assert select(rows) == rows


def test_select_keeps_top_slice_in_order():
    rows = [row(f"u{i}") for i in range(CAND_KEEP + 10)]
    got = select(rows)
    assert got == rows[:CAND_KEEP]


def test_select_adds_back_imminent_low_ranked_row():
    rows = [row(f"u{i}") for i in range(CAND_KEEP + 10)]
    rows[CAND_KEEP + 3] = row("reading", days_out=1, remaining=20)
    got = select(rows)
    assert "reading" in [r["uid"] for r in got]
    assert [r["uid"] for r in got[:CAND_KEEP]] == [f"u{i}" for i in range(CAND_KEEP)]


def test_select_adds_row_whose_prep_target_is_imminent():
    rows = [row(f"u{i}") for i in range(CAND_KEEP + 5)]
    rows[CAND_KEEP + 1] = row("prep", days_out=9, prep=1)
    assert "prep" in [r["uid"] for r in select(rows)]


def test_select_boundary_and_far_rows_excluded():
    rows = [row(f"u{i}") for i in range(CAND_KEEP)]
    rows += [row("edge", days_out=IMMINENT_DAYS), row("far", days_out=IMMINENT_DAYS + 1),
             row("nodue", days_out=None)]
    uids = [r["uid"] for r in select(rows)]
    assert "edge" in uids and "far" not in uids and "nodue" not in uids


def test_select_includes_overdue():
    rows = [row(f"u{i}") for i in range(CAND_KEEP)] + [row("late", days_out=-3)]
    assert "late" in [r["uid"] for r in select(rows)]


def test_select_caps_at_cand_max_keeping_nearest():
    rows = [row(f"u{i}") for i in range(CAND_KEEP)]
    rows += [row(f"n{i}", days_out=i % 3) for i in range(30)]
    got = select(rows)
    assert len(got) == CAND_MAX
    extra = got[CAND_KEEP:]
    assert [nearest_days(r) for r in extra] == sorted(nearest_days(r) for r in extra)
    assert all(nearest_days(r) == 0 for r in extra[:10])


def test_select_does_not_duplicate_top_rows():
    rows = [row(f"u{i}", days_out=0) for i in range(CAND_KEEP + 3)]
    got = select(rows)
    uids = [r["uid"] for r in got]
    assert len(uids) == len(set(uids)) == CAND_KEEP + 3


def test_validate_rejects_unknown_uid():
    cands = [row("a")]
    out = validate({"ranked": [ranked("a"), ranked("ghost", title="Ghost HW")],
                    "later": [], "day_summary": "s"}, cands, 120)
    assert [r["uid"] for r in out["ranked"]] == ["a"]
    assert out["rejected"] == ["Ghost HW: not in the backlog"]
    assert out["day_summary"] == "s"


def test_validate_caps_minutes_at_remaining():
    out = validate({"ranked": [ranked("a", minutes=500)], "later": []},
                   [row("a", remaining=45)], 600)
    assert out["ranked"][0]["suggested_minutes"] == 45


def test_validate_bad_minutes_default_to_remaining():
    r = ranked("a")
    r["suggested_minutes"] = -5
    out = validate({"ranked": [r], "later": []}, [row("a", remaining=40)], 600)
    assert out["ranked"][0]["suggested_minutes"] == 40


def test_validate_bad_tier_becomes_important():
    r = ranked("a", tier="URGENT!!")
    out = validate({"ranked": [r], "later": []}, [row("a")], 600)
    assert out["ranked"][0]["tier"] == "important"


def test_validate_sorts_by_rank_and_strips_detail():
    out = validate({"ranked": [ranked("a", rank=2), ranked("b", rank=1)], "later": []},
                   [row("a"), row("b")], 600)
    assert [r["uid"] for r in out["ranked"]] == ["b", "a"]
    assert all("detail" not in r for r in out["ranked"])


def test_validate_demotes_over_budget_non_must_keeps_must():
    cands = [row(f"x{i}", remaining=100) for i in range(4)]
    ans = {"ranked": [ranked("x0", "important", 100, 1),
                      ranked("x1", "must_today", 100, 2),
                      ranked("x2", "if_time", 100, 3),
                      ranked("x3", "must_today", 100, 4)], "later": []}
    out = validate(ans, cands, budget=150)
    tiers = {r["uid"]: r["tier"] for r in out["ranked"]}
    assert tiers == {"x0": "important", "x1": "must_today", "x3": "must_today"}
    assert "x2" in [d["uid"] for d in out["later"]]


def test_validate_must_today_kept_even_if_alone_exceeds_budget():
    out = validate({"ranked": [ranked("a", "must_today", 400)], "later": []},
                   [row("a", remaining=400)], budget=60)
    assert [r["uid"] for r in out["ranked"]] == ["a"]


def test_validate_without_budget_keeps_everything():
    cands = [row(f"x{i}", remaining=300) for i in range(3)]
    out = validate({"ranked": [ranked(f"x{i}", minutes=300, rank=i) for i in range(3)],
                    "later": []}, cands, None)
    assert len(out["ranked"]) == 3


def test_validate_later_unknown_uid_nulled():
    out = validate({"ranked": [], "later": [{"uid": "nope", "title": "T", "why": "w"}]},
                   [row("a", days_out=9)], 100)
    assert out["later"][0]["uid"] is None


def test_validate_guarantees_imminent_surfaced():
    cands = [row("far", days_out=9),
             row("today", days_out=0, title="Reading 4", remaining=15),
             row("tmrw", days_out=1, title="Quiz 2"),
             row("pre", days_out=8, prep=2, title="Topic Overview 3"),
             row("soon3", days_out=3, title="Not imminent"),
             row("over", days_out=-1, title="Overdue thing"),
             row("nodue", days_out=None, title="No due")]
    out = validate({"ranked": [ranked("far")], "later": [], "day_summary": ""}, cands, 600)
    shown = {r["uid"] for r in out["ranked"]} | {d["uid"] for d in out["later"]}
    assert {"today", "tmrw", "pre", "over"} <= shown
    assert not {"soon3", "nodue"} & shown
    assert set(out["surfaced_imminent"]) == {"today", "tmrw", "pre", "over"}
    assert out["surfaced_imminent"] == ["over", "today", "tmrw", "pre"]
    why = {d["uid"]: d["why"] for d in out["later"]}
    assert why["over"].startswith("overdue")
    assert why["today"].startswith("due today")
    assert why["tmrw"].startswith("due tomorrow")
    assert why["pre"].startswith("due in 2 days")


def test_validate_does_not_resurface_what_agent_already_placed():
    cands = [row("a", days_out=0), row("b", days_out=1, title="Quiz B")]
    ans = {"ranked": [ranked("a")],
           "later": [{"uid": "b", "title": "Quiz B", "why": "tomorrow"}]}
    out = validate(ans, cands, 600)
    assert out["surfaced_imminent"] == [] and len(out["later"]) == 1


def test_validate_title_mention_in_later_counts_as_shown():
    cands = [row("b", days_out=1, title="Quiz B")]
    ans = {"ranked": [], "later": [{"uid": None, "title": "Quiz B", "why": "w"}]}
    assert validate(ans, cands, 600)["surfaced_imminent"] == []


def test_validate_demoted_imminent_not_double_listed():
    cands = [row("a", days_out=1, remaining=200), row("b", days_out=1, remaining=200)]
    ans = {"ranked": [ranked("a", "important", 200, 1), ranked("b", "important", 200, 2)],
           "later": []}
    out = validate(ans, cands, 250)
    assert [r["uid"] for r in out["ranked"]] == ["a"]
    assert [d["uid"] for d in out["later"]] == ["b"]
    assert out["surfaced_imminent"] == []


def test_validate_handles_empty_answer():
    out = validate({}, [row("a", days_out=0)], 100)
    assert out["ranked"] == [] and out["surfaced_imminent"] == ["a"]
