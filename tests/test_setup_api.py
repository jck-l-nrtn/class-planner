"""src/web/setup_api.py: setup in the browser."""
from __future__ import annotations

import pytest

from src import agent, ai_tools, config, setup_wizard as wiz
from src.web import setup_api as api

TOKEN = "7407~" + "aB3" * 22
CANVAS = [{"canvas_id": 111, "name": "BIO 100: Principles of Biology", "code": "BIO 100-003",
           "term": "", "start": None, "end": None}]
LS = [{"id": "aaa", "title": "HIST 201 (004)  - World Civ", "term": "Fall 2026"}]
WEEK = {"timezone": "America/Denver", "wake": "07:00", "sleep": "23:00",
        "term_start": "2026-09-01", "term_end": "2026-12-18"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "config"
    (d / "examples").mkdir(parents=True)
    real = config.ROOT / "config"
    (d / "prefs.yaml").write_text((real / "prefs.yaml").read_text())
    (d / "examples" / "courses.yaml").write_text((real / "examples" / "courses.yaml").read_text())
    monkeypatch.setattr(config, "CONFIG_DIR", d)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    monkeypatch.delenv("CANVAS_BASE_URL", raising=False)
    monkeypatch.setattr(api, "_canvas", {"name": "", "courses": [], "error": ""})
    monkeypatch.setattr(api, "_ls", {"status": "idle", "detail": "", "courses": [],
                                     "term": "", "session": None})
    monkeypatch.setattr(api, "_first", {"status": "idle", "log": []})
    monkeypatch.setattr(wiz, "canvas_courses", lambda base, token: ("Pat Student", CANVAS))
    monkeypatch.setattr(agent, "find", lambda cli: None)
    monkeypatch.setattr(ai_tools, "_jobs", {})
    monkeypatch.setattr(ai_tools, "_signed", {})
    ran = []
    monkeypatch.setattr(api, "_first_run", lambda: ran.append(1))
    monkeypatch.setattr(api.Path, "home", lambda: tmp_path / "home")
    return tmp_path, ran


def test_fresh_install_needs_setup_and_never_reveals_a_token(home):
    s = api.state()
    assert s["needed"] is True and s["canvas"]["saved"] is False and s["courses"] == []
    api.canvas({"token": TOKEN})
    assert TOKEN not in str(api.state())


def test_canvas_token_is_verified_saved_and_cleaned(home):
    root, _ = home
    res, code = api.canvas({"base": "byu.instructure.com/", "token": f"\nToken: {TOKEN}\n"})
    assert code == 200 and res == {"ok": True, "name": "Pat Student", "count": 1}
    assert wiz.read_env(root / ".env") == {"CANVAS_BASE_URL": "https://byu.instructure.com",
                                           "CANVAS_TOKEN": TOKEN}
    s = api.state()
    assert s["canvas"]["saved"] and s["canvas"]["name"] == "Pat Student"
    assert [c["key"] for c in s["courses"]] == ["BIO100"]


def test_rejected_or_missing_token_saves_nothing(home, monkeypatch):
    root, _ = home
    assert api.canvas({"token": ""})[1] == 400

    def reject(base, token):
        raise RuntimeError("Canvas rejected the token (401). Is it expired?")
    monkeypatch.setattr(wiz, "canvas_courses", reject)
    res, code = api.canvas({"token": TOKEN})
    assert code == 400 and "did not accept" in res["error"]
    assert not (root / ".env").exists()


def test_empty_token_reverifies_the_saved_one(home):
    api.canvas({"token": TOKEN})
    res, code = api.canvas({"token": ""})
    assert code == 200 and res["ok"]


def test_ls_login_runs_once_in_the_background(home, monkeypatch):
    started = []
    monkeypatch.setattr(api.threading, "Thread",
                        lambda target, daemon: type("T", (), {"start": lambda self: started.append(target)})())
    api.ls_login({})
    api.ls_login({})
    assert len(started) == 1 and api.state()["learningsuite"]["status"] == "running"


def test_finish_writes_config_from_the_servers_own_course_list(home):
    root, ran = home
    api.canvas({"token": TOKEN})
    api._ls.update(status="ok", courses=LS, term="Fall 2026")
    res, code = api.finish({**WEEK, "courses": [
        {"key": "BIO100", "credits": "4", "meets": "MWF 9:00-9:50"},
        {"key": "HIST201", "credits": "", "meets": ""},
        {"key": "MADEUP999", "credits": "3", "meets": ""},
    ]})
    assert code == 200 and res == {"ok": True}
    c = config.Config()
    assert list(c.courses) == ["BIO100", "HIST201"]
    assert c.courses["BIO100"]["credits"] == 4 and c.canvas_map == {111: "BIO100"}
    assert c.ls_course_ids() == {"HIST201": "aaa"}
    assert c.commitments["recurring"][0]["days"] == ["mon", "wed", "fri"]
    assert api.needed() is False and api.state()["first_run"]["status"] == "running"


def test_finish_reports_every_bad_field_and_writes_nothing(home):
    root, ran = home
    api.canvas({"token": TOKEN})
    res, code = api.finish({"courses": [{"key": "BIO100", "credits": "lots", "meets": "whenever"}],
                            "timezone": "Mars/Phobos", "wake": "7", "sleep": "23:00",
                            "term_start": "2026-09-01", "term_end": "2026-08-01"})
    assert code == 400
    assert set(res["errors"]) == {"credits:BIO100", "meets:BIO100", "timezone", "wake", "term_end"}
    assert api.needed() is True and ran == []


def test_finish_needs_a_course(home):
    res, code = api.finish({**WEEK, "courses": []})
    assert code == 400 and "courses" in res["errors"]


def test_finish_again_keeps_the_old_files(home):
    root, _ = home
    api.canvas({"token": TOKEN})
    body = {**WEEK, "courses": [{"key": "BIO100", "credits": "3", "meets": ""}]}
    api.finish(body)
    api._first["status"] = "done"
    api.finish(body)
    assert len(list((root / "config").glob("courses.yaml.bak-*"))) == 1


def test_course_on_both_sites_can_be_limited_to_one(home, monkeypatch):
    api.canvas({"token": TOKEN})
    api._ls.update(status="ok", term="Fall 2026", courses=[
        {"id": "zzz", "title": "BIO 100 (003) - Principles of Biology", "term": "Fall 2026"}])
    (found,) = api.state()["courses"]
    assert found["canvas_id"] == 111 and found["ls_course_id"] == "zzz"

    api.finish({**WEEK, "courses": [{"key": "BIO100", "credits": "3", "meets": "", "use": "canvas"}]})
    c = config.Config()
    assert c.canvas_map == {111: "BIO100"} and c.ls_course_ids() == {}

    api._first["status"] = "done"
    api.finish({**WEEK, "courses": [{"key": "BIO100", "credits": "3", "meets": "",
                                     "use": "learningsuite"}]})
    c = config.Config()
    assert c.canvas_map == {} and c.ls_course_ids() == {"BIO100": "zzz"}

    api._first["status"] = "done"
    api.finish({**WEEK, "courses": [{"key": "BIO100", "credits": "3", "meets": "", "use": "both"}]})
    c = config.Config()
    assert c.canvas_map == {111: "BIO100"} and c.ls_course_ids() == {"BIO100": "zzz"}


def test_catch_up_is_on_unless_unticked(home):
    api.canvas({"token": TOKEN})
    body = {**WEEK, "courses": [{"key": "BIO100", "credits": "3", "meets": ""}]}
    api.finish(body)
    assert api._first["catch_up"] is True
    api._first["status"] = "done"
    api.finish({**body, "catch_up": False})
    assert api._first["catch_up"] is False


def test_state_lists_the_ai_tools_with_nothing_installed(home):
    ai = api.state()["ai"]
    assert [t["cli"] for t in ai["tools"]] == ["claude", "codex", "copilot"]
    assert ai["choice"] == "" and not any(t["installed"] or t["signed_in"] for t in ai["tools"])
    assert all(t["manual"] and t["needs"] for t in ai["tools"])


def test_choosing_an_ai_is_saved_and_shown(home):
    root, _ = home
    assert api.ai_choose({"cli": "codex"}) == ({"ok": True}, 200)
    assert "cli: codex" in (root / "config" / "prefs.local.yaml").read_text()
    assert api.state()["ai"]["choice"] == "codex"
    assert api.ai_choose({"cli": "none"})[1] == 200
    assert api.state()["ai"]["choice"] == "none"
    assert agent.available() is None


def test_unknown_ai_is_refused_everywhere(home):
    root, _ = home
    for route in (api.ai_choose, api.ai_install, api.ai_signin):
        res, code = route({"cli": "rm -rf"})
        assert code == 400 and not res["ok"]
    assert api.ai_install({"cli": "none"})[1] == 400
    assert not (root / "config" / "prefs.local.yaml").exists()


def test_install_route_starts_the_install_once(home, monkeypatch):
    started = []
    monkeypatch.setattr(ai_tools, "_run_install", lambda cli: started.append(cli))
    assert api.ai_install({"cli": "claude"}) == ({"ok": True}, 200)
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.01)
    assert started == ["claude"]
    claude = next(t for t in api.state()["ai"]["tools"] if t["cli"] == "claude")
    assert claude["install"]["status"] == "running"
    assert claude["install"]["elapsed"] >= 0 and "started" not in claude["install"]
    api.ai_install({"cli": "claude"})
    time.sleep(0.05)
    assert started == ["claude"]


