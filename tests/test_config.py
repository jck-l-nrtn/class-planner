from datetime import date

from src.config import expand_env


def test_expand_env(monkeypatch):
    monkeypatch.setenv("FOO_X", "bar")
    monkeypatch.delenv("NOPE_X", raising=False)
    assert expand_env("https://x/${FOO_X}/y") == "https://x/bar/y"
    assert expand_env("a${NOPE_X}b") == "ab"
    assert expand_env(None) is None
    assert expand_env(5) == 5
    assert expand_env("no refs") == "no refs"


def test_cfg_loads(cfg):
    assert str(cfg.tz) == "America/Denver"
    assert cfg.canvas_map == {111: "AAA101", 222: "BBB202"}
    assert cfg.term_bounds() == (date(2026, 9, 1), date(2026, 12, 18))


def test_weight_exact_and_fuzzy(cfg):
    assert cfg.weight_for("AAA101", "homework") == 0.30
    assert cfg.weight_for("AAA101", "Homework") == 0.30
    assert cfg.weight_for("AAA101", "Homework 1-5") == 0.30


def test_weight_unlisted_uses_default(cfg):
    assert cfg.weight_for("AAA101", "participation") == 0.03


def test_weight_unlisted_residual_capped(cfg):
    assert cfg.weight_for("BBB202", "reading") == 0.03
    cfg.course_defaults.pop("unlisted_category_weight")
    assert cfg.weight_for("BBB202", "reading") == 0.0
    cfg.courses["BBB202"]["weights"] = {"exams": 0.5}
    assert cfg.weight_for("BBB202", "reading") == 0.05


def test_weight_unknown_course_uses_defaults(cfg):
    assert cfg.weight_for("ZZZ", "exams") == 0.40


def test_effort(cfg):
    assert cfg.effort_for("AAA101", "exams") == 240
    assert cfg.effort_for("AAA101", "Homework 3") == 60
    assert cfg.effort_for("AAA101", "mystery") == 120
    assert cfg.effort_for("ZZZ", "reading") == 40


def test_exam_ramp(cfg):
    assert cfg.exam_ramp_days("AAA101") == 5
    assert cfg.exam_ramp_days("BBB202") == 7


def test_travel_minutes(cfg):
    assert cfg.travel_minutes("home", "campus") == 15
    assert cfg.travel_minutes("campus", "home") == 15
    assert cfg.travel_minutes("work", "campus") == 20
    assert cfg.travel_minutes("home", "home") == 0
    assert cfg.travel_minutes("", "campus") == 0
    assert cfg.travel_minutes("home", "moon") == 15
