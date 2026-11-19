"""src/settings_edit.py: changing a planner that is already set up, in place."""
import difflib

import pytest
import yaml

from src import config, settings_edit as se, setup_wizard as wiz
from src.web import setup_api as api

HAND_COURSES = """\
# My courses. Weights are from the syllabus.
courses:
  - key: BIO100
    name: "BIO 100: Principles of Biology"
    credits: 3          # lab is separate
    canvas_id: 111
    ls_course_id: null
    weights:
      quizzes:  0.10
      exams:    0.45
    effort: {}
  - key: HIST201
    name: World Civ
    credits: 3
    canvas_id: null
    ls_course_id: aaa
    weights: {}
    effort: {}
defaults:
  late_ok: false
"""
HAND_WEEK = """\
timezone: America/Denver     # Utah
term:
  start: 2026-09-01
  end:   2026-12-18
day:
  wake:      "07:00"
  sleep:     "23:00"
  wind_down: 45
recurring:
  # classes
  - {name: "BIO 100", kind: class, course: BIO100,
     days: [mon, wed, fri], start: "09:00", end: "09:50", location: campus}
  # work, never move this
  - {name: Work, kind: work, days: [tue, thu], start: "13:00", end: "17:00", location: campus}
one_offs: []
"""


def changed_lines(before: str, after: str) -> list[str]:
    return [l for l in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                            lineterm="", n=0)
            if l[:1] in "+-" and l[:3] not in ("+++", "---")]


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    (d / "prefs.yaml").write_text((config.ROOT / "config" / "prefs.yaml").read_text())
    monkeypatch.setattr(config, "CONFIG_DIR", d)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (d / "courses.yaml").write_text(HAND_COURSES)
    (d / "commitments.yaml").write_text(HAND_WEEK)
    return d


@pytest.fixture
def generated(home):
    from datetime import date
    courses = [{"key": "BIO100", "name": "BIO 100", "credits": 3, "canvas_id": 111,
                "ls_course_id": None},
               {"key": "HIST201", "name": "World Civ", "credits": 3, "canvas_id": None,
                "ls_course_id": "aaa"}]
    (home / "courses.yaml").write_text(wiz.courses_yaml(courses, date(2026, 9, 1)))
    (home / "commitments.yaml").write_text(wiz.commitments_yaml(
        "America/Denver", "07:00", "23:00", (date(2026, 9, 1), date(2026, 12, 18)),
        [{"name": "BIO100", "kind": "class", "course": "BIO100", "days": ["mon", "wed", "fri"],
          "start": "09:00", "end": "09:50", "location": "campus"}], date(2026, 9, 1)))
    return home


def both(home):
    return (home / "courses.yaml").read_text(), (home / "commitments.yaml").read_text()


def test_current_reads_courses_meetings_and_week(home):
    cur = se.current()
    bio, hist = cur["courses"]
    assert bio["key"] == "BIO100" and bio["credits"] == 3 and not bio["meets_locked"]
    assert bio["meets"] == {"days": ["mon", "wed", "fri"], "start": "09:00", "end": "09:50"}
    assert hist["meets"] is None and not hist["meets_locked"]
    assert cur["week"] == {"timezone": "America/Denver", "wake": "07:00", "sleep": "23:00",
                           "term_start": "2026-09-01", "term_end": "2026-12-18"}


def test_current_reads_what_setup_wrote(generated):
    assert se.current()["courses"][0]["meets"] == {"days": ["mon", "wed", "fri"],
                                                   "start": "09:00", "end": "09:50"}


def test_time_yaml_turned_into_a_number_is_still_a_time():
    assert se.hhmm(630) == "10:30" and se.hhmm("9:05") == "09:05" and se.hhmm("soon") == ""


def test_saving_the_same_values_writes_nothing(home):
    cur = se.current()
    before = both(home)
    done = se.apply({"update": [{"key": c["key"], "credits": c["credits"], "meets": c["meets"]}
                                for c in cur["courses"]], "week": cur["week"]})
    assert done == {"changed": [], "backups": []}
    assert both(home) == before and not list(home.glob("*.bak-*"))


