"""src/ai_tools.py: is an AI tool installed and signed in, install it, open its sign-in."""
import stat
import subprocess
import sys
import textwrap

import pytest
import yaml

from src import agent, ai_tools, config


def fake(tmp_path, name, body):
    p = tmp_path / "bin" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    (d / "prefs.yaml").write_text((config.ROOT / "config" / "prefs.yaml").read_text())
    monkeypatch.setattr(config, "CONFIG_DIR", d)
    monkeypatch.setattr(ai_tools, "_jobs", {})
    monkeypatch.setattr(ai_tools, "_signed", {})
    monkeypatch.delenv("PLANNER_AI", raising=False)
    paths = {}
    monkeypatch.setattr(agent, "find", lambda cli: paths.get(cli))
    return d, paths


def test_choice_is_written_to_a_new_file(home):
    d, _ = home
    assert ai_tools.saved_choice() == ""
    ai_tools.save_choice("codex")
    assert yaml.safe_load((d / "prefs.local.yaml").read_text()) == {"agent": {"cli": "codex"}}
    assert ai_tools.saved_choice() == "codex"


def test_choice_is_added_without_disturbing_other_settings(home):
    d, _ = home
    mine = "# my notes\ndaily_focus_cap_minutes: 200   # less on purpose\n"
    (d / "prefs.local.yaml").write_text(mine)
    ai_tools.save_choice("codex")
    text = (d / "prefs.local.yaml").read_text()
    assert text.startswith(mine)
    assert yaml.safe_load(text) == {"daily_focus_cap_minutes": 200, "agent": {"cli": "codex"}}
    assert not list(d.glob("prefs.local.yaml.bak-*"))


def test_changing_the_choice_keeps_other_agent_settings_and_a_backup(home):
    d, _ = home
    (d / "prefs.local.yaml").write_text("agent:\n  cli: claude\n  plan_model: sonnet\nx: 1\n")
    ai_tools.save_choice("codex")
    assert yaml.safe_load((d / "prefs.local.yaml").read_text()) == {
        "agent": {"cli": "codex", "plan_model": "sonnet"}, "x": 1}
    assert len(list(d.glob("prefs.local.yaml.bak-*"))) == 1
    ai_tools.save_choice("codex")
    assert len(list(d.glob("prefs.local.yaml.bak-*"))) == 1


def test_the_saved_choice_is_what_the_planner_runs(home):
    _, paths = home
    paths.update(claude="/x/claude", codex="/x/codex")
    assert agent.chosen()[0] == "claude"
    ai_tools.save_choice("codex")
    assert agent.chosen()[0] == "codex"


def test_claude_sign_in_is_read_from_its_status(home, tmp_path):
    _, paths = home
    paths["claude"] = fake(tmp_path, "claude", """
        import sys
        assert sys.argv[1:] == ["auth", "status", "--json"]
        print('{"loggedIn": true, "email": "someone@example.com"}')
    """)
    assert ai_tools.signed_in("claude") is True
    s = ai_tools.status()
    claude = s["tools"][0]
    assert claude["installed"] and claude["signed_in"] is True and s["choice"] == "claude"
    assert "someone@example.com" not in str(s)


def test_signed_out_claude(home, tmp_path):
    _, paths = home
    paths["claude"] = fake(tmp_path, "claude", "print('{\"loggedIn\": false}')")
    assert ai_tools.signed_in("claude") is False


def test_old_claude_without_a_status_command_is_unknown_not_signed_out(home, tmp_path):
    _, paths = home
    paths["claude"] = fake(tmp_path, "claude", "import sys; sys.exit('unknown command')")
    assert ai_tools.signed_in("claude") is None


def test_codex_sign_in_is_its_exit_code(home, tmp_path):
    _, paths = home
    paths["codex"] = fake(tmp_path, "codex", """
        import sys
        assert sys.argv[1:] == ["login", "status"]
        sys.exit(1)
    """)
    assert ai_tools.signed_in("codex") is False
    paths["codex"] = fake(tmp_path, "codex", "pass")
    assert ai_tools.signed_in("codex", fresh=True) is True


