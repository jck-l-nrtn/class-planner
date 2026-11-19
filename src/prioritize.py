"""Scores open tasks so the scheduler knows what to place first."""
from __future__ import annotations

import math

from datetime import datetime

from .model import Task, Kind, ScoredTask, Status
from .workload import factor_for as _factor
from .timeline import capacity_minutes
from .dates import fmt


def _category_points(tasks: list[Task]) -> dict[tuple[str, str], float]:
    """Total points per (course, category) — the denominator for grade impact."""
    totals: dict[tuple[str, str], float] = {}
    for t in tasks:
        key = (t.course_key, t.category.lower())
        totals[key] = totals.get(key, 0.0) + max(t.points, 0.0)
    return totals


def _category_counts(tasks: list[Task]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for t in tasks:
        key = (t.course_key, t.category.lower())
        counts[key] = counts.get(key, 0) + 1
    return counts


def grade_impact(cfg, task: Task, points_by_cat, counts_by_cat) -> float:
    """Fraction of the final course grade riding on this one task."""
    if task.impact_override is not None:
        return task.impact_override

    weight = cfg.weight_for(task.course_key, task.category)
    key = (task.course_key, task.category.lower())
    total_pts = points_by_cat.get(key, 0.0)
    if task.points > 0 and total_pts > 0:
        share = task.points / total_pts
    else:
        share = 1.0 / max(counts_by_cat.get(key, 1), 1)
    return weight * share


def effective_deadline(task: Task):
    """The date the RANKING should work back from."""
    if task.prep_by and (task.due_at is None or task.prep_by < task.due_at):
        return task.prep_by
    return task.due_at


def urgency(cfg, slack_hours: float) -> float:
    """Map slack to 0..1. Falls off linearly between the two configured bounds."""
    p = cfg.prefs.get("priority", {})
    crit = float(p.get("slack_critical_hours", 0))
    comf = float(p.get("slack_comfortable_hours", 40))
    if slack_hours <= crit:
        return 1.0
    if slack_hours >= comf:
        return 0.05
    return 1.0 - (slack_hours - crit) / (comf - crit) * 0.95


def exam_ramp_urgency(cfg, task: Task, now: datetime) -> float:
    """Exams need prep spread over days, not a panic the night before."""
    if task.kind != Kind.EXAM or not task.due_at:
        return 0.0
    ramp_days = cfg.exam_ramp_days(task.course_key)
    days_out = (task.due_at - now).total_seconds() / 86400.0
    if days_out > ramp_days or days_out < 0:
        return 0.0
    progress = 1.0 - (days_out / ramp_days)
    return 0.30 + 0.70 * progress


def horizon_decay(cfg, task: Task, now: datetime) -> float:
    """Damp work that is far beyond the planning horizon."""
    target = effective_deadline(task)
    if target is None:
        return 0.15
    p = cfg.prefs.get("priority", {})
    lookahead = float(p.get("lookahead_days", 10))
    half = float(p.get("horizon_decay_days", 14))
    days_out = (target - now).total_seconds() / 86400.0
    beyond = max(0.0, days_out - lookahead)
    return 1.0 / (1.0 + beyond / max(half, 1.0))


def score_all(cfg, tasks: list[Task], now: datetime | None = None,
              external_by_day=None, calibration: dict[str, float] | None = None
              ) -> list[ScoredTask]:
    now = now or cfg.now()
    p = cfg.prefs.get("priority", {})
    mult = p.get("multipliers", {})
    exp = float(p.get("impact_exp", 1.0))

    live = [t for t in tasks if not t.is_done]
    pts_by_cat = _category_points(live)
    cnt_by_cat = _category_counts(live)

    calibration = calibration or {}

    prelim: list[tuple[Task, float, int]] = []
    for t in live:
        cal = _factor(calibration, t.course_key, t.kind.value)
        need = int(t.remaining_minutes * cal) if t.schedulable else 0
        target = effective_deadline(t)
        if target is None:
            prelim.append((t, math.inf, need))
            continue
        cap = capacity_minutes(cfg, now, target, external_by_day)
        prelim.append((t, (cap - need) / 60.0, need))

    prelim.sort(key=lambda x: (x[1], x[0].due_at or datetime.max.replace(tzinfo=now.tzinfo)))


    scored: list[ScoredTask] = []
    committed_before: dict[str, int] = {}
    running = 0
    i = 0
    while i < len(prelim):
        j = i
        tie = (prelim[i][1], effective_deadline(prelim[i][0]))
        while j < len(prelim) and (prelim[j][1], effective_deadline(prelim[j][0])) == tie:
            committed_before[prelim[j][0].uid] = running
            j += 1
        running += sum(n for _t, _s, n in prelim[i:j])
        i = j

    for t, naive_slack, need in prelim:
        reasons: list[str] = []
        target = effective_deadline(t)
        if target is None:
            slack = math.inf
            reasons.append("no due date — backlog only")
        else:
            cap = capacity_minutes(cfg, now, target, external_by_day)
            slack = (cap - need - committed_before[t.uid]) / 60.0
            reasons.append(
                f"{cap/60:.1f}h free before due, {need/60:.1f}h of work left, "
                f"{committed_before[t.uid]/60:.1f}h owed to higher-priority work"
            )

        if t.prep_by and t.due_at and t.prep_by < t.due_at:
            reasons.append(f"prep target {fmt(t.prep_by, '%a %b %-d')} — "
                           f"ahead of the {fmt(t.due_at, '%a %b %-d')} deadline")
        gi = grade_impact(cfg, t, pts_by_cat, cnt_by_cat)
        urg = urgency(cfg, slack)
        decay = horizon_decay(cfg, t, now)
        ramp = exam_ramp_urgency(cfg, t, now)
        if ramp > urg:
            urg = ramp
            days_out = (t.due_at - now).total_seconds() / 86400.0
            reasons.append(f"exam prep window open ({days_out:.1f}d out)")
        s = (gi ** exp) * urg * decay
        reasons.append(f"worth {gi*100:.2f}% of final grade")
        if decay < 0.9 and t.due_at:
            days_out = (t.due_at - now).total_seconds() / 86400.0
            reasons.append(f"{days_out:.0f} days out — damped x{decay:.2f}")

        if t.kind == Kind.EXAM:
            s *= float(mult.get("exam", 1.0)); reasons.append("exam")
        if t.due_at and t.due_at < now:
            s *= float(mult.get("late", 1.0)); reasons.append("PAST DUE")
        if t.hard_deadline:
            s *= float(mult.get("hard_deadline", 1.0)); reasons.append("no late submissions")
        if t.status == Status.IN_PROGRESS:
            s *= float(mult.get("in_progress", 1.0)); reasons.append("already started")
        if t.group_work:
            s *= float(mult.get("group_work", 1.0)); reasons.append("group work")
        if t.category_drops:
            d = float(p.get("drop_discount", 0.85))
            s *= d
            reasons.append(f"category drops {t.category_drops} lowest (x{d})")
        if t.impact_override is not None:
            reasons.append(
                "impact from config/manual_assignments.yaml" if t.source == "manual"
                else "prep weight from courses.yaml (not a graded item)"
                if t.source == "ls_prep"
                else "impact from real gradebook weights")
        if t.agent_weight != 1.0:
            s *= float(t.agent_weight)
            reasons.append(f"agent weight ×{t.agent_weight:g}"
                           + (f": {t.agent_note}" if t.agent_note else ""))

        if slack < 0:
            reasons.append(f"⚠ BEHIND by {abs(slack):.1f}h")

        scored.append(ScoredTask(task=t, score=s * 1000, slack_hours=slack,
                                 grade_impact=gi, urgency=urg, reasons=reasons))

    scored.sort(key=lambda s: -s.score)
    return scored

