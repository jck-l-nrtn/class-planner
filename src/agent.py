"""The one place that shells out to an AI command-line tool."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

CLIS = ("claude", "codex", "copilot")
NAMES = {"claude": "Claude", "codex": "ChatGPT", "copilot": "GitHub Copilot"}
SIGN_IN = {"claude": "claude auth login", "codex": "codex login", "copilot": "copilot login"}
CLAUDE_MODELS = ("opus", "sonnet", "haiku", "fable", "claude")


class AgentError(RuntimeError):
    pass


def _prefs() -> dict:
    try:
        prefs = config.load_prefs()
    except Exception:
        return {}
    return prefs.get("agent") or {}


def find(cli: str) -> str | None:
    """Absolute path to one of the tools, or None."""
    override = os.environ.get(f"{cli.upper()}_BIN")
    if override:
        return override
    found = shutil.which(cli)
    if found:
        return found
    home = Path.home()
    for d in (home / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
              home / ".npm-global/bin", home / ".local/node/bin", home / f".{cli}/bin"):
        if (d / cli).exists():
            return str(d / cli)
    return None


def chosen() -> tuple[str, str]:
    """(which tool, its path). Raises AgentError when there is none to use."""
    want = str(os.environ.get("PLANNER_AI") or _prefs().get("cli") or "auto").strip().lower()
    if want == "none":
        raise AgentError("no AI tool is chosen (agent.cli is none). Pick one at /setup#ai "
                         "in the app, or set agent.cli in config/prefs.local.yaml.")
    if want not in ("", "auto"):
        if want not in CLIS:
            raise AgentError(f"agent.cli is `{want}`; it must be one of: auto, none, "
                             + ", ".join(CLIS))
        path = find(want)
        if not path:
            raise AgentError(f"{NAMES[want]} is chosen for plans but its `{want}` command "
                             f"was not found. Install it from /setup#ai in the app, or set "
                             f"{want.upper()}_BIN to its full path.")
        return want, path
    for cli in CLIS:
        path = find(cli)
        if path:
            return cli, path
    raise AgentError("no AI tool found (looked for: " + ", ".join(CLIS)
                     + "). Install one from /setup#ai in the app.")


def available() -> tuple[str, str] | None:
    """Like chosen(), but None instead of an error."""
    try:
        return chosen()
    except AgentError:
        return None


def label(cli: str | None = None) -> str:
    """What to call the tool in messages: "Claude", "ChatGPT", "your AI tool"."""
    if cli is None:
        found = available()
        cli = found[0] if found else ""
    return NAMES.get(cli, "your AI tool")


def model_for(cli: str, model: str | None, which: str = "") -> str | None:
    """The model to ask for, or None for the tool's own default."""
    per = _prefs().get(cli)
    if isinstance(per, dict) and which and per.get(f"{which}_model"):
        return str(per[f"{which}_model"])
    if not model:
        return None
    if cli != "claude" and model.lower().startswith(CLAUDE_MODELS):
        return None
    return model


def describe(model: str | None, which: str = "") -> str:
    """For logs and the saved plan: "Claude (opus)", "ChatGPT", "none"."""
    found = available()
    if not found:
        return "none"
    cli = found[0]
    m = model_for(cli, model, which) or ("sonnet" if cli == "claude" else "")
    return NAMES[cli] + (f" ({m})" if m else "")


def extract_json(text: str) -> dict:
    """The JSON object in an answer that may be wrapped in prose or a code fence."""
    text = (text or "").strip()
    try:
        out = json.loads(text)
        if isinstance(out, dict):
            return out
    except ValueError:
        pass
    dec = json.JSONDecoder()
    best, i = None, text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and (best is None or end - i > best[1]):
            best = (obj, end - i)
        i = text.find("{", end)
    if best is None:
        raise AgentError("the answer held no JSON object: " + (text[:160] or "(empty)"))
    return best[0]


def missing_keys(obj: dict, schema: dict) -> list[str]:
    return [k for k in schema.get("required", []) if k not in obj]


def with_schema(prompt: str, schema: dict) -> str:
    return (prompt + "\n\nReply with ONE JSON object and nothing else: no explanation, no "
            "code fence. Do not run commands or read files; everything you need is above. "
            "The object must match this JSON Schema:\n" + json.dumps(schema))


def _explain_claude(proc) -> str:
    msg = (proc.stderr or "").strip()
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return (msg or (proc.stdout or "")[:300]) or "claude -p failed with no output"
    detail = str(payload.get("result") or payload.get("error") or msg or "").strip()
    usage = payload.get("usage") or {}
    read = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
    if not read and not payload.get("total_cost_usd"):
        return ("claude -p read no tokens -- the CLI is most likely signed out or "
                "rate-limited. Open Settings in the planner and sign in again under Your AI "
                "(or run `claude` in Terminal, then /login)."
                + (f" [{detail[:200]}]" if detail else ""))
    return detail[:300] or "claude -p failed"