def test_sign_in_answer_is_reused_for_a_moment(home, tmp_path):
    _, paths = home
    count = tmp_path / "count"
    paths["codex"] = fake(tmp_path, "codex", f"open({str(count)!r}, 'a').write('x')")
    for _ in range(5):
        ai_tools.signed_in("codex")
    assert count.read_text() == "x"


def test_not_installed_means_not_signed_in(home):
    assert ai_tools.signed_in("codex") is False
    s = ai_tools.status()
    assert s["choice"] == "" and not any(t["installed"] for t in s["tools"])


def test_default_choice_prefers_a_tool_that_is_ready(home, tmp_path):
    _, paths = home
    paths["claude"] = fake(tmp_path, "claude", "print('{\"loggedIn\": false}')")
    paths["codex"] = fake(tmp_path, "codex", "pass")
    assert ai_tools.status()["choice"] == "codex"


def test_install_runs_the_makers_installer_and_reports_success(home, tmp_path, monkeypatch):
    _, paths = home
    marker = tmp_path / "installed"
    monkeypatch.setitem(ai_tools.TOOLS["claude"], "install", f"echo hi > {marker}")
    monkeypatch.setattr(agent, "find",
                        lambda cli: "/x/claude" if cli == "claude" and marker.exists() else None)
    ai_tools._run_install("claude")
    assert marker.exists()
    assert ai_tools._jobs["claude"] == {"status": "ok", "detail": ""}


def test_failed_install_keeps_the_reason(home, monkeypatch):
    monkeypatch.setitem(ai_tools.TOOLS["codex"], "install", "echo 'no space left' >&2; exit 3")
    ai_tools._run_install("codex")
    job = ai_tools._jobs["codex"]
    assert job["status"] == "failed" and "no space left" in job["detail"]


def test_install_that_leaves_no_command_is_a_failure(home, monkeypatch):
    monkeypatch.setitem(ai_tools.TOOLS["codex"], "install", "true")
    ai_tools._run_install("codex")
    assert ai_tools._jobs["codex"]["status"] == "failed"
    assert "not found afterwards" in ai_tools._jobs["codex"]["detail"]


def test_installers_are_the_published_ones():
    assert ai_tools.TOOLS["claude"]["install"] == "curl -fsSL https://claude.ai/install.sh | bash"
    assert ai_tools.TOOLS["codex"]["install"].startswith(
        "curl -fsSL https://chatgpt.com/codex/install.sh | ")
    for cli in ai_tools.TOOLS:
        assert all(ai_tools.manual_steps(cli))


def test_only_known_tools_can_be_installed(home):
    with pytest.raises(ValueError):
        ai_tools.install("something; rm -rf ~")


def test_sign_in_opens_a_terminal_running_the_tools_own_login(home, monkeypatch):
    _, paths = home
    paths["codex"] = "/opt/tools/bin/codex"
    opened = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: opened.append(cmd))
    assert ai_tools.open_sign_in("codex") == ""
    assert opened[0][0] == "/usr/bin/open" and opened[0][1].endswith(".command")
    script = open(opened[0][1]).read()
    assert "/opt/tools/bin/codex login" in script
    assert stat.S_IMODE(__import__("os").stat(opened[0][1]).st_mode) & stat.S_IXUSR


def test_sign_in_falls_back_to_showing_the_command(home, monkeypatch):
    _, paths = home
    paths["claude"] = "/home/pat/my tools/claude"
    monkeypatch.setattr(sys, "platform", "linux")
    assert ai_tools.open_sign_in("claude") == "'/home/pat/my tools/claude' auth login"


def test_sign_in_needs_the_tool(home):
    with pytest.raises(RuntimeError, match="not installed"):
        ai_tools.open_sign_in("claude")


def test_switching_account_signs_out_first(home, tmp_path, monkeypatch):
    _, paths = home
    state = tmp_path / "signed-in"
    state.write_text("yes")
    paths["codex"] = fake(tmp_path, "codex", f"""
        import os, sys
        f = {str(state)!r}
        if sys.argv[1:] == ["logout"]:
            os.remove(f)
        elif sys.argv[1:] == ["login", "status"]:
            sys.exit(0 if os.path.exists(f) else 1)
    """)
    assert ai_tools.signed_in("codex") is True
    monkeypatch.setattr(sys, "platform", "linux")
    assert ai_tools.open_sign_in("codex", switch=True).endswith("codex login")
    assert not state.exists() and ai_tools.signed_in("codex") is False


