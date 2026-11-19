"""Turn raw LearningSuite HTML into validated data."""
from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass

SCHEMA = 2


class PageChanged(Exception):
    """The page loaded, but is not shaped the way this parser expects."""


@dataclass
class PageState:
    kind: str
    detail: str = ""
    next_path: str = ""

_LOGIN_HOST = re.compile(r"https://(?:login|cas|id)\.byu\.edu/|/oauth2/|duosecurity\.com", re.I)
_LOADER = re.compile(r'location\.href\s*=\s*"((?:\\.|[^"\\])+)"')
MARKERS = {
    "gradebook": "studentAssignmentsData",
    "exam": "app/drivers/student/exam/",
    "calendar": "eventArray",
    "top": "initialCourseGroups",
}


def classify(status: int, url: str, text: str, page: str) -> PageState:
    """Say what a response IS before anyone tries to parse it."""
    if _LOGIN_HOST.search(url) or (status in (301, 302, 303) and not text):
        return PageState("logged_out", f"redirected to {url.split('?')[0]}")
    if status >= 400:
        return PageState("http_error", f"HTTP {status} from {url.split('?')[0]}")
    if "learningsuite.byu.edu" not in url:
        return PageState("page_changed", f"unexpected host: {url.split('?')[0]}")
    if MARKERS[page] in text:
        return PageState("ok")
    if len(text) < 8000:
        m = _LOADER.search(text)
        if m:
            return PageState("loader", next_path=json.loads(f'"{m.group(1)}"'))
    if "okta" in text.lower() and "sign" in text.lower():
        return PageState("logged_out", "login form served on a LearningSuite URL")
    return PageState(
        "page_changed",
        f"LearningSuite served a {len(text):,}-byte page for '{page}' with no "
        f"'{MARKERS[page]}' in it -- the page layout has probably changed")


def _balanced(text: str, i: int) -> str:
    """The {...} or [...] literal starting at text[i]."""
    depth, in_str, esc = 0, False, False
    for j in range(i, len(text)):
        c = text[j]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif in_str:
            continue
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    raise PageChanged("unterminated literal in inline script")


def js_const(text: str, name: str):
    """Value of `const|var|let NAME = <json>` (None if not declared)."""
    m = re.search(rf"(?:const|var|let)\s+{re.escape(name)}\s*=\s*(?=[\[{{])", text)
    if not m:
        return None
    raw = _balanced(text, m.end())
    try:
        return json.loads(raw)
    except ValueError:
        return _bare_key_object(raw, name)


_KEY = re.compile(r"\s*,?\s*([A-Za-z_$][\w$]*)\s*:\s*")
_SCALAR = re.compile(r'"(?:\\.|[^"\\])*"|[^,\n}]+')


def _bare_key_object(raw: str, name: str) -> dict:
    """Parse `{ key: <json>, key2: <json> }` -- bare keys, JSON values."""
    if not raw.startswith("{"):
        raise PageChanged(f"`{name}` is neither JSON nor an object literal")
    out, i = {}, 1
    while True:
        m = _KEY.match(raw, i)
        if not m:
            break
        j = m.end()
        piece = _balanced(raw, j) if raw[j] in "{[" else _SCALAR.match(raw, j).group(0)
        try:
            out[m.group(1)] = json.loads(piece.strip())
        except ValueError as e:
            raise PageChanged(f"`{name}.{m.group(1)}` is not valid JSON: {e}") from None
        i = j + len(piece)
    if raw[i:].strip(" \t\r\n,") != "}":
        raise PageChanged(f"`{name}`: could not parse past {raw[i:i+40]!r}")
    return out


def _require(obj, keys: tuple[str, ...], where: str):
    if not isinstance(obj, dict):
        raise PageChanged(f"{where}: expected an object, got {type(obj).__name__}")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise PageChanged(f"{where}: missing {missing} (has {sorted(obj)[:12]}...)")


def _rows(obj, key: str, row_keys: tuple[str, ...], where: str) -> list[dict]:
    rows = obj.get(key)
    if not isinstance(rows, list):
        raise PageChanged(f"{where}.{key}: expected a list, got {type(rows).__name__}")
    for r in rows[:1] + rows[-1:]:
        _require(r, row_keys, f"{where}.{key}[]")
    return rows


def _clean(s) -> str:
    return _html.unescape(s or "").strip()


