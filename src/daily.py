"""Today's plan: the engine supplies facts, an agent supplies judgment."""
from __future__ import annotations

import json
from datetime import date, datetime, time

from . import agent, calendars, journal
from .config import OUT_DIR, write_atomic
from .content import ContentStore
from .prioritize import score_all
from .store import Store
from .timeline import free_minutes
from .workload import calibration
from .dates import fmt

HORIZON = 21
CAND_KEEP = 24
CAND_MAX = 40
IMMINENT_DAYS = 2

TIERS = ["must_today", "important", "if_time"]

SCHEMA = {
    "type": "object",
    "properties": {
        "day_summary": {"type": "string"},
        "ranked": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "rank": {"type": "integer"},
                "tier": {"type": "string", "enum": TIERS},
                "uid": {"type": "string"},
                "title": {"type": "string"},
                "suggested_minutes": {"type": "integer"},
                "action": {"type": "string"},
                "urgency": {"type": "string"},
            },
            "required": ["rank", "tier", "uid", "title", "suggested_minutes",
                         "action", "urgency"],
            "additionalProperties": False}},
        "later": {"type": "array", "items": {
            "type": "object",
            "properties": {"uid": {"type": "string"}, "title": {"type": "string"},
                           "why": {"type": "string"}},
            "required": ["uid", "title", "why"],
            "additionalProperties": False}},
    },
    "required": ["day_summary", "ranked", "later"],
    "additionalProperties": False,
}

PREAMBLE = """You are the daily planner for one university student. You are given roughly how much study time they have today and their backlog of assignments. Produce a RANKED to-do list for today -- most pressing first -- and for each item say how long to spend on it and what specifically to do. Do NOT assign clock times; the student schedules the actual hours themselves.

For each ranked item:
  tier               "must_today"  genuinely has to be done today: due today or
                                   tonight, or it gates other work, or skipping
                                   it directly costs points now.
                     "important"   should really happen today to stay ahead (due
                                   in the next day or two, or high grade impact),
                                   but the world won't end if bumped.
                     "if_time"     worth doing if there's room: getting ahead,
                                   low-stakes, or comfortably far from due.
                     Be honest and selective. An empty must_today is fine.
  rank               1 = do this first. Order by real pressure across ALL tiers.
  suggested_minutes  how long to spend on it TODAY (not the whole assignment).
                     Spread exam prep across days rather than one marathon.
  action             one sentence, to the student, on what specifically to do --
                     "draft the intro and first two body paragraphs", not "work
                     on the essay".
  urgency            one sentence on WHY it's ranked here.

Rules:
  * Only use uids from the backlog. Never invent an assignment.
  * suggested_minutes must not exceed an item's minutes_remaining.
  * Total suggested_minutes should fit within today's budget. Anything
    important that doesn't fit goes in `later` (with its uid) and a one-line
    reason, so nothing pressing silently vanishes.
  * The "% of grade" figures are given with where they came from. "gradebook"
    and "canvas" are exact. "hand-set" and "proxy" are the student's own
    estimates of importance -- respect them, but don't describe them as exact.
  * Don't pad. A 10-point reading quiz is 20-40 min, not two hours.

`day_summary`: two sentences -- what today is really about, and the one thing not
to skip.
"""

WEIGHT_LABEL = {"gradebook": "gradebook", "canvas": "Canvas", "manual": "hand-set",
                "proxy": "proxy", "config": "estimated"}


def plan_path(day: date):
    return OUT_DIR / f"day-{day:%Y-%m-%d}.json"


