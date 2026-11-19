from datetime import timedelta

import pytest

from src.model import Kind, Task
from src.sources import ics_feed
from src.sources.ics_feed import (clean_title, collapse_windows, decode_feed,
                                  dedupe, parse, repair_mangled_utf8)
from tests.conftest import at


def ics(*events: str) -> str:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            + "".join(f"BEGIN:VEVENT\r\n{e}\r\nEND:VEVENT\r\n" for e in events)
            + "END:VCALENDAR\r\n")


def ev(uid, summary, start, end=None, desc=""):
    s = f"UID:{uid}\r\nSUMMARY:{summary}\r\nDTSTART{start}"
    if end:
        s += f"\r\nDTEND{end}"
    if desc:
        s += f"\r\nDESCRIPTION:{desc}"
    return s


def mk(title, day, course="AAA101", notes=""):
    return Task(uid=f"t:{title}{day}", source="t", course_key=course, title=title,
                due_at=at(f"{day} 23:59"), notes=notes)


def test_clean_title_mashed_boundary_and_no_class():
    assert clean_title("No classSecond Exam") == "Second Exam"
    assert clean_title("No class 不上課Second Exam") == "Second Exam"


def test_clean_title_cjk_boundary():
    assert clean_title("第一課：開學Text Read Aloud") == "第一課：開學 Text Read Aloud"


def test_clean_title_tails_and_whitespace():
    assert clean_title("Quiz 3   Online: https://x") == "Quiz 3"
    assert clean_title("Essay Location: Room 5") == "Essay"
    assert clean_title("  - Final Project -  ") == "Final Project"


def test_clean_title_pure_cancellation_is_empty():
    assert clean_title("No class") == ""
    assert clean_title("No class 不上課") == ""


def corrupt(b: bytes) -> bytes:
    return b.replace(b"\xa0", b" ")


@pytest.mark.parametrize("text", ["根", "校", "狠", "第一課：根據 Quiz 校對 狠", "é"])
def test_repair_roundtrip(text):
    raw = corrupt(text.encode())
    assert decode_feed(raw) == text


def test_repair_needs_actual_corruption():
    assert b"\xa0" in "根".encode()
    with pytest.raises(UnicodeDecodeError):
        corrupt("根".encode()).decode("utf-8")
    assert repair_mangled_utf8(corrupt("根".encode())) == "根".encode()


def test_repair_leaves_valid_and_ascii_alone():
    ok = "Hello 根 world".encode()
    assert repair_mangled_utf8(ok) == ok
    assert decode_feed(ok) == "Hello 根 world"
    assert decode_feed(b"plain ascii") == "plain ascii"


def test_decode_feed_never_raises():
    out = decode_feed(b"abc \xff\xfe def")
    assert out.startswith("abc") and out.endswith("def")


def test_dedupe_merges_bilingual_same_day_keeps_shortest_and_notes():
    a = mk("Second Exam", "2026-10-05")
    b = mk("Second Exam 第二次考試 (Listening)", "2026-10-05", notes="long description")
    out, merged = dedupe([b, a])
    assert [t.title for t in out] == ["Second Exam"]
    assert out[0].notes == "long description"
    assert len(merged) == 1


def test_dedupe_keeps_different_day_or_course():
    ts = [mk("Exam", "2026-10-05"), mk("Exam", "2026-10-06"),
          mk("Exam", "2026-10-05", course="BBB202")]
    out, merged = dedupe(ts)
    assert len(out) == 3 and merged == []


def test_collapse_consecutive_days_to_first_day():
    ts = [mk("First Exam", f"2026-10-0{d}") for d in (1, 2, 3)]
    out, collapsed = collapse_windows(ts)
    assert len(out) == 1
    assert out[0].due_at.date().isoformat() == "2026-10-01"
    assert "Open Oct 1 through Oct 3" in out[0].notes
    assert len(collapsed) == 1


def test_collapse_gap_makes_two_runs():
    ts = [mk("First Exam", d) for d in ("2026-10-01", "2026-10-02", "2026-10-05")]
    out, _ = collapse_windows(ts)
    assert sorted(t.due_at.day for t in out) == [1, 5]


def test_collapse_listening_vs_reading_adjacent_days_not_collapsed():
    ts = [mk("Unit 1 Exam (Listening)", "2026-10-01"),
          mk("Unit 1 Exam (Reading)", "2026-10-02")]
    out, collapsed = collapse_windows(ts)
    assert len(out) == 2 and collapsed == []


def test_collapse_does_not_cross_courses():
    ts = [mk("Exam", "2026-10-01"), mk("Exam", "2026-10-02", course="BBB202")]
    out, _ = collapse_windows(ts)
    assert len(out) == 2


