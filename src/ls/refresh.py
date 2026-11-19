"""Refresh every LearningSuite course into data/learningsuite/ls-<id>.json."""
from __future__ import annotations

import contextlib
import json
import sys
import time
from dataclasses import dataclass, asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from ..config import DATA_DIR, write_atomic
from . import parse, power, session as sess_mod
from .parse import PageChanged, PageState
from ..sources.ls_gradebook import DUMP_DIR

STATUS_FILE = DUMP_DIR / "status.json"
OPTIONAL = ("exam", "calendar")
NET_WAIT = 90
WAKE_WAIT_MINUTES = 240
RECENT = 300
TRANSIENT = ("offline", "asleep", "http_error")
FIX = {
    "offline": "no network; will retry on the next run",
    "asleep": "nothing to do; it retries on the next run, or use Grade refresh "
              "in the app",
    "logged_out": "click Sign in to LearningSuite in the planner (or run ./plan ls-login)",
    "page_changed": "LearningSuite changed its page; src/ls/parse.py needs updating",
    "http_error": "LearningSuite returned an error; retry later",
    "rejected": "kept the previous dump; inspect the course page by hand",
}


@dataclass
class CourseResult:
    course: str
    state: str
    detail: str = ""
    assignments: int = 0
    scores: int = 0
    exams_complete: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state in ("ok", "empty_course")

    def line(self) -> str:
        if self.state == "ok":
            s = (f"  ok    {self.course}: {self.assignments} assignments, "
                 f"{self.scores} scored, {self.exams_complete} taken online")
        elif self.state == "empty_course":
            s = f"  ok    {self.course}: gradebook is valid and lists no assignments"
        else:
            s = (f"  FAIL  {self.course}: {self.state} -- {self.detail} "
                 f"[{FIX.get(self.state, '')}]")
        return s + "".join(f"\n        warning: {w}" for w in self.warnings)


def _atomic_write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(obj, indent=1, ensure_ascii=False))


def _previous(course_id: str) -> dict | None:
    try:
        return json.loads((DUMP_DIR / f"ls-{course_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build(course: str, course_id: str, pages: dict[str, tuple[PageState, str]],
          now: str, previous: dict | None) -> tuple[CourseResult, dict | None]:
    """Pure: page responses in, (result, dump-to-write or None) out."""
    state, text = pages["gradebook"]
    if state.kind != "ok":
        return CourseResult(course, state.kind, state.detail), None
    try:
        gb = parse.parse_gradebook(text)
    except PageChanged as e:
        return CourseResult(course, "page_changed", str(e)), None

    warnings, extra = [], {}
    for pg, fn in (("exam", parse.parse_exams), ("calendar", parse.parse_schedule)):
        st, body = pages.get(pg, (PageState("skipped"), ""))
        if st.kind == "ok":
            try:
                extra[pg] = fn(body)
                continue
            except PageChanged as e:
                st = PageState("page_changed", str(e))
        extra[pg] = None
        if st.kind != "skipped":
            warnings.append(f"{pg} page not read ({st.kind}: {st.detail})")

    dump = parse.build_dump(course_id, now, gb, extra["exam"], extra["calendar"])
    if previous and previous.get("schema") == parse.SCHEMA:
        if extra["exam"] is None:
            old = {a["id"]: a for a in previous.get("assignments", [])}
            for a in dump["assignments"]:
                a["examStatus"] = (old.get(a["id"]) or {}).get("examStatus")
        if extra["calendar"] is None:
            dump["schedule"] = previous.get("schedule")

    n = len(dump["assignments"])
    was = len((previous or {}).get("assignments", []))
    if was >= 10 and n < was * 0.5:
        return CourseResult(course, "rejected",
                            f"gradebook lists {n} assignments, the last good dump "
                            f"had {was}"), None
    res = CourseResult(
        course, "ok" if n else "empty_course", assignments=n,
        scores=sum(1 for a in dump["assignments"] if a["score"] is not None),
        exams_complete=sum(1 for a in dump["assignments"]
                           if a.get("examStatus") == "complete"),
        warnings=tuple(warnings))
    return res, dump


def _record(results: list[CourseResult], now: str) -> None:
    status = read_status()
    for r in results:
        prev = status.get(r.course, {})
        status[r.course] = {**asdict(r), "at": now,
                            "last_ok": now if r.ok else prev.get("last_ok")}
    _atomic_write(STATUS_FILE, status)


def read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {}


@dataclass
class _Fail:
    state: str
    detail: str
    retry: bool = False
    definite: bool = False


def _fetch(cfg, ids, allow_browser: bool, interactive: bool, via_browser: bool,
           log, session=None, linger: int = 0) -> tuple[dict | None, _Fail | None]:
    """One attempt at reading every course's pages: (pages, None) or (None, why)."""
    keep = bool((cfg.prefs.get("learningsuite") or {}).get("save_session"))
    fetched: dict[str, dict[str, tuple[PageState, str]]] = {}
    if via_browser:
        from . import browser
        try:
            raw = browser.fetch_html(ids, ["gradebook", *OPTIONAL], interactive)
        except browser.LoginFailed as e:
            return None, _Fail(e.kind, str(e), e.retry, e.definite)
        for (key, pg), (url, text) in raw.items():
            fetched.setdefault(key, {})[pg] = (parse.classify(200, url, text, pg), text)
        log("  transport: browser (fallback)")
        return fetched, None

    first = next(iter(ids.values()))
    s = session or (sess_mod.load() if keep and not interactive else None)
    how = "session in hand" if session else "saved session"
    state = (sess_mod.get_page(s, first, "gradebook")[0] if s
             else PageState("logged_out", "no saved session"))
    if state.kind == "offline":
        return None, _Fail("offline", state.detail, retry=True)
    if state.kind == "logged_out":
        if not allow_browser:
            return None, _Fail("logged_out", "no live session, and this run may not "
                               "open a browser", definite=True)
        if not sess_mod.online():
            return None, _Fail("offline", "could not reach learningsuite.byu.edu",
                               retry=True)
        from . import browser
        try:
            cookies, ua = browser.login(interactive=interactive, linger=linger)
        except browser.LoginFailed as e:
            return None, _Fail(e.kind, str(e), e.retry, e.definite)
        s = sess_mod.new_session(cookies, ua)
        how = "after browser sign-in"
    log(f"  transport: plain HTTP ({how})")
    for key, cid in ids.items():
        fetched[key] = {"gradebook": sess_mod.get_page(s, cid, "gradebook")}
        if fetched[key]["gradebook"][0].kind == "ok":
            for pg in OPTIONAL:
                fetched[key][pg] = sess_mod.get_page(s, cid, pg)
    if keep and any(p["gradebook"][0].kind == "ok" for p in fetched.values()):
        sess_mod.save(s)
    return fetched, None


@contextlib.contextmanager
def _exclusive():
    from ..store import _lock, _unlock
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with open(DUMP_DIR / ".refresh.lock", "a+") as f:
        _lock(f)
        try:
            yield time.time() - t0 > 1.0
        finally:
            _unlock(f)


def _just_done(ids) -> list[CourseResult] | None:
    """The results of a refresh that succeeded for every course moments ago."""
    status, names = read_status(), {f.name for f in fields(CourseResult)}
    out = []
    for key in ids:
        st = status.get(key) or {}
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(st["at"])
                   ).total_seconds()
        except (KeyError, ValueError, TypeError):
            return None
        if st.get("state") not in ("ok", "empty_course") or not 0 <= age <= RECENT:
            return None
        st = {k: v for k, v in st.items() if k in names}
        out.append(CourseResult(**{**st, "warnings": tuple(st.get("warnings") or ())}))
    return out


