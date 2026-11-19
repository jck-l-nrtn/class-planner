"""LearningSuite (and any) iCalendar feed source."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time, timedelta

import requests
from icalendar import Calendar

from ..config import DATA_DIR
from ..model import Task, Kind, Status
from .canvas import classify
from ..dates import fmt

CACHE = DATA_DIR / "cache"


DEFAULT_ACTIONABLE = [
    r"\bHW\s*\d",
    r"\bhomework\b",
    r"\bquiz\s*\d",
    r"\bexam\b", r"\btest\b", r"\bmidterm\b", r"\bfinal\b",
    r"\bskit\b",
    r"\bproject\b", r"\bpaper\b", r"\bessay\b",
    r"\bsentence check\b",
    r"\bassignment\b", r"\bsubmission\b", r"\bsubmit\b", r"\bdue\b",
    r"\bpresentation\b", r"\bportfolio\b",
    r"\bOPI\b", r"\bTOCFL\b", r"\bHSK\b",
    r"\bcultural activity\b",
]

DEFAULT_IGNORE = [
    r"^\s*\d{1,2}\s*/\s*\d{1,2}\s*$",
    r"^\s*\*",
    r"\bno\s*class\b",
    r"\boffice hour",
    r"\breview day\b",
    r"\bplease preview\b",
    r"\bself.?study\b",
]

CATEGORY_PATTERNS = [
    (re.compile(r"\bskit\b", re.I), "skit"),
    (re.compile(r"\bsentence check\b", re.I), "sentence check"),
    (re.compile(r"\b(OPI|TOCFL|HSK)\b", re.I), "proficiency"),
    (re.compile(r"\bcultural activity\b", re.I), "cultural activity"),
    (re.compile(r"\bHW\s*\d|\bhomework\b", re.I), "homework"),
    (re.compile(r"\bquiz\b", re.I), "quizzes"),
    (re.compile(r"\bexam\b|\bfinal\b|\bmidterm\b", re.I), "exams"),
]

_TITLE_TAIL = re.compile(r"\s*(online|location)\s*:.*$", re.I)

_MASHED = re.compile(r"(?<=[a-z\u4e00-\u9fff\u3000-\u303f])(?=[A-Z])")

_LEADING_NO_CLASS = re.compile(r"^\s*no\s*class\s*(?:\u4e0d\u4e0a\u8ab2)?\s*", re.I)


def clean_title(s: str) -> str:
    s = " ".join(s.split())
    s = _MASHED.sub(" ", s)
    s = _LEADING_NO_CLASS.sub("", s)
    s = _TITLE_TAIL.sub("", s)
    return s.strip(" -–—:;,")


def _dedupe_key(title: str) -> str:
    """Identity of an event ignoring language, punctuation and parentheticals."""
    t = re.sub(r"\([^)]*\)", " ", title)
    t = re.sub(r"[^\x00-\x7F]+", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t.lower())
    return " ".join(t.split())


def _window_key(title: str) -> str:
    """Stricter identity for multi-day collapsing."""
    t = re.sub(r"[^\x00-\x7F]+", " ", title)
    t = re.sub(r"[^a-z0-9]+", " ", t.lower())
    return " ".join(t.split())


def categorize(title: str, default: str) -> str:
    for rx, cat in CATEGORY_PATTERNS:
        if rx.search(title):
            return cat
    return default


def collapse_windows(tasks: list[Task]) -> tuple[list[Task], list[str]]:
    """Collapse an identical event repeated across consecutive days."""
    groups: dict[tuple, list[Task]] = {}
    for t in tasks:
        groups.setdefault((t.course_key, _window_key(t.title)), []).append(t)

    kept, collapsed = [], []
    for (_, spine), group in groups.items():
        dated = sorted([t for t in group if t.due_at], key=lambda t: t.due_at)
        if len(dated) < 2:
            kept.extend(group); continue

        run = [dated[0]]
        for prev, cur in zip(dated, dated[1:]):
            if (cur.due_at.date() - prev.due_at.date()).days <= 1:
                run.append(cur)
            else:
                kept.append(_finish_run(run, collapsed)); run = [cur]
        kept.append(_finish_run(run, collapsed))
    return kept, collapsed


def _finish_run(run: list[Task], collapsed: list[str]) -> Task:
    first = run[0]
    if len(run) > 1:
        last = run[-1]
        first.notes = (f"Open {fmt(first.due_at, '%b %-d')} through {fmt(last.due_at, '%b %-d')}. "
                       + first.notes)[:500]
        collapsed.append(f"{first.title[:44]} ({len(run)} days -> "
                         f"{fmt(first.due_at, '%b %-d')}..{fmt(last.due_at, '%b %-d')})")
    return first


def dedupe(tasks: list[Task]) -> tuple[list[Task], list[str]]:
    """Collapse same-course, same-day events with the same ASCII spine."""
    groups: dict[tuple, list[Task]] = {}
    for t in tasks:
        key = (t.course_key, t.due_at.date() if t.due_at else None,
               _dedupe_key(t.title))
        groups.setdefault(key, []).append(t)

    kept, merged = [], []
    for (_, _, spine), group in groups.items():
        if len(group) == 1:
            kept.append(group[0]); continue
        group.sort(key=lambda t: len(t.title))
        winner = group[0]
        longest_note = max((g.notes for g in group), key=len, default="")
        if len(longest_note) > len(winner.notes):
            winner.notes = longest_note
        kept.append(winner)
        merged.append(f"{winner.title}  (merged {len(group)} entries)")
    return kept, merged


def repair_mangled_utf8(raw: bytes) -> bytes:
    """Undo a 0xA0 -> 0x20 byte substitution seen in BYU LearningSuite feeds."""
    out = bytearray(raw)
    n = len(out)
    i = 0
    while i < n:
        b = out[i]
        if 0xE0 <= b <= 0xEF and i + 2 < n:
            b1, b2 = out[i + 1], out[i + 2]
            if b1 == 0x20 and 0x80 <= b2 <= 0xBF:
                out[i + 1] = 0xA0
            elif 0x80 <= b1 <= 0xBF and b2 == 0x20:
                out[i + 2] = 0xA0
            i += 3
            continue
        if 0xC2 <= b <= 0xDF and i + 1 < n:
            if out[i + 1] == 0x20:
                out[i + 1] = 0xA0
            i += 2
            continue
        i += 1
    return bytes(out)


def decode_feed(raw: bytes) -> str:
    """Decode a feed that may be byte-damaged, without ever raising."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    repaired = repair_mangled_utf8(raw)
    try:
        return repaired.decode("utf-8")
    except UnicodeDecodeError:
        return repaired.decode("utf-8", errors="replace")


