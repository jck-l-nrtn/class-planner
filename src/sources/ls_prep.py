"""Ungraded class preparation, derived from a LearningSuite schedule feed."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from ..model import Task, Kind, Status
from .ics_feed import _fetch_text, clean_title
from ..dates import fmt

from icalendar import Calendar

_COMPLETE = re.compile(r"\bcomplete\b", re.I)
_CONTINUED = re.compile(r"\.\.\.\s*continued|\bcontinued\b", re.I)

_TOPIC_NUM = re.compile(r"\btopic\s*#?\s*0*(\d{1,2})\b(?!\s*\d)", re.I)
_TOPIC_TITLE = re.compile(r"\btopic\s*#?\s*0*(\d{1,2})\s*[.:]\s*(.+)$", re.I)


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _default_url(course: dict, cfg) -> str:
    """A course's feed URL, or the standard one built from its course id."""
    explicit = cfg.expand(course.get("ls_ics_url"))
    if explicit:
        return explicit
    cid = course.get("ls_course_id")
    return (f"https://learningsuite.byu.edu/iCalFeed/ical.php?courseID={cid}"
            if cid else "")


def parse(text: str, course_key: str, cfg, spec: dict,
          today: date | None = None) -> list[Task]:
    """Group a feed's events into one prep task per topic."""
    today = today or cfg.now().date()
    match = str(spec.get("match", "topic overview")).lower()
    label = spec.get("label", "Topic Overview")

    cal = Calendar.from_ical(text)
    class_days: dict[int, set[date]] = {}
    complete_day: dict[int, date] = {}
    names: dict[int, str] = {}

    for ev in cal.walk("VEVENT"):
        summary = clean_title(str(ev.get("SUMMARY") or ""))
        if not summary:
            continue
        start = _as_date(getattr(ev.get("DTSTART"), "dt", None))
        if start is None:
            continue
        num = _TOPIC_NUM.search(summary)
        if not num:
            continue
        n = int(num.group(1))

        class_days.setdefault(n, set()).add(start)

        named = _TOPIC_TITLE.match(summary)
        if named and not _CONTINUED.search(named.group(2)) and match not in summary.lower():
            cleaned = named.group(2).strip(" .-–—")
            if cleaned and len(cleaned) > len(names.get(n, "")):
                names[n] = cleaned

        if match in summary.lower() and _COMPLETE.search(summary):
            complete_day[n] = max(complete_day.get(n, start), start)

    if not class_days:
        return []

    cid = cfg.ls_course_id(course_key) or ""
    link = spec.get("url") or (
        f"https://learningsuite.byu.edu/.x/cid-{cid}/student/pages" if cid else "")

    lead = int(spec.get("lead_days", 2))
    est = int(spec.get("est_minutes", 60))
    kind = Kind(spec.get("kind", "reading"))
    start_hhmm = str(spec.get("class_start", "23:59"))
    hh, mm = (int(x) for x in start_hhmm.split(":"))
    class_start = time(hh, mm)

    active = [n for n in sorted(class_days)
              if (complete_day.get(n) or max(class_days[n])) >= today]
    weight = float(spec.get("weight", 0.0))
    impact = (weight / len(active)) if active else 0.0

    tasks: list[Task] = []
    for n in sorted(class_days):
        days = sorted(class_days[n])
        done_day = complete_day.get(n) or days[-1]
        due_at = datetime.combine(done_day, class_start, tzinfo=cfg.tz)
        first_class = days[0]
        prep_day = first_class - timedelta(days=lead)
        prep_by = datetime.combine(prep_day, time(23, 59), tzinfo=cfg.tz)
        if prep_by > due_at:
            prep_by = due_at

        name = names.get(n, "")
        title = f"{label} {n}" + (f" — {name}" if name else "")

        expired = done_day < today
        note = (f"Topic {n} in class "
                f"{', '.join(fmt(d, '%b %-d') for d in days)}; "
                f"{label.lower()} due at the start of {fmt(done_day, '%b %-d')}")
        if expired:
            note += " — preparation window closed"

        tasks.append(Task(
            uid=f"lsprep:{course_key}-topic-{n}",
            source="ls_prep",
            course_key=course_key,
            title=title,
            kind=kind,
            due_at=due_at,
            prep_by=None if expired else prep_by,
            category=label,
            est_minutes=est,
            status=Status.DROPPED if expired else Status.NOT_STARTED,
            hard_deadline=True,
            schedulable=True,
            impact_override=0.0 if expired else impact,
            weight_source="proxy",
            url=link,
            notes=note,
        ))
    return tasks


def fetch(cfg) -> tuple[list[Task], list[dict]]:
    """Pull prep tasks for every course configured with `prep_tasks`."""
    tasks, seen = [], []
    for key, course in cfg.courses.items():
        spec = course.get("prep_tasks") or {}
        if not spec or spec.get("enabled") is False:
            continue
        url = _default_url(course, cfg)
        if not url:
            seen.append({"course": key, "count": 0, "ok": False,
                         "error": "no ls_course_id or ls_ics_url"})
            continue
        try:
            text = _fetch_text(url, cache_key=f"{key}-prep")
            found = parse(text, key, cfg, spec)
            tasks.extend(found)
            live = [t for t in found if not t.is_done]
            seen.append({"course": key, "count": len(live),
                         "closed": len(found) - len(live), "ok": True})
        except Exception as e:
            seen.append({"course": key, "count": 0, "ok": False, "error": str(e)})
    return tasks, seen