def _attempt(cfg, ids, allow_browser, interactive, via_browser, log, session=None,
             linger: int = 0):
    with _exclusive() as queued:
        if queued and not interactive and (done := _just_done(ids)):
            log("  another run refreshed LearningSuite a moment ago; using that")
            return done, None
        with power.keep_awake():
            fetched, fail = _fetch(cfg, ids, allow_browser, interactive, via_browser,
                                   log, session, linger)
            if (fail and fail.state == "offline" and not interactive
                    and power.wait_until(sess_mod.online, NET_WAIT)):
                log("  the network came up; trying again")
                fetched, fail = _fetch(cfg, ids, allow_browser, interactive,
                                       via_browser, log, linger=linger)
        if fail:
            return None, fail
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        results = []
        for key, cid in ids.items():
            res, dump = build(key, cid, fetched[key], now, _previous(cid))
            if dump is not None:
                _atomic_write(DUMP_DIR / f"ls-{cid}.json", dump)
            results.append(res)
        _record(results, now)
        return results, None


def refresh(cfg, allow_browser: bool = True, interactive: bool = False,
            via_browser: bool = False, log=print, session=None,
            linger: int = 0) -> list[CourseResult]:
    ids = cfg.ls_course_ids()
    if not ids:
        log("No LearningSuite course ids in config/courses.yaml.")
        return []
    args = (cfg, ids, allow_browser, interactive, via_browser, log)
    results, fail = _attempt(*args, session=session, linger=linger)

    if fail and fail.retry and not interactive and not power.full_wake():
        prefs = cfg.prefs.get("learningsuite") or {}
        minutes = float(prefs.get("wait_for_wake_minutes", WAKE_WAIT_MINUTES))
        log(f"  the Mac is asleep ({fail.state}: {fail.detail[:80]})")
        if minutes > 0:
            log(f"  waiting up to {minutes:g} min for the lid to open")
        if minutes > 0 and power.wait_for_wake(minutes * 60):
            log("  awake; trying again")
            results, fail = _attempt(*args)
        elif not fail.definite:
            fail = _Fail("asleep", "the Mac was asleep with the lid closed, so "
                         "sign-in could not finish")
    if fail:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        results = [CourseResult(k, fail.state, fail.detail) for k in ids]
        _record(results, now)
    return results


def main(argv=None) -> int:
    import argparse
    from ..config import Config
    ap = argparse.ArgumentParser(prog="plan ls-refresh")
    ap.add_argument("--login", action="store_true",
                    help="sign in by hand in a visible browser")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--via-browser", action="store_true",
                    help="fallback transport: read pages through the browser")
    a = ap.parse_args(argv)
    results = refresh(Config(), allow_browser=not a.no_browser, interactive=a.login,
                      via_browser=a.via_browser)
    for r in results:
        print(r.line(), file=sys.stdout if r.ok else sys.stderr)
    return exit_code(results)


def exit_code(results: list[CourseResult]) -> int:
    bad = [r for r in results if not r.ok]
    if results and not bad:
        return 0
    return 75 if bad and all(r.state in TRANSIENT for r in bad) else 1


if __name__ == "__main__":
    sys.exit(main())
