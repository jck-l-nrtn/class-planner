"""Calibrates effort estimates against the credit-hour benchmark."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .timeline import build_day

MIN_SCALE, MAX_SCALE = 0.6, 2.0
TOLERANCE = 0.15


def contact_hours(cfg, week_start: date) -> dict[str, float]:
    """Weekly in-class hours per course, read off the real timeline."""
    mins: dict[str, int] = defaultdict(int)
    for i in range(7):
        for b in build_day(cfg, week_start + timedelta(days=i))[0]:
            if b.kind == "class" and b.course_key:
                mins[b.course_key] += b.minutes
    return {k: v / 60 for k, v in mins.items()}


def term_weeks(cfg) -> float:
    start, end = cfg.term_bounds()
    if not start or not end:
        return 15.0
    return max((end - start).days / 7, 1.0)


def benchmark_scale(cfg, tasks, week_start: date) -> tuple[dict[str, float], list[str]]:
    """Per-course multiplier bringing estimated effort in line with benchmark."""
    mult = float(cfg.prefs.get("calibration", {}).get("credit_hour_multiplier", 3))
    contact = contact_hours(cfg, week_start)
    weeks = term_weeks(cfg)

    est: dict[str, float] = defaultdict(float)
    for t in tasks:
        if t.schedulable and not t.is_done:
            est[t.course_key] += t.remaining_minutes / 60

    scales: dict[str, float] = {}
    notes: list[str] = []
    for key, course in cfg.courses.items():
        credits = float(course.get("credits") or 0)
        if not credits:
            continue
        expected = credits * mult - contact.get(key, 0.0)
        if expected <= 0:
            continue
        mine = est.get(key, 0.0) / weeks
        if mine <= 0:
            notes.append(f"**{key}** has no coursework data — roughly "
                         f"{expected:.1f}h/week of real work is invisible to the planner.")
            continue
        ratio = mine / expected
        if abs(ratio - 1.0) <= TOLERANCE:
            continue
        scale = max(MIN_SCALE, min(MAX_SCALE, 1.0 / ratio))
        scales[key] = scale
        notes.append(f"{key} estimates were {ratio*100:.0f}% of the "
                     f"{credits:g}-credit benchmark — scaled ×{scale:.2f}.")
    return scales, notes


def apply(scales: dict[str, float], logged: dict[str, float]) -> dict[str, float]:
    """Merge benchmark scaling with logged-time calibration."""
    merged = dict(logged)
    for course, s in scales.items():
        if not any(k.startswith(f"{course}:") for k in logged):
            merged[f"__course__{course}"] = s
    return merged


def factor_for(merged: dict[str, float], course_key: str, kind: str) -> float:
    exact = merged.get(f"{course_key}:{kind}")
    if exact is not None:
        return exact
    return merged.get(f"__course__{course_key}", 1.0)


def calibration(cfg, store, tasks, day: date) -> dict[str, float]:
    return apply(benchmark_scale(cfg, tasks, day)[0], store.calibration())
