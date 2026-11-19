"""The one job a browser still has: getting signed in."""
from __future__ import annotations

import os
import re
import time

from ..config import DATA_DIR, ROOT
from . import power
from .session import BASE

PROFILE = DATA_DIR / ".browser-profile"
BROWSERS = ROOT / ".tools" / "browsers"
if BROWSERS.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS))
TRAIL = ROOT / "logs" / "ls-login.log"
_LOGIN_HOST = re.compile(r"https://(?:login|cas|id)\.byu\.edu/|duosecurity\.com", re.I)
_SUBMIT = ["#okta-signin-submit", "input[type=submit]", "button[type=submit]",
           "input[value='Log In']", "input[value='Sign In']", "input[value='Verify']"]


_NET_ERR = re.compile(r"ERR_(INTERNET_DISCONNECTED|NAME_NOT_RESOLVED|NETWORK_\w+|"
                      r"CONNECTION_\w+|ADDRESS_UNREACHABLE|TIMED_OUT)|Timeout \d+ms", re.I)
MAX_SUBMITS = 3
_LOOK = """() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const auto = e => { try { return e.matches(':autofill'); } catch (_) { return false; } };
  const text = e => (e.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 120);
  const pw = [...document.querySelectorAll('input[type=password]')].filter(vis);
  return {
    password_field: pw.length > 0,
    password_filled: pw.some(e => !!e.value || auto(e)),
    errors: [...document.querySelectorAll('[role=alert], .alert-danger, .banner-danger, ' +
        '.errors, #msg.errors, .okta-form-infobox-error, .o-form-error-container')]
      .filter(vis).map(text).filter(Boolean).slice(0, 2),
    headings: [...document.querySelectorAll('h1, h2')].filter(vis).map(text)
      .filter(Boolean).slice(0, 3),
  };
}"""


class LoginFailed(Exception):
    def __init__(self, msg: str, kind: str = "logged_out", retry: bool = True,
                 definite: bool = False):
        super().__init__(msg)
        self.kind, self.retry, self.definite = kind, retry, definite


def _trail(msg: str, start: bool = False) -> None:
    """Append to the sign-in trail."""
    TRAIL.parent.mkdir(exist_ok=True)
    if start:
        try:
            lines = TRAIL.read_text().splitlines()[-300:]
            TRAIL.write_text("".join(ln + "\n" for ln in lines))
        except OSError:
            pass
    with TRAIL.open("a") as f:
        f.write(f"{time.strftime('%F %T')} {msg}\n")


def _short(url: str) -> str:
    """Host and path only, with LearningSuite's per-session path token masked."""
    url = re.sub(r"^https?://|[?#].*$", "", url)
    return re.sub(r"(learningsuite\.byu\.edu/)\.[^/]+/", r"\1.x/", url)[:80]


def _where(page) -> str:
    try:
        url = page.url
    except Exception:
        return "moving"
    if url.startswith("chrome-error://"):
        return "error"
    if _LOGIN_HOST.search(url):
        return "login"
    if "learningsuite.byu.edu" in url and "/student/" in url:
        return "in"
    return "moving"


def _click_sign_in(page) -> bool:
    for sel in _SUBMIT:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=2000)
                return True
        except Exception:
            continue
    try:
        page.get_by_role("button", name=re.compile("sign ?in|log ?in|verify", re.I)
                         ).first.click(timeout=2000)
        return True
    except Exception:
        return False


