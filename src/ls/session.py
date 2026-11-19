"""An authenticated LearningSuite HTTP session, with no browser on the normal path."""
from __future__ import annotations

import json
import time

import requests

from ..config import DATA_DIR, write_atomic
from .parse import PageState, classify

BASE = "https://learningsuite.byu.edu"
SESSION_FILE = DATA_DIR / ".ls-session.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_KEEP = ("learningsuite.byu.edu", "login.byu.edu", "cas.byu.edu")


def new_session(cookies: list[dict] | None = None, ua: str = UA) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = ua
    for c in cookies or []:
        if any(c["domain"].lstrip(".").endswith(h) for h in _KEEP):
            s.cookies.set(c["name"], c["value"], domain=c["domain"],
                          path=c.get("path", "/"))
    return s


def load() -> requests.Session | None:
    try:
        raw = json.loads(SESSION_FILE.read_text())
        return new_session(raw["cookies"], raw.get("ua") or UA)
    except (OSError, ValueError, KeyError):
        return None


def save(s: requests.Session) -> None:
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
               for c in s.cookies
               if any(c.domain.lstrip(".").endswith(h) for h in _KEEP)]
    write_atomic(SESSION_FILE, json.dumps({"saved_at": time.time(), "ua": s.headers.get("User-Agent"),
                                           "cookies": cookies}), mode=0o600)


def _get(s: requests.Session, url: str, page: str, timeout: int) -> tuple[PageState, str]:
    """Fetch one student page and say what came back."""
    try:
        for _ in range(3):
            r = s.get(url, timeout=timeout)
            state = classify(r.status_code, r.url, r.text, page)
            if state.kind != "loader":
                return state, r.text
            url = BASE + state.next_path
        return PageState("page_changed", "loader page kept redirecting"), ""
    except (requests.ConnectionError, requests.Timeout) as e:
        return PageState("offline", f"{type(e).__name__}: could not reach "
                                    f"learningsuite.byu.edu"), ""
    except requests.RequestException as e:
        return PageState("http_error", f"{type(e).__name__}: {e}"[:160]), ""


def get_page(s: requests.Session, course_id: str, page: str,
             timeout: int = 40) -> tuple[PageState, str]:
    """One course's gradebook, exam or calendar page."""
    return _get(s, f"{BASE}/.x/cid-{course_id}/student/{page}", page, timeout)


def get_course_list(s: requests.Session, timeout: int = 40) -> tuple[PageState, str]:
    """The signed-in landing page, which lists every course the student has."""
    return _get(s, f"{BASE}/student/top", "top", timeout)


def online(timeout: int = 8) -> bool:
    """Can we reach LearningSuite at all?"""
    try:
        requests.get(f"{BASE}/iCalFeed/ical.php", timeout=timeout,
                     headers={"User-Agent": UA}, allow_redirects=False)
        return True
    except requests.RequestException:
        return False