def test_parse_allowlist_and_ignore(cfg):
    text = ics(
        ev("1", "HW 1.1", ":20261005T180000Z"),
        ev("2", "Lecture: Supply and demand", ":20261005T180000Z"),
        ev("3", "9/8", ":20261005T180000Z"),
        ev("4", "*Quiz 1 reminder", ":20261005T180000Z"),
        ev("5", "Exam review day", ":20261006T180000Z"),
        ev("6", "Office hours for the exam", ":20261006T180000Z"),
        ev("7", "No class", ":20261007T180000Z"),
        ev("8", "No classSecond Exam", ":20261008T180000Z"),
    )
    rep = {}
    tasks = parse(text, "AAA101", cfg, report=rep)
    titles = sorted(t.title for t in tasks)
    assert titles == ["HW 1.1", "Second Exam"]
    assert rep["kept"] == 2
    assert "Lecture: Supply and demand" in rep["skipped"]
    by = {t.title: t for t in tasks}
    assert by["HW 1.1"].kind == Kind.HOMEWORK and by["HW 1.1"].category == "homework"
    assert by["HW 1.1"].est_minutes == 60
    assert by["Second Exam"].kind == Kind.EXAM and by["Second Exam"].est_minutes == 240
    assert by["HW 1.1"].uid == "ls_ics:1"


def test_parse_timed_utc_converted_to_local(cfg):
    t, = parse(ics(ev("1", "Quiz 2", ":20261005T160000Z")), "AAA101", cfg)
    assert t.due_at == at("2026-10-05 10:00")
    assert t.due_at.utcoffset() == timedelta(hours=-6)


def test_parse_all_day_becomes_2359_local(cfg):
    t, = parse(ics(ev("1", "Final Project", ";VALUE=DATE:20261015")), "AAA101", cfg)
    assert t.due_at == at("2026-10-15 23:59")


def test_parse_all_day_with_exclusive_dtend(cfg):
    t, = parse(ics(ev("1", "Final Project", ";VALUE=DATE:20261015",
                      ";VALUE=DATE:20261016")), "AAA101", cfg)
    assert t.due_at == at("2026-10-15 23:59")


def test_parse_floating_time_gets_local_tz(cfg):
    t, = parse(ics(ev("1", "Quiz 2", ":20261005T143000")), "AAA101", cfg)
    assert t.due_at == at("2026-10-05 14:30")


def test_parse_uid_hash_fallback_is_stable(cfg):
    e = "SUMMARY:Quiz 2\r\nDTSTART:20261005T160000Z"
    a, = parse(ics(e), "AAA101", cfg)
    b, = parse(ics(e), "AAA101", cfg)
    assert a.uid == b.uid and a.uid.startswith("ls_ics:")


def test_parse_ls_filter_override(cfg):
    cfg.courses["AAA101"]["ls_filter"] = {"actionable": [r"\bwidget\b"], "ignore": []}
    text = ics(ev("1", "Build widget", ":20261005T160000Z"),
               ev("2", "HW 1", ":20261005T160000Z"))
    assert [t.title for t in parse(text, "AAA101", cfg)] == ["Build widget"]


def test_parse_collapses_testing_window(cfg):
    text = ics(*[ev(str(d), "First Exam", f";VALUE=DATE:2026100{d}") for d in (1, 2, 3)])
    rep = {}
    tasks = parse(text, "AAA101", cfg, report=rep)
    assert len(tasks) == 1 and len(rep["collapsed"]) == 1


class FakeResp:
    def __init__(self, content): self.content = content
    def raise_for_status(self): pass


def test_fetch_text_falls_back_to_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ics_feed, "CACHE", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "k.ics").write_text("CACHED", encoding="utf-8")

    def boom(*a, **k): raise ConnectionError("offline")
    monkeypatch.setattr(ics_feed.requests, "get", boom)
    assert ics_feed._fetch_text("http://x", "k") == "CACHED"


def test_fetch_text_no_cache_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ics_feed, "CACHE", tmp_path / "cache")

    def boom(*a, **k): raise ConnectionError("offline")
    monkeypatch.setattr(ics_feed.requests, "get", boom)
    with pytest.raises(ConnectionError):
        ics_feed._fetch_text("http://x", "k")


def test_fetch_text_success_repairs_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(ics_feed, "CACHE", tmp_path / "cache")
    raw = corrupt("第一課 根".encode())
    monkeypatch.setattr(ics_feed.requests, "get", lambda *a, **k: FakeResp(raw))
    assert ics_feed._fetch_text("http://x", "k") == "第一課 根"
    assert (tmp_path / "cache" / "k.ics").read_text(encoding="utf-8") == "第一課 根"


def test_fetch_expands_env_and_reports_failures(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(ics_feed, "CACHE", tmp_path / "cache")
    monkeypatch.setenv("AAA_ICS", "http://feed/aaa")
    seen_urls = []

    def get(url, **k):
        seen_urls.append(url)
        return FakeResp(ics(ev("1", "HW 1", ":20261005T160000Z")).encode())
    monkeypatch.setattr(ics_feed.requests, "get", get)
    tasks, seen = ics_feed.fetch(cfg)
    assert seen_urls == ["http://feed/aaa"]
    assert len(tasks) == 1 and seen == [{"course": "AAA101", "count": 1, "ok": True}]
