"""Choosing and running the AI command-line tool."""
import json
import os
import stat
import textwrap

import pytest

from src import agent

SCHEMA = {"type": "object", "properties": {"estimates": {"type": "array"}},
          "required": ["estimates"], "additionalProperties": False}
ANSWER = {"estimates": [{"uid": "a", "est_minutes": 30}]}


def fake(tmp_path, name, body):
    import sys
    p = tmp_path / "bin" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.fixture
def only(tmp_path, monkeypatch):
    def setup(*names, prefs=None):
        paths = {n: str(tmp_path / "bin" / n) for n in names}
        monkeypatch.setattr(agent, "find", lambda cli: paths.get(cli))
        monkeypatch.setattr(agent, "_prefs", lambda: prefs or {})
        monkeypatch.delenv("PLANNER_AI", raising=False)
    return setup


def test_first_installed_tool_is_used(only):
    only("codex")
    assert agent.chosen()[0] == "codex"
    only("claude", "codex")
    assert agent.chosen()[0] == "claude"


def test_prefs_pick_a_tool_over_the_default_order(only):
    only("claude", "codex", prefs={"cli": "codex"})
    assert agent.chosen()[0] == "codex"


def test_environment_beats_prefs(only, monkeypatch):
    only("claude", "codex", prefs={"cli": "codex"})
    monkeypatch.setenv("PLANNER_AI", "claude")
    assert agent.chosen()[0] == "claude"


def test_asking_for_a_tool_that_is_not_installed_says_so(only):
    only("claude", prefs={"cli": "codex"})
    with pytest.raises(agent.AgentError, match="ChatGPT is chosen"):
        agent.chosen()
    assert agent.available() is None


def test_none_turns_it_off(only):
    only("claude", prefs={"cli": "none"})
    assert agent.available() is None


def test_unknown_tool_name_is_refused(only):
    only("claude", prefs={"cli": "gemini"})
    with pytest.raises(agent.AgentError, match="must be one of"):
        agent.chosen()


def test_nothing_installed(only):
    only()
    assert agent.available() is None
    assert agent.describe("opus") == "none"
    with pytest.raises(agent.AgentError, match="no AI tool found"):
        agent.run_json("p", SCHEMA)


def test_bin_override_wins(monkeypatch):
    monkeypatch.setenv("CODEX_BIN", "/somewhere/codex")
    assert agent.find("codex") == "/somewhere/codex"


def test_claude_model_names_are_not_passed_to_other_tools(only):
    only("codex")
    assert agent.model_for("claude", "opus") == "opus"
    assert agent.model_for("codex", "opus") is None
    assert agent.model_for("codex", "claude-sonnet-5-5") is None
    assert agent.model_for("codex", "gpt-5") == "gpt-5"
    assert agent.model_for("codex", None) is None


def test_per_tool_model_from_prefs(only):
    only("codex", prefs={"codex": {"plan_model": "gpt-big"}})
    assert agent.model_for("codex", "opus", "plan") == "gpt-big"
    assert agent.model_for("codex", "sonnet", "estimate") is None
    assert agent.describe("opus", "plan") == "ChatGPT (gpt-big)"
    assert agent.describe("sonnet", "estimate") == "ChatGPT"


def test_describe_claude(only):
    only("claude")
    assert agent.describe("opus", "plan") == "Claude (opus)"


@pytest.mark.parametrize("text", [
    json.dumps(ANSWER),
    "Here you go:\n```json\n" + json.dumps(ANSWER, indent=2) + "\n```\nHope that helps.",
    'I considered {"uid": "x"} first.\n' + json.dumps(ANSWER),
    "  \n" + json.dumps(ANSWER) + "\n\n",
])
def test_json_is_found_in_a_wordy_answer(text):
    assert agent.extract_json(text) == ANSWER


@pytest.mark.parametrize("text", ["", "I could not do that.", "[1, 2, 3]", "{broken"])
def test_no_json_is_an_error(text):
    with pytest.raises(agent.AgentError, match="no JSON object"):
        agent.extract_json(text)


def test_prompt_carries_the_schema():
    full = agent.with_schema("PROMPT", SCHEMA)
    assert full.startswith("PROMPT") and json.dumps(SCHEMA) in full


def test_claude_is_called_with_schema_and_model(tmp_path, only):
    from src import config
    fake(tmp_path, "claude", f"""
        import json, os, sys
        a = sys.argv[1:]
        assert a[0] == "-p" and "--json-schema" in a
        assert a[a.index("--tools") + 1] == "" and "--strict-mcp-config" in a
        assert os.path.realpath(os.getcwd()) != os.path.realpath({str(config.ROOT)!r})
        json.dump({{"structured_output": {ANSWER!r}, "model": a[a.index("--model") + 1],
                   "usage": {{"input_tokens": 5}}}}, sys.stdout)
    """)
    only("claude")
    assert agent.run_json("p", SCHEMA, "opus") == ANSWER