def html_text(s) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def parse_gradebook(text: str) -> dict:
    d = js_const(text, "studentAssignmentsData")
    if d is None:
        raise PageChanged("no `studentAssignmentsData` declaration on the gradebook page")
    _require(d, ("categories", "assignments", "scores"), "studentAssignmentsData")
    cats = _rows(d, "categories", ("id", "title", "weight", "graded"), "gradebook")
    assigns = _rows(d, "assignments",
                    ("id", "gbAssignmentID", "categoryID", "name", "dueDate", "points"),
                    "gradebook")
    scores = _rows(d, "scores", ("assignmentID", "score"), "gradebook")
    subs = d.get("submissions") if isinstance(d.get("submissions"), list) else []
    done = d.get("completed") if isinstance(d.get("completed"), list) else []

    score_by_gb = {s["assignmentID"]: s for s in scores if not s.get("deleted")}
    sub_by_gb = {s.get("assignmentID"): s for s in subs}
    self_done = {c.get("assignmentID") for c in done if c.get("completed")}

    cat_ids = {c["id"] for c in cats}
    out_assign = []
    for a in assigns:
        if a.get("deleted"):
            continue
        s = score_by_gb.get(a["gbAssignmentID"]) or {}
        rec = dict(a)
        rec["name"] = _clean(a["name"])
        rec["score"] = s.get("score")
        rec["excused"] = bool(s.get("excuse"))
        rec["scoredAt"] = s.get("modifyTime")
        rec["submittedAt"] = (sub_by_gb.get(a["gbAssignmentID"]) or {}).get("modifyTime")
        rec["selfCompleted"] = a["id"] in self_done
        out_assign.append(rec)

    orphans = [a["name"] for a in out_assign if a["categoryID"] not in cat_ids]
    if orphans and len(orphans) == len(out_assign):
        raise PageChanged("no assignment's categoryID matches any category id")
    joined = sum(1 for a in out_assign if a["gbAssignmentID"] in score_by_gb)
    live_scores = len(score_by_gb)
    if live_scores and joined == 0:
        raise PageChanged(f"{live_scores} score rows but none joined to an assignment "
                          f"on gbAssignmentID -- the id scheme has changed")

    return {
        "useCategoryWeights": bool(d.get("useCategoryWeights")),
        "categories": [{**c, "title": _clean(c["title"])} for c in cats],
        "assignments": out_assign,
        "gradeScale": d.get("gradeScale"),
        "counts": {"assignments": len(out_assign), "scoreRows": live_scores,
                   "scoresJoined": joined, "submissions": len(sub_by_gb)},
    }


def parse_exams(text: str) -> dict:
    """Online quizzes/exams and whether the student has finished each one."""
    args = js_const(text, "args")
    if args is None:
        raise PageChanged("no `args` declaration on the exam page")
    rows = _rows(args, "exams", ("id", "title", "status"), "exam")
    return {r["id"]: {"status": r["status"], "timesTaken": r.get("timesTaken") or 0,
                      "scoringComplete": bool(r.get("isScoringComplete")),
                      "title": _clean(r["title"])} for r in rows}


def parse_schedule(text: str) -> dict:
    events = js_const(text, "eventArray")
    if events is None:
        raise PageChanged("no `eventArray` declaration on the schedule page")
    if not isinstance(events, list):
        raise PageChanged("schedule `eventArray` is not a list")
    out = []
    for e in events:
        _require(e, ("serverDate", "name"), "schedule.eventArray[]")
        t = html_text(e["name"])
        if t:
            out.append({"date": e["serverDate"], "column": e.get("column") or "",
                        "text": t})
    out.sort(key=lambda e: (e["date"], e["column"]))
    return {"events": out, "columns": sorted({e["column"] for e in out})}


def parse_course_list(text: str) -> list[dict]:
    m = re.search(r"initialCourseGroups\s*=\s*(?=\{)", text)
    if not m:
        raise PageChanged("no `initialCourseGroups` on the course-list page")
    try:
        groups = json.loads(_balanced(text, m.end()))["data"]["courseGroups"]
    except (ValueError, KeyError, TypeError) as e:
        raise PageChanged(f"`initialCourseGroups` is not shaped as expected: {e}") from None
    out = []
    for g in groups:
        for c in g.get("courseList", []):
            cid = re.search(r"cid-([A-Za-z0-9_-]+)", c.get("href") or "")
            if cid:
                out.append({"id": cid.group(1), "title": _clean(c.get("courseTitle")),
                            "term": g.get("periodName", "")})
    return out


def build_dump(course_id: str, fetched_at: str, gradebook: dict,
               exams: dict | None, schedule: dict | None) -> dict:
    """One self-describing file per course. Carries no student identifiers."""
    drop = ("studentID", "ownerID")
    assigns = []
    for a in gradebook["assignments"]:
        rec = {k: v for k, v in a.items() if k not in drop}
        ex = (exams or {}).get(a.get("typeID")) if a.get("typeID") else None
        rec["examStatus"] = ex["status"] if ex else None
        rec["examTimesTaken"] = ex["timesTaken"] if ex else None
        assigns.append(rec)
    counts = dict(gradebook["counts"])
    counts["exams"] = len(exams) if exams is not None else None
    counts["examsJoined"] = sum(1 for a in assigns if a["examStatus"])
    counts["scheduleEvents"] = len(schedule["events"]) if schedule else None
    return {
        "schema": SCHEMA, "courseID": course_id, "extractedAt": fetched_at,
        "useCategoryWeights": gradebook["useCategoryWeights"],
        "categories": gradebook["categories"], "assignments": assigns,
        "gradeScale": gradebook.get("gradeScale"),
        "schedule": schedule, "counts": counts,
        "pages": {"gradebook": True, "exam": exams is not None,
                  "calendar": schedule is not None},
    }