def test_sign_out_that_does_not_take_is_reported(home, tmp_path):
    _, paths = home
    paths["codex"] = fake(tmp_path, "codex", "pass")
    with pytest.raises(RuntimeError, match="still says it is signed in"):
        ai_tools.open_sign_in("codex", switch=True)


def test_copilot_sign_in_is_read_from_the_keychain_without_opening_it(home, monkeypatch):
    _, paths = home
    paths["copilot"] = "/opt/tools/bin/copilot"
    asked = []

    class Result:
        returncode = 0
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ai_tools, "_quiet", lambda cmd, **kw: (asked.append(cmd), Result())[1])
    assert ai_tools.signed_in("copilot") is True
    assert asked == [["/usr/bin/security", "find-generic-password", "-s", "copilot-cli"]]
    assert "-w" not in asked[0] and "-g" not in asked[0]


def test_copilot_not_in_keychain_is_signed_out(home, tmp_path, monkeypatch):
    _, paths = home
    paths["copilot"] = "/opt/tools/bin/copilot"

    class Result:
        returncode = 44
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ai_tools, "_quiet", lambda cmd, **kw: Result())
    monkeypatch.setattr(ai_tools.Path, "home", lambda: tmp_path)
    assert ai_tools.signed_in("copilot") is False
    (tmp_path / ".copilot").mkdir()
    (tmp_path / ".copilot" / "config.json").write_text("{}")
    assert ai_tools.signed_in("copilot", fresh=True) is None


def test_copilot_sign_in_cannot_be_told_off_a_mac(home, monkeypatch):
    _, paths = home
    paths["copilot"] = "/opt/tools/bin/copilot"
    monkeypatch.setattr(sys, "platform", "linux")
    assert ai_tools.signed_in("copilot") is None


def test_every_tool_says_what_it_costs_and_has_a_logo(home):
    tools = {t["cli"]: t for t in ai_tools.status()["tools"]}
    assert list(tools) == ["claude", "codex", "copilot"]
    assert tools["claude"]["tiers"] == [["paid", "Paid"]]
    assert [k for k, _ in tools["codex"]["tiers"]] == ["paid", "free"]
    assert [k for k, _ in tools["copilot"]["tiers"]] == ["free"]
    static = config.ROOT / "src" / "web" / "static"
    for t in tools.values():
        svg = (static / t["logo"]).read_text()
        assert svg.startswith("<svg") and "<script" not in svg and "href" not in svg


def test_copilot_explains_the_student_account(home):
    copilot = next(t for t in ai_tools.status()["tools"] if t["cli"] == "copilot")
    account = copilot["account"]
    assert "student" in account["title"].lower()
    links = [url for _, url in account["steps"] if url]
    assert "https://github.com/settings/education/benefits" in links
    assert all(u.startswith("https://github.com/") for u in links)
    assert account["help"].startswith("https://docs.github.com/")
    assert copilot["can_switch"] is False
    assert next(t for t in ai_tools.status()["tools"] if t["cli"] == "claude")["account"] is None


def test_switching_copilot_account_points_at_its_own_logout(home):
    _, paths = home
    paths["copilot"] = "/opt/tools/bin/copilot"
    with pytest.raises(RuntimeError, match="/logout"):
        ai_tools.open_sign_in("copilot", switch=True)


def test_install_runs_without_a_terminal_to_ask_questions_on(home, tmp_path, monkeypatch):
    seen = {}
    real = subprocess.run

    def run(cmd, **kw):
        seen.update(kw)
        return real(["/bin/bash", "-c", "true"], capture_output=True, text=True)
    monkeypatch.setattr(subprocess, "run", run)
    ai_tools._run_install("copilot")
    assert seen["start_new_session"] is True and seen["stdin"] == subprocess.DEVNULL