def test_one_change_moves_one_value_and_nothing_else(home):
    c0, w0 = both(home)
    done = se.apply({"update": [{"key": "BIO100", "credits": 4}]})
    c1, w1 = both(home)
    assert changed_lines(c0, c1) == ["-    credits: 3          # lab is separate",
                                     "+    credits: 4          # lab is separate"]
    assert w1 == w0
    assert done["changed"] == ["BIO100: credits 4"] and len(done["backups"]) == 1
    assert (home / done["backups"][0]).read_text() == c0


def test_meeting_time_changes_in_a_hand_written_one_line_entry(home):
    _, w0 = both(home)
    se.apply({"update": [{"key": "BIO100",
                          "meets": {"days": ["tue", "thu"], "start": "10:00", "end": "11:15"}}]})
    _, w1 = both(home)
    assert changed_lines(w0, w1) == [
        '-     days: [mon, wed, fri], start: "09:00", end: "09:50", location: campus}',
        '+     days: [tue, thu], start: "10:00", end: "11:15", location: campus}']
    assert "# work, never move this" in w1 and "# Utah" in w1
    assert se.current()["courses"][0]["meets"]["days"] == ["tue", "thu"]


def test_meeting_time_changes_in_a_file_setup_wrote(generated):
    _, w0 = both(generated)
    se.apply({"update": [{"key": "BIO100",
                          "meets": {"days": ["tue", "thu"], "start": "10:00", "end": "11:15"}}]})
    _, w1 = both(generated)
    assert se.current()["courses"][0]["meets"] == {"days": ["tue", "thu"], "start": "10:00",
                                                   "end": "11:15"}
    com = yaml.safe_load(w1)
    assert com["recurring"][0]["start"] == "10:00" and com["recurring"][0]["end"] == "11:15"
    assert len(changed_lines(w0, w1)) <= 9 and w1.splitlines()[:4] == w0.splitlines()[:4]


@pytest.mark.parametrize("which", ["home", "generated"])
def test_a_course_can_gain_and_lose_a_meeting_time(which, request):
    d = request.getfixturevalue(which)
    meets = {"days": ["mon", "wed"], "start": "14:00", "end": "15:15"}
    assert se.apply({"update": [{"key": "HIST201", "meets": meets}]})["changed"] == \
        ["HIST201: meets mon/wed 14:00-15:15"]
    assert se.current()["courses"][1]["meets"] == meets
    rec = yaml.safe_load((d / "commitments.yaml").read_text())["recurring"]
    assert rec[-1]["course"] == "HIST201" and rec[-1]["kind"] == "class"
    assert rec[-1]["start"] == "14:00"
    se.apply({"update": [{"key": "HIST201", "meets": None}]})
    assert se.current()["courses"][1]["meets"] is None
    assert se.current()["courses"][0]["meets"] is not None


def test_week_changes_keep_alignment_and_comments(home):
    _, w0 = both(home)
    done = se.apply({"week": {"timezone": "America/Boise", "wake": "06:30", "sleep": "23:00",
                              "term_start": "2026-09-01", "term_end": "2026-12-19"}})
    _, w1 = both(home)
    assert changed_lines(w0, w1) == ["-timezone: America/Denver     # Utah",
                                     "+timezone: America/Boise     # Utah",
                                     "-  end:   2026-12-18", "+  end:   2026-12-19",
                                     '-  wake:      "07:00"', '+  wake:      "06:30"']
    assert done["changed"] == ["timezone America/Boise", "wake 06:30", "term end 2026-12-19"]


def test_a_course_meeting_at_two_times_is_left_to_the_file(home):
    p = home / "commitments.yaml"
    p.write_text(p.read_text().replace("one_offs: []", '''\
  - {name: "BIO lab", kind: class, course: BIO100, days: [thu], start: "15:00", end: "16:50", location: campus}
one_offs: []'''))
    before = p.read_text()
    assert se.current()["courses"][0]["meets_locked"] is True
    done = se.apply({"update": [{"key": "BIO100", "credits": 3,
                                 "meets": {"days": ["mon"], "start": "08:00", "end": "08:50"}}]})
    assert done["changed"] == [] and p.read_text() == before


def test_unknown_courses_are_ignored(home):
    before = both(home)
    assert se.apply({"update": [{"key": "NOPE101", "credits": 9}]})["changed"] == []
    assert both(home) == before


