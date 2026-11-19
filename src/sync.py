"""Pull every source and merge it into the store."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .model import Task
from .store import Store
from .dates import fmt


@dataclass
class SourceResult:
    name: str
    ok: bool
    tasks: list[Task] = field(default_factory=list)
    complete: set[tuple[str, str]] = field(default_factory=set)
    message: str = ""
    lines: list[str] = field(default_factory=list)
    fresh: bool = True
    extra: dict = field(default_factory=dict)


def _canvas(cfg) -> SourceResult:
    if not cfg.env("CANVAS_TOKEN"):
        return SourceResult("canvas", False, message="skipped (no CANVAS_TOKEN)")
    from .sources import canvas
    try:
        tasks, seen = canvas.fetch(cfg)
    except Exception as e:
        return SourceResult("canvas", False, message=f"{type(e).__name__}: {e}"[:160])
    mapped = [c for c in seen if c["mapped_to"]]
    unmapped = len(seen) - len(mapped)
    token = cfg.env("CANVAS_TOKEN")
    expires = canvas.CanvasClient(cfg.env("CANVAS_BASE_URL"), token).token_expiry(token)
    return SourceResult(
        "canvas", True, tasks, {("canvas", c["mapped_to"]) for c in mapped},
        f"{len(tasks)} tasks from {len(mapped)} mapped courses"
        + (f", {unmapped} unmapped" if unmapped else ""),
        extra={"token_expires": expires})


def _gradebook(cfg) -> SourceResult:
    if not cfg.ls_course_ids():
        return SourceResult("learningsuite", False,
                            message="skipped (no LearningSuite courses in courses.yaml)")
    from .sources import ls_gradebook
    tasks, seen = ls_gradebook.fetch(cfg)
    good = [s for s in seen if s["ok"]]
    lines = [f"{s['course']}: {s['count']} assignments" if s["ok"]
             else f"{s.get('course') or s['file']} SKIPPED: {s.get('error', '')[:90]}"
             for s in seen]
    return SourceResult("learningsuite", bool(good), tasks,
                        {("learningsuite", s["course"]) for s in good},
                        f"{len(tasks)} assignments from {len(good)} gradebook dumps"
                        if good else "nothing fetched from LearningSuite yet -- click Grades in "
                        "the planner (or run ./plan ls-refresh)", lines)


def _feeds(cfg, covered: set[str]) -> SourceResult:
    from .sources import ics_feed
    tasks, seen = ics_feed.fetch(cfg)
    kept = [t for t in tasks if t.course_key not in covered]
    good = [s for s in seen if s["ok"]]
    cached = {s["course"] for s in good if s["course"] in ics_feed.FROM_CACHE}
    lines = [f"{s['course']} feed FAILED: {s.get('error', '')[:80]}"
             for s in seen if not s["ok"]]
    lines += [f"{c}: feed unreachable, using the cached copy" for c in sorted(cached)]
    return SourceResult(
        "ls_ics", bool(good) or not seen, kept,
        {("ls_ics", s["course"]) for s in good if s["course"] not in cached},
        fresh=not cached, lines=lines, message=
        f"{len(kept)} items from {len(good)}/{len(seen)} feeds"
        + (f" ({len(tasks) - len(kept)} superseded by gradebook)"
           if len(tasks) != len(kept) else "")
        + (" -- FROM CACHE, feed unreachable" if cached else ""))


def _prep(cfg) -> SourceResult:
    from .sources import ls_prep, ics_feed
    tasks, seen = ls_prep.fetch(cfg)
    good = [s for s in seen if s["ok"]]
    cached = {s["course"] for s in good if f"{s['course']}-prep" in ics_feed.FROM_CACHE}
    lines = [f"{s['course']}: {s['count']} upcoming"
             + (f" ({s['closed']} past windows closed)" if s.get("closed") else "")
             if s["ok"] else f"{s['course']} SKIPPED: {s.get('error', '')[:80]}"
             for s in seen]
    lines += [f"{c}: feed unreachable, using the cached copy" for c in sorted(cached)]
    return SourceResult("ls_prep", bool(good) or not seen, tasks,
                        {("ls_prep", s["course"]) for s in good if s["course"] not in cached},
                        f"{sum(s['count'] for s in good)} upcoming prep tasks"
                        + (" -- FROM CACHE, feed unreachable" if cached else ""),
                        lines, fresh=not cached)


def _manual(cfg) -> SourceResult:
    from .sources import manual
    try:
        tasks = manual.fetch(cfg)
    except Exception as e:
        return SourceResult("manual", False, message=f"manual_assignments.yaml: "
                            f"{type(e).__name__}: {e}"[:160])
    return SourceResult("manual", True, tasks,
                        {("manual", t.course_key) for t in tasks},
                        f"{len(tasks)} hand-entered assignments")


def gather(cfg) -> list[SourceResult]:
    gb = _gradebook(cfg)
    covered = {c for _s, c in gb.complete}
    return [_canvas(cfg), gb, _feeds(cfg, covered), _prep(cfg), _manual(cfg)]


_FAMILY = [("pow", r"\bpow\b"), ("midterm", r"\bmidterm\b"), ("final", r"\bfinal\b"),
           ("ch", r"\b(?:ch|chapter)\b")]


def signature(title: str) -> tuple[str, str] | None:
    t = title.lower()
    for fam, rx in _FAMILY:
        m = re.search(rx, t)
        if m:
            num = re.search(r"\d+", t[m.end():])
            if fam in ("midterm", "final"):
                lead = re.search(r"(?:midterm|final)\s*(?:exam\s*)?#?\s*(\d+)\b", t)
                return fam, lead.group(1) if lead else ""
            return (fam, num.group(0)) if num else None
    return None


def _norm(title: str) -> str:
    t = title.lower().replace("chapter", "ch")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def same_work(placeholder: Task, real: Task) -> bool:
    if placeholder.course_key != real.course_key:
        return False
    if _norm(placeholder.title) == _norm(real.title):
        return True
    if "practice" in real.title.lower():
        return False
    sig = signature(placeholder.title)
    if not sig or sig != signature(real.title):
        return False
    if not placeholder.due_at or not real.due_at:
        return False
    return abs((placeholder.due_at - real.due_at).total_seconds()) <= 36 * 3600


def match_placeholders(manual: list[Task], real: list[Task]) -> dict[str, str]:
    """manual uid -> the gradebook uid that now represents the same work."""
    out = {}
    for m in manual:
        hits = [r for r in real if same_work(m, r)]
        if hits:
            out[m.uid] = max(hits, key=lambda r: r.impact_override or 0).uid
    return out


def merge(store: Store, results: list[SourceResult]) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in results:
        prev = store.sources.get(r.name, {})
        store.sources[r.name] = {
            "ok": r.ok and r.fresh, "at": now, "message": r.message,
            "last_ok": now if r.ok and r.fresh else prev.get("last_ok"),
            "lines": r.lines,
            "extra": r.extra or prev.get("extra") or {}}

    by = {r.name: r for r in results}
    incoming = [t for r in results if r.ok for t in r.tasks]
    complete = set().union(*(r.complete for r in results if r.ok)) if results else set()

    real = by["learningsuite"].tasks if by.get("learningsuite") and by["learningsuite"].ok else []
    manual = by["manual"].tasks if by.get("manual") and by["manual"].ok else []
    pairs = match_placeholders(manual, real)
    incoming = [t for t in incoming if t.uid not in pairs or t.uid in store.tasks]

    summary = store.upsert(incoming, complete)

    carried = []
    for old_uid, new_uid in pairs.items():
        if old_uid in store.tasks:
            title = store.tasks[old_uid].title
            if store.supersede(old_uid, new_uid):
                carried.append((title, store.tasks[new_uid].title))
        elif old_uid not in store.aliases and (
                old_uid in store.overrides or old_uid in store.estimates):
            ov = store.overrides.pop(old_uid, {})
            if ov:
                store.overrides[new_uid] = {**ov, **store.overrides.get(new_uid, {})}
            est = store.estimates.pop(old_uid, None)
            if est and new_uid not in store.estimates:
                store.estimates[new_uid] = est
            store.aliases[old_uid] = new_uid
            store.event(new_uid, "rehomed_orphan_override", old_uid, new_uid)
    if pairs:
        store.apply_overrides()
        summary["missing"] = [u for u in summary["missing"] if u in store.tasks]
    summary["superseded"] = carried
    summary["placeholders_matched"] = len(pairs)
    return summary


def run(cfg, origin: str = "sync", out=print) -> tuple[int, dict]:
    results = gather(cfg)
    with Store.edit(origin) as store:
        summary = merge(store, results)
        open_n = len(store.open_tasks())
        titles = {u: store.tasks[u].title for u in summary["missing"]}

    label = {"canvas": "Canvas", "learningsuite": "LS gradebook",
             "ls_ics": "LS iCal feeds", "ls_prep": "class prep", "manual": "manual"}
    failed = 0
    for r in results:
        skipped = r.message.startswith("skipped")
        mark = ("OLD " if r.ok and not r.fresh else "ok  " if r.ok
                else "--  " if skipped else "FAIL")
        failed += ((not r.ok or not r.fresh) and not skipped)
        out(f"{mark} {label[r.name]}: {r.message}")
        for line in r.lines:
            out(f"       {line}")
    if failed:
        out(f"\n{failed} source(s) FAILED or served old data. Their tasks were left exactly as they "
            f"were; nothing was marked missing because of the failure.")
    out(f"\nStore: +{summary['added']} new, {summary['updated']} updated, {open_n} open")
    for title, old, new in summary["changed_due"]:
        o = f"{fmt(old, '%b %-d %H:%M')}" if old else "none"
        out(f"  DUE DATE CHANGED  {title[:44]}: {o} -> {fmt(new, '%b %-d %H:%M')}")
    for title, course, score, pts in summary["newly_graded"]:
        out(f"  GRADED  {course} {title[:40]}: {score:g}/{pts:g}")
    for title, course, why in summary["closed_by_source"]:
        out(f"  DONE    {course} {title[:40]} ({why.replace('_', ' ')})")
    for t in summary["new_tasks"][:12]:
        due = f"due {fmt(t.due_at, '%b %-d')}" if t.due_at else "no due date"
        out(f"  NEW     {t.course_key} {t.title[:42]} ({due})")
    if len(summary["new_tasks"]) > 12:
        out(f"          ... and {len(summary['new_tasks']) - 12} more new tasks")
    for a, b in summary["superseded"]:
        out(f"  MERGED  placeholder '{a[:34]}' -> gradebook row '{b[:34]}'")
    if titles:
        out(f"  {len(titles)} open task(s) are no longer listed by their source "
            f"(resolve under Needs attention in the app):")
        for t in list(titles.values())[:8]:
            out(f"    - {t[:60]}")
    return (1 if failed else 0), summary