def _run(cmd: list[str], what: str, timeout: int, stdin: str | None = None,
         cwd: str | None = None, env: dict | None = None):
    try:
        if stdin is None:
            return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=timeout, cwd=cwd, env=env)
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd, env=env)
    except FileNotFoundError:
        raise AgentError(f"`{cmd[0]}` is not executable") from None
    except subprocess.TimeoutExpired:
        raise AgentError(f"{what} gave no answer within {timeout}s") from None


def _claude(path: str, prompt: str, schema: dict, model: str | None, timeout: int) -> dict:
    cmd = [path, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
           "--model", model or "sonnet", "--tools", "", "--strict-mcp-config",
           "--no-session-persistence", prompt]
    # Run in an empty folder (not the planner's, which holds .env) with no tools.
    with tempfile.TemporaryDirectory(prefix="planner-ai-") as tmp:
        proc = _run(cmd, "claude -p", timeout, cwd=tmp)
    if proc.returncode != 0:
        raise AgentError(_explain_claude(proc))
    try:
        payload = json.loads(proc.stdout)
        if payload.get("is_error"):
            raise AgentError(str(payload.get("result"))[:300])
        out = payload.get("structured_output") or json.loads(payload["result"])
    except (ValueError, KeyError, TypeError) as e:
        raise AgentError(f"claude -p returned something unparseable: {e}") from None
    if not isinstance(out, dict):
        raise AgentError("claude -p returned JSON that is not an object")
    return out


def _failed(cli: str, proc) -> AgentError:
    """Why a tool exited badly, with the fix when it looks like a sign-in."""
    tail = ((proc.stderr or "").strip() or (proc.stdout or "").strip())[-300:]
    low = tail.lower()
    name = NAMES.get(cli, cli)
    if "denied by policy" in low:
        return AgentError(f"{name} is signed in, but this account has no plan turned on for "
                          f"it yet. For Copilot, finish activating Copilot Student (or "
                          f"Copilot Free) at github.com/settings/copilot. [{tail[-160:]}]")
    if any(w in low for w in ("log in", "login", "sign in", "not authenticated",
                              "unauthorized", "401", "auth")):
        return AgentError(f"{name} is not signed in. Open Settings in the planner and "
                          f"sign in under Your AI (or run `{SIGN_IN.get(cli, cli)}` in "
                          f"Terminal). [{tail[-160:]}]")
    if any(w in low for w in ("quota", "rate limit", "429", "usage limit", "exhausted")):
        return AgentError(f"{name} has hit its usage limit for now; try again later. "
                          f"[{tail[-160:]}]")
    return AgentError(f"{name} failed (exit {proc.returncode}): {tail or 'no output'}")


def _codex(path: str, prompt: str, schema: dict, model: str | None, timeout: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="planner-ai-") as tmp:
        answer = Path(tmp) / "answer.txt"
        (Path(tmp) / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
        cmd = [path, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
               "--ephemeral", "--color", "never", "-c", 'web_search="disabled"',
               "--output-schema", str(Path(tmp) / "schema.json"),
               "--output-last-message", str(answer)]
        if model:
            cmd += ["--model", model]
        proc = _run(cmd + ["-"], "codex exec", timeout, stdin=with_schema(prompt, schema),
                    cwd=tmp)
        if proc.returncode != 0:
            raise _failed("codex", proc)
        text = answer.read_text(encoding="utf-8") if answer.exists() else proc.stdout
    return extract_json(text)


def _copilot(path: str, prompt: str, schema: dict, model: str | None, timeout: int) -> dict:
    # A GitHub token in the environment would override the Copilot sign-in.
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    with tempfile.TemporaryDirectory(prefix="planner-ai-") as tmp:
        cmd = [path, "-s", "--no-ask-user", "--no-custom-instructions",
               "--disable-builtin-mcps", "--no-auto-update",
               "--deny-tool=shell", "--deny-tool=write", "--deny-tool=url"]
        if model:
            cmd.append(f"--model={model}")
        proc = _run(cmd, "copilot", timeout, stdin=with_schema(prompt, schema), cwd=tmp,
                    env=env)
    if proc.returncode != 0:
        raise _failed("copilot", proc)
    return extract_json(proc.stdout)


def run_json(prompt: str, schema: dict, model: str | None = None, timeout: int = 300,
             which: str = "") -> dict:
    """Run one prompt and return the schema-shaped object."""
    cli, path = chosen()
    run = {"claude": _claude, "codex": _codex, "copilot": _copilot}[cli]
    out = run(path, prompt, schema, model_for(cli, model, which), timeout)
    gone = missing_keys(out, schema)
    if gone:
        raise AgentError(f"{label(cli)} answered without: {', '.join(gone)}")
    return out