def test_codex_gets_the_prompt_on_stdin_and_answers_in_a_file(tmp_path, only):
    seen = tmp_path / "seen.json"
    fake(tmp_path, "codex", f"""
        import json, os, sys
        a = sys.argv[1:]
        assert a[0] == "exec" and a[-1] == "-", a
        out = a[a.index("--output-last-message") + 1]
        json.dump({{"args": a, "cwd": os.getcwd(), "stdin": sys.stdin.read()}},
                  open({str(seen)!r}, "w"))
        print("thinking out loud on stdout")
        open(out, "w").write(json.dumps({ANSWER!r}))
    """)
    only("codex")
    assert agent.run_json("THE PROMPT", SCHEMA, "opus") == ANSWER
    got = json.loads(seen.read_text())
    assert got["stdin"].startswith("THE PROMPT") and json.dumps(SCHEMA) in got["stdin"]
    assert "--model" not in got["args"]
    assert "read-only" in got["args"] and "--skip-git-repo-check" in got["args"]
    from src import config
    assert os.path.realpath(got["cwd"]) != os.path.realpath(config.ROOT)
    assert not os.path.exists(got["cwd"])


def test_signed_out_tool_says_how_to_fix_it(tmp_path, only):
    fake(tmp_path, "codex", """
        import sys
        sys.stderr.write("Error: not logged in. Run `codex login`.\\n")
        sys.exit(1)
    """)
    only("codex")
    with pytest.raises(agent.AgentError, match=r"ChatGPT is not signed in. Open Settings.*run `codex login`"):
        agent.run_json("p", SCHEMA)


def test_usage_limit_is_reported_as_such(tmp_path, only):
    fake(tmp_path, "codex", """
        import sys
        sys.stderr.write("You have hit your usage limit\\n")
        sys.exit(1)
    """)
    only("codex")
    with pytest.raises(agent.AgentError, match="usage limit"):
        agent.run_json("p", SCHEMA)


def test_answer_missing_a_required_key_is_refused(tmp_path, only):
    fake(tmp_path, "codex", """
        import json
        print(json.dumps({"something": "else"}))
    """)
    only("codex")
    with pytest.raises(agent.AgentError, match="without: estimates"):
        agent.run_json("p", SCHEMA)


def test_slow_tool_times_out(tmp_path, only):
    fake(tmp_path, "codex", "import time; time.sleep(5)")
    only("codex")
    with pytest.raises(agent.AgentError, match="no answer within 1s"):
        agent.run_json("p", SCHEMA, timeout=1)


def test_copilot_gets_the_prompt_on_stdin_with_its_tools_off(tmp_path, only, monkeypatch):
    seen = tmp_path / "seen.json"
    fake(tmp_path, "copilot", f"""
        import json, os, sys
        json.dump({{"args": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read(),
                   "token": os.environ.get("GITHUB_TOKEN"), "gh": os.environ.get("GH_TOKEN")}},
                  open({str(seen)!r}, "w"))
        print("Here is the plan:\\n```json\\n" + json.dumps({ANSWER!r}) + "\\n```")
    """)
    only("copilot")
    monkeypatch.setenv("GITHUB_TOKEN", "left-over-from-another-tool")
    monkeypatch.setenv("GH_TOKEN", "left-over-too")
    assert agent.run_json("THE PROMPT", SCHEMA, "opus") == ANSWER
    got = json.loads(seen.read_text())
    assert got["stdin"].startswith("THE PROMPT") and json.dumps(SCHEMA) in got["stdin"]
    assert "-p" not in got["args"] and "-s" in got["args"]
    for flag in ("--no-ask-user", "--no-custom-instructions", "--deny-tool=shell",
                 "--deny-tool=write", "--deny-tool=url"):
        assert flag in got["args"]
    assert not any(a.startswith("--model") for a in got["args"])
    assert not any("allow" in a or "yolo" in a for a in got["args"])
    assert got["token"] is None and got["gh"] is None
    from src import config
    assert os.path.realpath(got["cwd"]) != os.path.realpath(config.ROOT)


def test_copilot_without_a_plan_says_what_to_do(tmp_path, only):
    fake(tmp_path, "copilot", """
        import sys
        sys.stderr.write("Error: Access denied by policy settings\\n")
        sys.exit(1)
    """)
    only("copilot")
    with pytest.raises(agent.AgentError, match="Copilot Student"):
        agent.run_json("p", SCHEMA)


def test_copilot_signed_out_names_its_sign_in(tmp_path, only):
    fake(tmp_path, "copilot", """
        import sys
        sys.stderr.write("Error: No authentication information found\\n")
        sys.exit(1)
    """)
    only("copilot")
    with pytest.raises(agent.AgentError, match="copilot login"):
        agent.run_json("p", SCHEMA)