def test_bad_input_is_refused_and_nothing_is_written(home):
    before = both(home)
    with pytest.raises(se.Invalid) as e:
        se.apply({"update": [{"key": "BIO100", "credits": 40,
                              "meets": {"days": ["mon"], "start": "10:00", "end": "09:00"}},
                             {"key": "HIST201", "meets": {"days": [], "start": "10:00",
                                                          "end": "11:00"}}],
                  "week": {"timezone": "Mars/Base", "wake": "early",
                           "term_start": "2026-09-01", "term_end": "2026-08-01"}})
    assert set(e.value.errors) == {"credits:BIO100", "meets:BIO100", "meets:HIST201",
                                   "timezone", "wake", "term_end"}
    assert both(home) == before and not list(home.glob("*.bak-*"))


def test_days_with_no_time_is_an_error_but_all_empty_means_no_time(home):
    errors = {}
    assert se.clean_meets({"days": [], "start": "", "end": ""}, "m", errors) is None
    assert se.clean_meets(None, "m", errors) is None and errors == {}
    assert se.clean_meets({"days": ["tue"], "start": "", "end": ""}, "m", errors) is None
    assert errors == {"m": "Give a start and an end time."}


def test_a_result_the_planner_cannot_read_is_rolled_back(home, monkeypatch):
    before = both(home)

    def broken():
        raise RuntimeError("cannot read")
    monkeypatch.setattr(config, "Config", broken)
    with pytest.raises(se.Invalid, match="Nothing was changed"):
        se.apply({"update": [{"key": "BIO100", "credits": 4}]})
    assert both(home) == before


@pytest.fixture
def served(home, tmp_path, monkeypatch):
    from src import agent
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(agent, "find", lambda cli: None)
    return home


def test_state_carries_the_saved_settings(served):
    cur = api.state()["current"]
    assert [c["key"] for c in cur["courses"]] == ["BIO100", "HIST201"]
    assert cur["week"]["wake"] == "07:00"


def test_save_route_changes_and_reports(served):
    res, code = api.save({"courses": [{"key": "BIO100", "credits": "4",
                                       "meets": {"days": ["mon", "wed"], "start": "09:00",
                                                 "end": "09:50"}},
                                      {"key": "HIST201", "credits": "3", "meets": None}],
                          "timezone": "America/Denver", "wake": "07:00", "sleep": "22:30",
                          "term_start": "2026-09-01", "term_end": "2026-12-18"})
    assert code == 200 and res["ok"]
    assert res["changed"] == ["BIO100: credits 4", "BIO100: meets mon/wed 09:00-09:50",
                              "sleep 22:30"]
    assert len(res["backups"]) == 2


def test_save_route_reports_errors_by_field(served):
    res, code = api.save({"courses": [{"key": "BIO100", "credits": "lots"}], "wake": "25:00"})
    assert code == 400 and set(res["errors"]) == {"credits:BIO100", "wake"}


def test_save_is_refused_before_setup(served):
    (served / "courses.yaml").unlink()
    assert api.state()["current"] is None
    assert api.save({"courses": []})[1] == 400


def test_setup_accepts_picked_days_and_times(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_canvas", {"name": "", "error": "", "courses": [
        {"canvas_id": 111, "name": "BIO 100: Principles of Biology", "code": "BIO 100-003"}]})
    monkeypatch.setattr(api, "_ls", {"status": "idle", "detail": "", "courses": [], "term": "",
                                     "session": None})
    week = {"timezone": "America/Denver", "wake": "07:00", "sleep": "23:00",
            "term_start": "2026-09-01", "term_end": "2026-12-18"}
    clean, errors = api._validate({**week, "courses": [{"key": "BIO100", "credits": 3, "meets": {
        "days": ["fri", "mon"], "start": "9:00", "end": "09:50"}}]})
    assert errors == {} and clean["courses"][0]["meets"] == (["mon", "fri"], "09:00", "09:50")
    clean, errors = api._validate({**week, "courses": [{"key": "BIO100", "credits": 3,
                                                        "meets": {"days": ["mon"], "start": "",
                                                                  "end": ""}}]})
    assert "meets:BIO100" in errors
    clean, errors = api._validate({**week, "courses": [{"key": "BIO100", "credits": 3,
                                                        "meets": "MWF 9:00-9:50"}]})
    assert errors == {} and clean["courses"][0]["meets"] == (["mon", "wed", "fri"], "09:00",
                                                             "09:50")
