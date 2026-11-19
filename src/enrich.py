from __future__ import annotations

from .content import ContentStore, TaskContent, html_to_text


def _safe(fn, default, *a, **kw):
    """Course content is a nice-to-have; never let one 403 abort the run."""
    try:
        return fn(*a, **kw)
    except Exception:
        return default


def enrich_canvas(cfg, content: ContentStore) -> dict:
    from .sources.canvas import CanvasClient

    client = CanvasClient(cfg.env("CANVAS_BASE_URL"), cfg.env("CANVAS_TOKEN"))
    report = {"assignments": 0, "changed": 0, "chars": 0,
              "graded": 0, "skipped": []}

    for course in client.courses():
        cid = course["id"]
        key = cfg.canvas_map.get(cid)
        if not key:
            continue

        subs_by_aid = {}
        for sub in _safe(lambda: list(client._paged(
                f"courses/{cid}/students/submissions",
                **{"student_ids[]": "self",
                   "include[]": ["submission_comments", "rubric_assessment"]})), []):
            subs_by_aid[sub.get("assignment_id")] = sub

        for a in client.assignments(cid):
            uid = f"canvas:{a['id']}"
            desc = html_to_text(a.get("description"))
            rubric = a.get("rubric") or []
            sub = subs_by_aid.get(a["id"], {})
            comments = [{"author": (cm.get("author") or {}).get("display_name", ""),
                         "text": cm.get("comment", "")}
                        for cm in (sub.get("submission_comments") or [])
                        if cm.get("comment")]
            c = TaskContent(uid=uid, description=desc, rubric=rubric, extra={
                "course": key,
                "name": a.get("name", ""),
                "submission_types": a.get("submission_types") or [],
                "allowed_extensions": a.get("allowed_extensions") or [],
                "points_possible": a.get("points_possible"),
                "html_url": a.get("html_url", ""),
                "score": sub.get("score"),
                "grade": sub.get("grade"),
                "late": bool(sub.get("late")),
                "missing": bool(sub.get("missing")),
                "workflow_state": sub.get("workflow_state"),
                "submitted_at": sub.get("submitted_at"),
                "feedback": comments,
                "rubric_assessment": sub.get("rubric_assessment") or {},
            })
            report["assignments"] += 1
            report["chars"] += c.chars
            if content.put_task(c):
                report["changed"] += 1
    return report


def enrich_learningsuite(cfg, content: ContentStore) -> dict:
    """Read assignment descriptions out of the LearningSuite dumps."""
    from .sources.ls_gradebook import newest_dumps

    report = {"assignments": 0, "changed": 0, "chars": 0, "missing_desc": 0}
    id_to_key = {cid: key for key, cid in cfg.ls_course_ids().items()}
    for cid, (_path, d) in sorted(newest_dumps().items()):
        key = id_to_key.get(cid)
        if not key:
            continue
        for a in d.get("assignments", []):
            desc = html_to_text(a.get("description"))
            if not desc:
                report["missing_desc"] += 1
            c = TaskContent(uid=f"ls:{a['id']}", description=desc, extra={
                "course": key, "name": a.get("name", ""),
                "points": a.get("points"),
                "onlineSubmission": a.get("onlineSubmission"),
            })
            report["assignments"] += 1
            report["chars"] += c.chars
            if content.put_task(c):
                report["changed"] += 1
    return report
