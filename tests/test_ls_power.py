"""src/ls/power.py and the unattended sign-in loop in src/ls/browser.py."""
from __future__ import annotations

import pytest

from src.ls import browser, power


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_awake_ignores_time_spent_asleep():
    c = Clock()
    a = power.Awake(cap=6, clock=c)
    c.t += 2
    assert a.tick() == 2
    c.t += 3600
    assert a.tick() == 8
    c.t -= 50
    assert a.tick() == 8


def test_wait_until_true_immediately_and_gives_up():
    assert power.wait_until(lambda: True, 0)
    calls = []
    assert not power.wait_until(lambda: calls.append(1), 0)
    assert len(calls) == 1


def test_wait_until_polls_until_true():
    answers = iter([False, False, True])
    assert power.wait_until(lambda: next(answers), 30, poll=0.001, sleep=lambda s: None)


def test_wait_for_wake(monkeypatch):
    c = Clock()
    states = iter([False, False, True])
    monkeypatch.setattr(power, "full_wake", lambda: next(states))
    assert power.wait_for_wake(60, poll=5, sleep=lambda s: setattr(c, "t", c.t + s),
                               clock=c)
    assert c.t == 1010
    monkeypatch.setattr(power, "full_wake", lambda: False)
    assert not power.wait_for_wake(12, poll=5, sleep=lambda s: setattr(c, "t", c.t + s),
                                   clock=c)


def test_full_wake_reads_pmset(monkeypatch):
    class Out:
        stdout = ""
    monkeypatch.setattr(power, "MAC", True)
    monkeypatch.setattr(power.subprocess, "run", lambda *a, **k: Out)
    Out.stdout = "Current System Capabilities are: CPU Graphics Audio Network \n"
    assert power.full_wake()
    Out.stdout = "Current System Capabilities are: CPU Network \nCurrent Power State: 1\n"
    assert not power.full_wake()
    Out.stdout = "something unexpected"
    assert power.full_wake()

    def boom(*a, **k):
        raise OSError("no pmset")
    monkeypatch.setattr(power.subprocess, "run", boom)
    assert power.full_wake()
    monkeypatch.setattr(power, "MAC", False)
    assert power.full_wake()


CAS = "https://cas.byu.edu/cas/login?service=x"
DUO = "https://api-x.duosecurity.com/prompt/abc"
TOP = "https://learningsuite.byu.edu/.x/student/top"


class Button:
    def __init__(self, page):
        self.page = page

    def is_visible(self):
        return True

    def click(self, timeout=None):
        self.page.clicks += 1
        self.page.on_click(self.page)


class FakePage:
    def __init__(self, clock, url=CAS, filled=True, errors=(), on_click=lambda p: None):
        self.clock, self.url, self.clicks = clock, url, 0
        self.filled, self.errors, self.on_click = filled, list(errors), on_click
        self.gotos = 0

    def goto(self, url, **kw):
        self.gotos += 1

    def is_closed(self):
        return False

    def evaluate(self, js):
        pw = "cas.byu.edu" in self.url
        return {"password_field": pw, "password_filled": pw and self.filled,
                "errors": self.errors, "headings": []}

    def query_selector(self, sel):
        return Button(self) if "cas.byu.edu" in self.url else None

    def get_by_role(self, *a, **k):
        raise RuntimeError("no such button")

    def wait_for_timeout(self, ms):
        self.clock.t += ms / 1000


class Ctx:
    def __init__(self, page):
        self.pages = [page]


@pytest.fixture
def clock(monkeypatch, tmp_path):
    c = Clock()
    real = power.Awake
    monkeypatch.setattr(browser.power, "Awake", lambda: real(clock=c))
    monkeypatch.setattr(browser, "TRAIL", tmp_path / "ls-login.log")
    return c


def sign_in(page, interactive=False, timeout=60):
    browser._signed_in(Ctx(page), page, interactive, timeout)


def test_filled_form_is_submitted_once_and_signs_in(clock):
    p = FakePage(clock, on_click=lambda p: setattr(p, "url", TOP))
    sign_in(p)
    assert p.clicks == 1
    assert "submitted the sign-in form (1)" in browser.TRAIL.read_text()


def test_empty_password_is_never_submitted(clock):
    p = FakePage(clock, filled=False)
    with pytest.raises(browser.LoginFailed) as e:
        sign_in(p)
    assert p.clicks == 0
    assert "no saved password" in str(e.value) and not e.value.retry


def test_refused_password_stops_after_one_submit(clock):
    p = FakePage(clock, on_click=lambda p: p.errors.append("Invalid credentials."))
    with pytest.raises(browser.LoginFailed) as e:
        sign_in(p)
    assert p.clicks == 1
    assert "Invalid credentials." in str(e.value)
    assert not e.value.retry and e.value.definite and e.value.kind == "logged_out"