def test_sign_in_before_install_is_an_error(home):
    res, code = api.ai_signin({"cli": "claude"})
    assert code == 400 and "not installed" in res["error"]


def _agent_file(root, folder):
    agents = root / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "com.classplanner.daily.plist").write_text(
        f"<string>{folder}/scripts/run-daily.sh</string>")


def test_schedule_is_on_only_for_this_folders_timers(home):
    root, _ = home
    assert api.state()["schedule"]["on"] is False
    _agent_file(root, "/somewhere/else")
    assert api.schedule_on() is False
    _agent_file(root, root)
    assert api.state()["schedule"]["on"] is True


@pytest.mark.parametrize("on, script", [(True, "install-schedule.sh"),
                                        (False, "uninstall-schedule.sh"),
                                        ("yes", "uninstall-schedule.sh")])
def test_schedule_runs_only_the_planners_own_script(home, monkeypatch, on, script):
    root, _ = home
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(api.sys, "platform", "darwin")
    monkeypatch.setattr(api.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or Done)
    res, code = api.schedule({"on": on})
    assert code == 200 and res["ok"] is True
    (cmd, kw), = calls
    assert cmd == ["/bin/bash", str(root / "scripts" / script)]
    assert kw["stdin"] is api.subprocess.DEVNULL


def test_schedule_failure_is_reported(home, monkeypatch):
    class Failed:
        returncode, stdout, stderr = 1, "", "launchctl: nope"

    monkeypatch.setattr(api.sys, "platform", "darwin")
    monkeypatch.setattr(api.subprocess, "run", lambda cmd, **kw: Failed)
    res, code = api.schedule({"on": True})
    assert code == 500 and res["ok"] is False and "nope" in res["error"]


def test_first_run_reports_its_step_while_running(home):
    assert "steps" not in api.state()["first_run"]
    api._first.update(status="running", steps=["sync", "estimate", "plan"])
    api._step("estimate")
    f = api.state()["first_run"]
    assert [s["label"] for s in f["steps"]] == ["Coursework", "Time estimates", "Plan"]
    assert f["step"] == 1 and f["elapsed"] >= 0
    api._first["status"] = "done"
    assert "steps" not in api.state()["first_run"]