def read_plan(day: date) -> dict | None:
    try:
        return json.loads(plan_path(day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_plan(day: date, plan: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_atomic(plan_path(day), json.dumps(plan, indent=1, ensure_ascii=False))


def task_row(cfg, s, day: date, content: ContentStore | None = None) -> dict:
    """One scored task as the agent and the UI both see it."""
    t = s.task
    rec = (content.get_task(t.uid) if content else None) or {}
    prep = t.prep_by if t.prep_by and t.due_at and t.prep_by < t.due_at else None
    return {
        "uid": t.uid, "title": t.title, "course": t.course_key, "kind": t.kind.value,
        "category": t.category,
        "link": rec.get("html_url") or t.url or "",
        "due": t.due_at.isoformat() if t.due_at else None,
        "due_label": fmt(t.due_at, "%a %b %-d") if t.due_at else "no due date",
        "days_out": (t.due_at.date() - day).days if t.due_at else None,
        "prep_by": prep.isoformat() if prep else None,
        "prep_label": fmt(prep, "%a %b %-d") if prep else None,
        "prep_days_out": (prep.date() - day).days if prep else None,
        "impact_pct": round(s.grade_impact * 100, 2),
        "weight_source": t.weight_source,
        "weight_label": WEIGHT_LABEL.get(t.weight_source, t.weight_source),
        "est": t.est_minutes, "spent": t.spent_minutes,
        "remaining": t.remaining_minutes,
        "hard_deadline": t.hard_deadline,
        "note": (t.agent_note or "").strip(),
        "source_note": t.notes,
        "reasons": s.reasons, "score": round(s.score, 2),
        "detail": (rec.get("description") or "")[:400],
    }


def nearest_days(c: dict) -> int | None:
    """Days until the date that matters first: the prep target if there is one."""
    ds = [d for d in (c["days_out"], c["prep_days_out"]) if d is not None]
    return min(ds) if ds else None


def candidates(cfg, day: date, store: Store, dismissed: set[str],
               busy: dict | None = None) -> list[dict]:
    calib = calibration(cfg, store, store.open_tasks(), day)
    scored = score_all(cfg, store.open_tasks(), now=cfg.at(day, time(6, 0)),
                       external_by_day=busy, calibration=calib)
    content = ContentStore()
    rows = []
    for s in scored:
        t = s.task
        if t.uid in dismissed or not t.schedulable:
            continue
        if t.due_at and (t.due_at.date() - day).days > HORIZON:
            continue
        if t.due_at and t.due_at.date() < day and t.kind.value == "exams":
            continue
        rows.append(task_row(cfg, s, day, content))
    return select(rows)


def select(rows: list[dict]) -> list[dict]:
    """Keep the prompt tight without ever hiding a deadline."""
    top = rows[:CAND_KEEP]
    near = [c for c in rows[CAND_KEEP:]
            if (d := nearest_days(c)) is not None and d <= IMMINENT_DAYS]
    near.sort(key=nearest_days)
    return top + near[:CAND_MAX - CAND_KEEP]


def build_prompt(day: date, free: int, budget: int, cands: list[dict], tz=None) -> str:
    lines = [PREAMBLE, f"\n=== TODAY: {fmt(day, '%A, %B %-d')} ===",
             f"About {free} minutes ({free/60:.1f} h) are free today, but plan AT "
             f"MOST {budget} minutes ({budget/60:.1f} h) of focused work. That cap "
             f"is deliberate, not a target to fill. Exceed it only for a must_today "
             f"deadline that truly cannot wait.", "\nBACKLOG:"]
    for c in cands:
        due = f"due {c['due_label']}" + (f" ({c['days_out']}d)"
                                         if c["days_out"] is not None else "")
        lines.append(f"\n  {c['uid']}\n    {c['title']}  [{c['course']} · {c['kind']}]\n"
                     f"    {due} · {c['impact_pct']}% of grade ({c['weight_label']}) · "
                     f"{c['remaining']} min of work left"
                     + (" · NO LATE SUBMISSIONS" if c["hard_deadline"] else ""))
        if c["prep_by"]:
            n = c["prep_days_out"]
            when = "TODAY" if n == 0 else "already past" if n < 0 else f"in {n}d"
            lines.append(f"    PREP TARGET {c['prep_label']} ({when}): this must be "
                         f"done BEFORE class, not handed in. Plan it by the target; "
                         f"the due date is when class starts.")
        if c["note"]:
            lines.append(f"    estimator: {c['note']}")
        if c["detail"]:
            lines.append(f"    assignment: {c['detail']}")
    notes = journal.recent(days=2, tz=tz) if tz else journal.recent(days=2)
    if notes:
        lines.append("\nWHAT THE STUDENT TOLD YOU (most recent last) -- treat as "
                     "ground truth about what's already done:")
        lines += [f"  [{e.get('ts', '')[:16].replace('T', ' ')}] {e['text']}" for e in notes]
        lines.append("Don't re-list work they say is finished, shrink minutes for "
                     "anything partly done, and honor what they say they will or "
                     "won't get to. If a note conflicts with the gradebook, trust "
                     "the note (a sync hasn't caught up).")
    lines.append(f"\nBuild today's list within the {budget}-minute cap. Return JSON "
                 f"per the schema.")
    return "\n".join(lines)


def validate(answer: dict, cands: list[dict], budget: int | None = None) -> dict:
    """Ground the agent's answer in the backlog."""
    by_uid = {c["uid"]: c for c in cands}
    kept, rejected = [], []
    for r in answer.get("ranked", []):
        c = by_uid.get(r.get("uid"))
        if not c:
            rejected.append(f"{r.get('title', '?')}: not in the backlog")
            continue
        m = r.get("suggested_minutes")
        m = m if isinstance(m, int) and m > 0 else c["remaining"]
        item = {**c, "rank": r.get("rank", 999),
                "tier": r.get("tier") if r.get("tier") in TIERS else "important",
                "suggested_minutes": min(m, c["remaining"]) if c["remaining"] else m,
                "action": r.get("action", ""), "urgency": r.get("urgency", "")}
        item.pop("detail", None)
        kept.append(item)
    kept.sort(key=lambda r: r["rank"])

    later = [{"uid": d.get("uid") if d.get("uid") in by_uid else None,
              "title": d.get("title", ""), "why": d.get("why", "")}
             for d in answer.get("later", [])]
    if budget:
        under, total = [], 0
        for r in kept:
            if r["tier"] == "must_today" or total + r["suggested_minutes"] <= budget:
                under.append(r)
                total += r["suggested_minutes"]
            else:
                later.append({"uid": r["uid"], "title": f"{r['title']} ({r['course']})",
                              "why": r["urgency"] or "did not fit in today's cap"})
        kept = under

    shown = {r["uid"] for r in kept} | {d["uid"] for d in later if d["uid"]}
    text = " ".join(f"{d['title']} {d['why']}" for d in later)
    surfaced = []
    for c in sorted(cands, key=lambda c: nearest_days(c) if nearest_days(c) is not None else 99):
        d = nearest_days(c)
        if d is None or d > IMMINENT_DAYS or c["uid"] in shown or c["title"] in text:
            continue
        when = ("overdue" if d < 0 else "due today" if d == 0 else
                "due tomorrow" if d == 1 else f"due in {d} days")
        later.append({"uid": c["uid"], "title": f"{c['title']} ({c['course']})",
                      "why": f"{when}, {c['remaining']} min, {c['impact_pct']}% of "
                             f"grade. Small, but the deadline is here."})
        surfaced.append(c["uid"])
    return {"day_summary": answer.get("day_summary", ""), "ranked": kept,
            "later": later, "rejected": rejected, "surfaced_imminent": surfaced}


def run(cfg, day: date | None = None, model: str = "opus", timeout: int = 300,
        out=print) -> int:
    day = day or cfg.now().date()
    store = Store()
    previous = read_plan(day) or {}
    dismissed = set(previous.get("dismissed", []))

    calendars.refresh(max_age_minutes=15, timeout=10)
    busy = calendars.busy_by_day(cfg, day)
    free = free_minutes(cfg, day, busy.get(day))
    cands = candidates(cfg, day, store, dismissed, busy)
    if not cands:
        out(f"{fmt(day, '%A %b %-d')}: nothing open to plan.")
        write_plan(day, {"date": day.isoformat(), "generated_at": cfg.now().isoformat(),
                         "day_summary": "Nothing open to plan today.", "ranked": [],
                         "later": [], "dismissed": sorted(dismissed)})
        return 0
    budget = min(free, int(cfg.prefs.get("daily_focus_cap_minutes", 270))) or 60

    try:
        answer = agent.run_json(build_prompt(day, free, budget, cands, cfg.tz), SCHEMA,
                                model, timeout, which="plan")
    except agent.AgentError as e:
        out(f"PLANNING FAILED: {e}")
        return 1
    plan = validate(answer, cands, budget)
    plan = {"date": day.isoformat(), "generated_at": cfg.now().isoformat(),
            "model": agent.describe(model, "plan"), "free_minutes": free, "budget_minutes": budget,
            "candidates_shown": len(cands), **plan, "dismissed": sorted(dismissed)}
    write_plan(day, plan)

    total = sum(r["suggested_minutes"] for r in plan["ranked"])
    out(f"{fmt(day, '%A, %B %-d')}: {len(plan['ranked'])} items, {total//60}h {total%60:02d}m "
        f"suggested of a {budget}m cap ({len(cands)} candidates considered)")
    out(plan["day_summary"])
    for tier in TIERS:
        for r in (x for x in plan["ranked"] if x["tier"] == tier):
            out(f"  [{tier:<10}] {r['title'][:48]:<48} {r['course']:<8} "
                f"{r['suggested_minutes']:>3}m  due {r['due_label']}")
    for d in plan["later"]:
        out(f"  [later     ] {d['title'][:60]}")
    for r in plan["rejected"]:
        out(f"  dropped as ungrounded: {r}")
    out(f"wrote {plan_path(day)}")
    return 0