def test_form_that_keeps_coming_back_is_submitted_at_most_three_times(clock):
    p = FakePage(clock)
    with pytest.raises(browser.LoginFailed) as e:
        sign_in(p)
    assert p.clicks == browser.MAX_SUBMITS and not e.value.retry


def test_stuck_on_duo_says_so_and_can_be_retried(clock):
    p = FakePage(clock, on_click=lambda p: setattr(p, "url", DUO))
    with pytest.raises(browser.LoginFailed) as e:
        sign_in(p)
    assert "Duo" in str(e.value) and e.value.retry and e.value.definite


def test_sleep_during_sign_in_does_not_use_up_the_deadline(clock):
    def click(p):
        p.clock.t += 3600
        p.url = DUO
        p.wakes = 3
    p = FakePage(clock, on_click=click)
    orig = p.wait_for_timeout

    def wait(ms):
        orig(ms)
        if getattr(p, "wakes", 0):
            p.wakes -= 1
            if not p.wakes:
                p.url = TOP
    p.wait_for_timeout = wait
    sign_in(p)


def test_browser_error_page_is_reloaded_then_reported_offline(clock):
    p = FakePage(clock, url="chrome-error://chromewebdata/")
    with pytest.raises(browser.LoginFailed) as e:
        sign_in(p)
    assert e.value.kind == "offline" and p.gotos == 1 + 3


def test_goto_network_error_is_offline(clock):
    p = FakePage(clock)

    def goto(url, **kw):
        raise RuntimeError("Page.goto: net::ERR_INTERNET_DISCONNECTED at https://x")
    p.goto = goto
    with pytest.raises(browser.LoginFailed) as e:
        sign_in(p)
    assert e.value.kind == "offline" and e.value.retry


def test_interactive_never_clicks(clock):
    p = FakePage(clock)
    with pytest.raises(browser.LoginFailed) as e:
        sign_in(p, interactive=True, timeout=20)
    assert p.clicks == 0 and "timed out" in str(e.value)


def test_trail_is_kept_across_runs_and_trimmed(clock):
    browser.TRAIL.write_text("".join(f"old {i}\n" for i in range(500)))
    browser._trail("login started", start=True)
    lines = browser.TRAIL.read_text().splitlines()
    assert len(lines) == 301 and lines[0] == "old 200" and "login started" in lines[-1]


def test_trail_urls_carry_no_query_and_no_session_token():
    assert browser._short("https://learningsuite.byu.edu/.N9pS/student/top?x=1#y") \
        == "learningsuite.byu.edu/.x/student/top"
    assert browser._short("https://cas.byu.edu/cas/login?service=secret") \
        == "cas.byu.edu/cas/login"


def _login_data(profile, rows):
    import sqlite3
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(profile / "Default" / "Login Data")
    con.execute("CREATE TABLE IF NOT EXISTS logins (origin_url TEXT, blacklisted_by_user INTEGER)")
    con.execute("DELETE FROM logins")
    con.executemany("INSERT INTO logins VALUES (?, ?)", rows)
    con.commit()
    con.close()


def test_saved_logins_counts_saved_passwords_not_never_for_this_site(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "PROFILE", tmp_path)
    assert browser._saved_logins() == 0
    _login_data(tmp_path, [("https://cas.byu.edu/", 0), ("https://x/", 1)])
    assert browser._saved_logins() == 1
    (tmp_path / "Default" / "Login Data").write_text("not a database")
    assert browser._saved_logins() is None


def test_linger_closes_once_the_password_is_saved(clock, monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "PROFILE", tmp_path)
    page = FakePage(clock, url=TOP)
    start = clock.t
    real_wait = page.wait_for_timeout

    def wait(ms):
        real_wait(ms)
        if clock.t - start >= 5 and not browser._saved_logins():
            _login_data(tmp_path, [("https://cas.byu.edu/", 0)])
    page.wait_for_timeout = wait
    browser._linger(Ctx(page), 30, saved_before=0)
    assert 5 <= clock.t - start < 10


def test_linger_closes_at_once_when_a_password_was_already_saved(clock):
    page = FakePage(clock, url=TOP)
    start = clock.t
    browser._linger(Ctx(page), 30, saved_before=1)
    assert clock.t == start


def test_linger_gives_up_after_its_time_without_a_save(clock, monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "PROFILE", tmp_path)
    page = FakePage(clock, url=TOP)
    start = clock.t
    browser._linger(Ctx(page), 30, saved_before=0)
    assert 30 <= clock.t - start < 32
