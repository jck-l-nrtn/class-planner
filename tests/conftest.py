"""Shared fixtures."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

DENVER = ZoneInfo("America/Denver")

COMMITMENTS = {
    "timezone": "America/Denver",
    "term": {"start": date(2026, 9, 1), "end": date(2026, 12, 18)},
    "day": {"wake": "07:00", "sleep": "23:00", "wind_down": 45},
    "meals": [
        {"name": "breakfast", "at": "07:30", "minutes": 25, "flex": 45},
        {"name": "lunch", "at": "12:30", "minutes": 30, "flex": 90},
        {"name": "dinner", "at": "18:15", "minutes": 45, "flex": 90},
    ],
    "locations": {"default": "home", "places": ["home", "campus", "work"]},
    "travel": {
        "default_minutes": 15,
        "buffer_before": 5,
        "matrix": [
            {"from": "home", "to": "campus", "minutes": 15},
            {"from": "home", "to": "work", "minutes": 20},
            {"from": "campus", "to": "work", "minutes": 20},
        ],
    },
    "recurring": [
        {"name": "AAA 101", "kind": "class", "course": "AAA101",
         "days": ["mon", "wed", "fri"], "start": "09:00", "end": "09:50",
         "location": "campus"},
        {"name": "BBB 202", "kind": "class", "course": "BBB202",
         "days": ["tue", "thu"], "start": "10:00", "end": "11:15",
         "location": "campus"},
        {"name": "Work", "kind": "work", "days": ["fri"],
         "start": "13:00", "end": "17:00", "location": "work"},
    ],
    "one_offs": [],
}

COURSES = {
    "courses": [
        {"key": "AAA101", "name": "Alpha 101", "credits": 3, "canvas_id": 111,
         "ls_ics_url": "${AAA_ICS}",
         "weights": {"homework": 0.30, "quizzes": 0.10, "exams": 0.45, "project": 0.15},
         "effort": {"homework": 60, "quizzes": 30, "exams": 240, "project": 300},
         "exam_ramp_days": 5},
        {"key": "BBB202", "name": "Beta 202", "credits": 4, "canvas_id": 222,
         "ls_ics_url": None,
         "weights": {"exams": 0.6, "homework": 0.4},
         "effort": {"exams": 300, "homework": 90}},
    ],
    "defaults": {
        "unlisted_category_weight": 0.03,
        "weights": {"homework": 0.40, "quizzes": 0.10, "exams": 0.40, "project": 0.10},
        "effort": {"homework": 120, "quizzes": 45, "exams": 300, "project": 360,
                   "reading": 40},
        "exam_ramp_days": 7,
    },
}

PREFS = {
    "horizon_days": 14,
    "daily_focus_cap_minutes": 270,
    "calibration": {"credit_hour_multiplier": 3},
    "blocks": {"min_minutes": 30, "max_minutes": 90, "break_minutes": 10,
               "long_break_after": 3, "long_break_minutes": 30,
               "daily_study_cap": 360, "context_switch_penalty": True},
    "priority": {
        "slack_critical_hours": 0,
        "slack_comfortable_hours": 40,
        "impact_exp": 1.0,
        "multipliers": {"exam": 1.6, "late": 2.5, "hard_deadline": 1.3,
                        "in_progress": 1.15, "group_work": 1.2},
        "drop_discount": 0.85,
        "lookahead_days": 10,
        "horizon_decay_days": 14,
    },
    "shaping": {"peak_hours": ["09:00-12:00"], "trough_hours": ["13:00-14:30"],
                "no_heavy_before_sleep": 90, "daily_slack_minutes": 60},
}


def at(s: str) -> datetime:
    """Aware America/Denver datetime from 'YYYY-MM-DD HH:MM'."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=DENVER)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    import src.config as config

    d = tmp_path / "config"
    d.mkdir()
    (d / "commitments.yaml").write_text(yaml.safe_dump(COMMITMENTS, sort_keys=False))
    (d / "courses.yaml").write_text(yaml.safe_dump(COURSES, sort_keys=False))
    (d / "prefs.yaml").write_text(yaml.safe_dump(PREFS, sort_keys=False))
    monkeypatch.setattr(config, "CONFIG_DIR", d)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return config.Config()