def _open(pw, headless: bool = False):
    # Headed on purpose: Duo's device trust is tied to the visible browser.
    PROFILE.mkdir(parents=True, exist_ok=True)
    PROFILE.chmod(0o700)
    ctx = pw.chromium.launch_persistent_context(
        str(PROFILE), headless=headless, viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"])
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


def _look(page) -> dict | None:
    try:
        return page.evaluate(_LOOK)
    except Exception:
        return None


def _goto_top(page) -> None:
    try:
        page.goto(f"{BASE}/student/top", timeout=45000, wait_until="domcontentloaded")
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        _trail(f"could not open LearningSuite: {msg}")
        if _NET_ERR.search(msg):
            raise LoginFailed("the browser could not reach learningsuite.byu.edu",
                              kind="offline") from None
        raise


def _why_stuck(page, submits: int) -> LoginFailed:
    """Turn 'it timed out' into the thing a person can act on."""
    hand = " -- click Sign in to LearningSuite in the planner (or run ./plan ls-login) and sign in by hand"
    where, look = _short(page.url), _look(page) or {}
    _trail(f"gave up at {where}: {look}")
    if "duosecurity.com" in where:
        return LoginFailed("Duo is asking for a fresh approval (its 'remember this "
                           "device' ran out)" + hand, definite=True)
    if look.get("password_field") and not look.get("password_filled"):
        return LoginFailed("the browser has no saved password to fill in" + hand,
                           retry=False, definite=True)
    if look.get("password_field") and submits:
        return LoginFailed("the sign-in form was submitted and came back" + hand,
                           retry=False, definite=True)
    return LoginFailed(f"automatic sign-in did not finish (last page: {where})" + hand)


def _signed_in(ctx, page, interactive: bool, timeout: int) -> None:
    """Drive (or wait for) sign-in until a LearningSuite page is showing."""
    _goto_top(page)
    awake, last, submits, reloads = power.Awake(), None, 0, 0
    pages = [page]
    while awake.tick() < timeout:
        pages = [p for p in ctx.pages if not p.is_closed()]
        if not pages:
            _trail("window closed before sign-in finished")
            raise LoginFailed("the login window was closed before sign-in finished",
                              retry=False, definite=True)
        states = [_where(p) for p in pages]
        if states != last:
            _trail(f"tabs: {[(s, _short(p.url)) for s, p in zip(states, pages)]}")
            last = states
        if "in" in states:
            pages[states.index("in")].wait_for_timeout(1500)
            return
        if "error" in states:
            reloads += 1
            if reloads > 3:
                raise LoginFailed("the browser kept losing the connection",
                                  kind="offline")
            pages[states.index("error")].wait_for_timeout(2000)
            _goto_top(pages[states.index("error")])
            continue
        if not interactive and "login" in states:
            p = pages[states.index("login")]
            look = _look(p)
            if look and look["password_field"]:
                if submits and look["errors"]:
                    _trail(f"sign-in page answered: {look['errors']}")
                    raise LoginFailed(
                        f"the sign-in page refused the saved login "
                        f"(\"{look['errors'][0]}\") -- click Sign in to LearningSuite in "
                        f"the planner (or run ./plan ls-login) and sign in by hand", retry=False, definite=True)
                if look["password_filled"] and submits < MAX_SUBMITS:
                    if _click_sign_in(p):
                        submits += 1
                        _trail(f"submitted the sign-in form ({submits})")
            elif look:
                _click_sign_in(p)
        pages[0].wait_for_timeout(1500 if not interactive else 2500)
    if interactive:
        raise LoginFailed("timed out waiting for sign-in", retry=False, definite=True)
    raise _why_stuck(pages[0], submits)


def _saved_logins() -> int | None:
    """How many passwords this profile has saved, or None if that can't be told."""
    import sqlite3
    path = PROFILE / "Default" / "Login Data"
    if not path.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            return con.execute("SELECT COUNT(*) FROM logins "
                               "WHERE blacklisted_by_user = 0").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _linger(ctx, seconds: int, saved_before: int | None) -> None:
    if saved_before:
        return
    awake = power.Awake()
    while awake.tick() < seconds:
        pages = [p for p in ctx.pages if not p.is_closed()]
        if not pages:
            return
        now = _saved_logins()
        if now and now > (saved_before or 0):
            _trail("password saved")
            pages[0].wait_for_timeout(1000)
            return
        pages[0].wait_for_timeout(1000)


def login(interactive: bool = False, timeout: int | None = None,
          linger: int = 0) -> tuple[list[dict], str]:
    """Sign in and return (cookies, user_agent). Raises LoginFailed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise LoginFailed("the browser the planner signs in with is not installed "
                          "-- run ./install", retry=False, definite=True) from None
    _trail(f"login started (interactive={interactive}, "
           f"full_wake={power.full_wake()})", start=True)
    with sync_playwright() as pw:
        ctx = None
        try:
            saved_before = _saved_logins()
            ctx, page = _open(pw)
            if interactive:
                print("A browser window has opened. Sign in and approve Duo "
                      "(tick 'remember this device'). This finishes by itself.")
            _signed_in(ctx, page, interactive, timeout or (300 if interactive else 60))
            live = next((p for p in ctx.pages if not p.is_closed()), page)
            ua = live.evaluate("navigator.userAgent")
            cookies = ctx.cookies()
            _trail("signed in")
            if interactive and linger:
                if not saved_before:
                    print(f"Signed in. Click Save when the browser offers to save your "
                          f"password; the window then closes (or in {linger} seconds).")
                _linger(ctx, linger, saved_before)
            return cookies, ua
        except LoginFailed as e:
            _trail(f"failed: {e}")
            raise
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:160]}"
            _trail(f"error: {msg}")
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                raise LoginFailed("the browser the planner signs in with has not been "
                                  "downloaded -- run ./install", retry=False,
                                  definite=True) from None
            raise LoginFailed(f"browser error during sign-in: {msg[:140]}",
                              kind="offline" if _NET_ERR.search(msg) else "logged_out"
                              ) from None
        finally:
            if ctx is not None:
                ctx.close()


def fetch_html(course_ids: dict[str, str], pages: list[str],
               interactive: bool = False) -> dict[tuple[str, str], tuple[str, str]]:
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as pw:
        ctx, page = _open(pw)
        try:
            _signed_in(ctx, page, interactive, 300 if interactive else 60)
            for key, cid in course_ids.items():
                for pg in pages:
                    try:
                        page.goto(f"{BASE}/.x/cid-{cid}/student/{pg}", timeout=45000,
                                  wait_until="networkidle")
                        out[(key, pg)] = (page.url, page.content())
                    except Exception as e:
                        out[(key, pg)] = ("", f"<!-- {type(e).__name__} -->")
        finally:
            ctx.close()
    return out