FROM_CACHE: set[str] = set()


def _fetch_text(url: str, cache_key: str, timeout: int = 30) -> str:
    """Fetch with an on-disk fallback so a flaky feed can't blank your schedule."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"{cache_key}.ics"
    FROM_CACHE.discard(cache_key)
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        text = decode_feed(r.content)
        cached.write_text(text, encoding="utf-8")
        return text
    except Exception:
        if cached.exists():
            FROM_CACHE.add(cache_key)
            return cached.read_text(encoding="utf-8")
        raise


def _as_dt(value, tz) -> datetime | None:
    """ICS gives date or datetime; all-day items become end-of-day local."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)
    if isinstance(value, date):
        return datetime.combine(value, time(23, 59), tzinfo=tz)
    return None


def _filters(cfg, course_key: str):
    conf = (cfg.course(course_key).get("ls_filter")
            or cfg.course_defaults.get("ls_filter") or {})
    actionable = conf.get("actionable", DEFAULT_ACTIONABLE)
    ignore = conf.get("ignore", DEFAULT_IGNORE)
    return ([re.compile(p, re.I) for p in actionable],
            [re.compile(p, re.I) for p in ignore])


def parse(ics_text: str, course_key: str, cfg, source: str = "ls_ics",
          report: dict | None = None) -> list[Task]:
    cal = Calendar.from_ical(ics_text)
    actionable, ignore = _filters(cfg, course_key)
    tasks: list[Task] = []
    skipped: list[str] = []

    for comp in cal.walk("VEVENT"):
        summary = clean_title(str(comp.get("summary", "")))
        if not summary:
            continue
        if any(r.search(summary) for r in ignore):
            skipped.append(summary); continue
        if actionable and not any(r.search(summary) for r in actionable):
            skipped.append(summary); continue

        end = getattr(comp.get("dtend"), "dt", None)
        start = getattr(comp.get("dtstart"), "dt", None)
        if (end is not None and not isinstance(end, datetime)
                and start is not None and not isinstance(start, datetime)
                and end > start):
            end = end - timedelta(days=1)
        due = _as_dt(end, cfg.tz) or _as_dt(start, cfg.tz)
        if due is None:
            continue

        kind = classify(summary, "", [])
        category = categorize(summary, kind.value)
        raw_uid = str(comp.get("uid", "")) or hashlib.sha1(
            f"{course_key}{summary}{due.isoformat()}".encode()).hexdigest()[:16]

        tasks.append(Task(
            uid=f"{source}:{raw_uid}",
            source=source,
            course_key=course_key,
            title=summary,
            kind=kind,
            due_at=due,
            category=category,
            est_minutes=cfg.effort_for(course_key, category),
            status=Status.NOT_STARTED,
            url=str(comp.get("url", "")),
            notes=" ".join(str(comp.get("description", "")).split())[:500],
        ))

    tasks, merged = dedupe(tasks)
    tasks, collapsed = collapse_windows(tasks)
    if report is not None:
        report["kept"] = len(tasks)
        report["skipped"] = skipped
        report["merged"] = merged
        report["collapsed"] = collapsed
    return tasks


def fetch(cfg) -> tuple[list[Task], list[dict]]:
    """Pull every course that has an `ls_ics_url` configured."""
    tasks, seen = [], []
    for key, course in cfg.courses.items():
        url = cfg.expand(course.get("ls_ics_url"))
        if not url:
            continue
        try:
            text = _fetch_text(url, cache_key=key)
            found = parse(text, key, cfg)
            tasks.extend(found)
            seen.append({"course": key, "count": len(found), "ok": True})
        except Exception as e:
            seen.append({"course": key, "count": 0, "ok": False, "error": str(e)})
    return tasks, seen
