"""Canvas LMS source."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo

import requests

from ..model import Task, Kind, Status

UTC = ZoneInfo("UTC")

class CanvasError(RuntimeError):
    pass


class CanvasClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        if not base_url or not token:
            raise CanvasError(
                "CANVAS_BASE_URL and CANVAS_TOKEN must be set (run ./plan setup)."
            )
        self.base = base_url.rstrip("/").removesuffix("/api/v1")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout

    def _paged(self, path: str, **params):
        """Follow RFC-5988 Link headers so we never silently truncate results."""
        url = f"{self.base}/api/v1/{path.lstrip('/')}"
        params.setdefault("per_page", 100)
        while url:
            r = self.session.get(url, params=params, timeout=self.timeout)
            if r.status_code == 401:
                raise CanvasError("Canvas rejected the token (401). It has most likely "
                                  "expired: tokens stop working after a set time.")
            r.raise_for_status()
            body = r.json()
            yield from (body if isinstance(body, list) else [body])
            url = (r.links.get("next") or {}).get("url")
            params = {}

    def courses(self) -> list[dict]:
        out = []
        for c in self._paged("courses", enrollment_state="active",
                             include=["term", "total_scores"]):
            if c.get("access_restricted_by_date") or not c.get("id"):
                continue
            out.append(c)
        return out

    def token_expiry(self, token: str) -> str | None:
        try:
            for row in self._paged("users/self/user_generated_tokens"):
                shown = str(row.get("visible_token") or "").rstrip(".…*")
                if len(shown) >= 6 and token.startswith(shown):
                    return row.get("expires_at")
        except Exception:
            return None
        return None

    def assignments(self, course_id: int) -> list[dict]:
        return list(self._paged(f"courses/{course_id}/assignments",
                                include=["submission"], order_by="due_at"))

    def assignment_groups(self, course_id: int) -> list[dict]:
        return list(self._paged(f"courses/{course_id}/assignment_groups"))


def _parse_dt(s, tz):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz)


_PROJ_RE = re.compile(r"\b(projects?|capstone|portfolios?)\b", re.I)
_EXAM_RE = re.compile(r"\b(exams?|midterms?|finals?|tests?)\b", re.I)
_QUIZ_RE = re.compile(r"\b(quiz|quizzes|checkpoint)\b", re.I)
_HW_RE   = re.compile(r"\b(homework|hw|problem set|pass.?off|worksheet)\b", re.I)
_LAB_RE  = re.compile(r"\b(labs?|studio)\b", re.I)
_READ_RE = re.compile(r"\b(read|reading|chapter|textbook)\b", re.I)

_VAGUE_GROUP = re.compile(r"^\s*(other|assignments?|misc\w*|ungrouped)?\s*$", re.I)


def _match(text: str) -> Kind | None:
    """Highest-confidence category for a piece of text, or None."""
    if not text:
        return None
    if _PROJ_RE.search(text):
        return Kind.PROJECT
    if _QUIZ_RE.search(text):
        return Kind.QUIZ
    if _EXAM_RE.search(text):
        return Kind.EXAM
    if _HW_RE.search(text):
        return Kind.HOMEWORK
    if _LAB_RE.search(text):
        return Kind.PROJECT
    if _READ_RE.search(text):
        return Kind.READING
    return None


def classify(name: str, group: str, submission_types: list[str]) -> Kind:
    if not _VAGUE_GROUP.match(group or ""):
        by_group = _match(group)
        if by_group:
            return by_group
    if "online_quiz" in (submission_types or []):
        return Kind.QUIZ
    return _match(name) or Kind.HOMEWORK


def compute_impacts(course: dict, groups: list[dict],
                    assignments: list[dict]) -> dict[int, float]:
    """Exact fraction of the final grade for each assignment."""
    live = [a for a in assignments
            if not a.get("omit_from_final_grade") and a.get("published", True)]
    impacts: dict[int, float] = {a["id"]: 0.0 for a in assignments}

    if course.get("apply_assignment_group_weights"):
        by_group: dict[int, list[dict]] = {}
        for a in live:
            by_group.setdefault(a.get("assignment_group_id"), []).append(a)
        total_w = sum(float(g.get("group_weight") or 0) for g in groups) or 100.0
        for g in groups:
            members = by_group.get(g["id"], [])
            pts = sum(float(a.get("points_possible") or 0) for a in members)
            gw = float(g.get("group_weight") or 0) / total_w
            for a in members:
                p = float(a.get("points_possible") or 0)
                impacts[a["id"]] = gw * (p / pts) if pts else 0.0
    else:
        total = sum(float(a.get("points_possible") or 0) for a in live)
        for a in live:
            impacts[a["id"]] = (float(a.get("points_possible") or 0) / total) if total else 0.0
    return impacts


def _status(a: dict) -> Status:
    sub = a.get("submission") or {}
    state = sub.get("workflow_state")
    if sub.get("submitted_at") or state in ("submitted", "pending_review"):
        return Status.SUBMITTED
    if state == "graded" and sub.get("score") is not None:
        return Status.GRADED
    return Status.NOT_STARTED


def fetch(cfg) -> tuple[list[Task], list[dict]]:
    """Return (tasks, courses_seen)."""
    client = CanvasClient(cfg.env("CANVAS_BASE_URL"), cfg.env("CANVAS_TOKEN"))
    tasks: list[Task] = []
    seen: list[dict] = []

    for course in client.courses():
        cid = course["id"]
        key = cfg.canvas_map.get(cid)
        seen.append({"canvas_id": cid,
                     "name": course.get("name", ""),
                     "code": course.get("course_code", ""),
                     "mapped_to": key})
        if not key:
            continue

        groups = client.assignment_groups(cid)
        group_names = {g["id"]: g.get("name", "") for g in groups}
        assignments = client.assignments(cid)
        impacts = compute_impacts(course, groups, assignments)

        for a in assignments:
            group = group_names.get(a.get("assignment_group_id"), "")
            kind = classify(a.get("name", ""), group, a.get("submission_types", []))
            due = _parse_dt(a.get("due_at"), cfg.tz)
            lock = _parse_dt(a.get("lock_at"), cfg.tz)

            category = group or kind.value
            tasks.append(Task(
                uid=f"canvas:{a['id']}",
                source="canvas",
                course_key=key,
                title=a.get("name", "(untitled)"),
                kind=kind,
                due_at=due,
                available_from=_parse_dt(a.get("unlock_at"), cfg.tz),
                points=float(a.get("points_possible") or 0),
                category=category,
                est_minutes=cfg.effort_for(key, category),
                status=_status(a),
                hard_deadline=bool(lock and due and lock <= due),
                group_work=bool(a.get("group_category_id")),
                url=a.get("html_url", ""),
                impact_override=impacts.get(a["id"]),
                weight_source="canvas",
                score=(a.get("submission") or {}).get("score"),
            ))
    return tasks, seen
