"""LearningSuite gradebook source: dump files -> Tasks."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR
from ..ls.parse import _clean
from ..model import Task, Status
from .canvas import classify

DUMP_DIR = DATA_DIR / "learningsuite"

ATTENDANCE_RE = re.compile(
    r"attendance|participation|iclicker|clicker|in.?class", re.I)

GRADED_WORK_RE = re.compile(
    r"self.?eval|evaluation|reflection|essay|paper|report|journal|"
    r"quiz|exam|assignment|project|homework", re.I)


def _is_attendance(cat_title: str) -> bool:
    """True only for credit earned by being somewhere, not by doing work."""
    t = cat_title or ""
    return bool(ATTENDANCE_RE.search(t)) and not GRADED_WORK_RE.search(t)


def _parse_dt(s: str | None, tz) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return None


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def impacts(dump: dict) -> dict[str, float]:
    """Exact fraction of the final course grade riding on each assignment."""
    cats = {c["id"]: c for c in dump.get("categories", [])}
    assigns = dump.get("assignments", [])

    def counts(a: dict) -> bool:
        c = cats.get(a.get("categoryID")) or {}
        return (bool(c.get("graded")) and not c.get("extraCredit")
                and not a.get("extraCredit")
                and a.get("graded", True) is not False
                and a.get("includeScoreInFinalGrade", True) is not False)

    live = [a for a in assigns if counts(a)]
    out = {a["id"]: 0.0 for a in assigns}

    if not dump.get("useCategoryWeights", True):
        total = sum(_f(a.get("points")) for a in live)
        for a in live:
            out[a["id"]] = _f(a.get("points")) / total if total else 0.0
        return out

    base = sum(_f(c.get("weight")) for c in cats.values()
               if c.get("graded") and not c.get("extraCredit"))
    by_cat: dict[str, list[dict]] = {}
    for a in live:
        by_cat.setdefault(a["categoryID"], []).append(a)
    for cid, members in by_cat.items():
        c = cats[cid]
        cat_share = _f(c.get("weight")) / base if base else 0.0
        if c.get("equalAssignWeight"):
            shares = {a["id"]: 1.0 / len(members) for a in members}
        else:
            key = "weight" if sum(_f(a.get("weight")) for a in members) else "points"
            tot = sum(_f(a.get(key)) for a in members)
            shares = {a["id"]: (_f(a.get(key)) / tot if tot else 0.0) for a in members}
        for a in members:
            out[a["id"]] = cat_share * shares[a["id"]]
    return out


def _status(a: dict) -> tuple[Status, str]:
    """(status, why) from what LearningSuite itself records."""
    if a.get("score") is not None:
        return Status.GRADED, "score"
    if a.get("excused"):
        return Status.DROPPED, "excused"
    if a.get("examStatus") == "complete":
        return Status.SUBMITTED, "exam_complete"
    if a.get("submittedAt"):
        return Status.SUBMITTED, "submission"
    return Status.NOT_STARTED, ""


def tasks_from_dump(dump: dict, course_key: str, cfg) -> list[Task]:
    cats = {c["id"]: c for c in dump.get("categories", [])}
    impact = impacts(dump)
    tasks: list[Task] = []
    for a in dump.get("assignments", []):
        cat = cats.get(a.get("categoryID")) or {}
        cat_title = _clean(cat.get("title") or a.get("category")) or "other"
        name = _clean(a.get("name"))
        if not name:
            continue
        status, why = _status(a)
        schedulable = not _is_attendance(cat_title)
        score = a.get("score")
        note = ""
        if a.get("selfCompleted") and status == Status.NOT_STARTED:
            note = "You ticked this complete in LearningSuite."
        tasks.append(Task(
            uid=f"ls:{a['id']}",
            source="learningsuite",
            course_key=course_key,
            title=name,
            kind=classify(name, cat_title, []),
            due_at=_parse_dt(a.get("dueDate"), cfg.tz),
            available_from=_parse_dt(a.get("beginDate"), cfg.tz),
            points=_f(a.get("points")),
            category=cat_title,
            est_minutes=(cfg.effort_for(course_key, cat_title) if schedulable else 0),
            schedulable=schedulable,
            status=status,
            done_via=why,
            score=float(score) if score is not None else None,
            hard_deadline=not a.get("allowLateSubmission"),
            url=cfg.ls_url(course_key),
            impact_override=impact.get(a["id"], 0.0),
            weight_source="gradebook",
            category_drops=int(cat.get("lowScoresToDrop") or 0),
            notes=note,
        ))
    return tasks


def newest_dumps() -> dict[str, tuple[Path, dict]]:
    """courseID -> (path, parsed dump), keeping only the newest file per course."""
    best: dict[str, tuple[Path, dict]] = {}
    if not DUMP_DIR.exists():
        return best
    for path in sorted(DUMP_DIR.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cid = d.get("courseID")
        prev = best.get(cid)
        if cid and (not prev or path.stat().st_mtime > prev[0].stat().st_mtime):
            best[cid] = (path, d)
    return best


def fetch(cfg) -> tuple[list[Task], list[dict]]:
    """Read every dump whose courseID maps to a configured course."""
    tasks, seen = [], []
    id_to_key = {cid: key for key, cid in cfg.ls_course_ids().items()}
    for cid, (path, dump) in sorted(newest_dumps().items()):
        key = id_to_key.get(cid)
        if not key:
            seen.append({"file": path.name, "courseID": cid, "ok": False,
                         "error": "no course in courses.yaml maps to this courseID"})
            continue
        try:
            found = tasks_from_dump(dump, key, cfg)
        except Exception as e:
            seen.append({"file": path.name, "course": key, "ok": False,
                         "error": f"{type(e).__name__}: {e}"})
            continue
        tasks.extend(found)
        seen.append({"file": path.name, "course": key, "count": len(found), "ok": True,
                     "extracted_at": dump.get("extractedAt"),
                     "schema": dump.get("schema", 1)})
    return tasks, seen
