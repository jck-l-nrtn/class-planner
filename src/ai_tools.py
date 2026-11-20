"""Getting an AI tool onto the computer and signed in, for setup."""
from __future__ import annotations

import json
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import agent, config

TOOLS: dict[str, dict] = {
    "claude": {
        "name": "Claude",
        "logo": "img/claude.svg",
        "tiers": [["paid", "Paid"]],
        "needs": "A Claude Pro or Max plan.",
        "install": "curl -fsSL https://claude.ai/install.sh | bash",
        "signin": ["auth", "login"],
        "signout": ["auth", "logout"],
        "signin_help": "Your browser opens the Claude sign-in page. Sign in and approve.",
    },
    "codex": {
        "name": "ChatGPT",
        "logo": "img/chatgpt.svg",
        "tiers": [["paid", "Paid"], ["free", "Free with a school account"]],
        "needs": "ChatGPT Plus or higher, or a ChatGPT Edu account from your school.",
        "install": "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh",
        "signin": ["login"],
        "signout": ["logout"],
        "signin_help": "Your browser opens the ChatGPT sign-in page. Sign in and approve.",
    },
    "copilot": {
        "name": "GitHub Copilot",
        "logo": "img/copilot.svg",
        "tiers": [["free", "Free for students"]],
        "needs": "A GitHub account with Copilot Student, which is free for students.",
        "install": "curl -fsSL https://gh.io/copilot-install | bash",
        "signin": ["login"],
        "signin_help": "Your browser opens GitHub. Sign in and click Authorize.",
        "account": {
            "title": "First, get the free student plan",
            "steps": [
                ["Make a GitHub account if you don't have one.", "https://github.com/signup"],
                ["Open your GitHub education benefits. If you aren't a verified student yet, "
                 "click Start an application and finish it with your school email.",
                 "https://github.com/settings/education/benefits"],
                ["On that same page, under \"Free GitHub developer resources for students "
                 "and teachers\", click Learn more and follow the prompts to turn on "
                 "Copilot Student.", ""],
            ],
            "notes": [
                "GitHub can take a few days to approve a student. Until then your account "
                "has Copilot Free, which also works here but with a smaller monthly "
                "allowance. If GitHub shows only paid options, don't buy anything; wait "
                "and try again.",
                "GitHub doesn't publish how much the free allowance is. If it runs out, "
                "plans stop until the first of the next month.",
                "On Copilot Free, GitHub may use what is sent to train its models. You can "
                "turn that off under Privacy in your Copilot settings on GitHub.",
            ],
            "help": "https://docs.github.com/en/copilot/how-tos/copilot-on-github/"
                    "set-up-copilot/enable-copilot/set-up-for-students",
        },
    },
}

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_signed: dict[str, tuple[float, bool | None]] = {}
SIGNED_TTL = 3.0


def saved_choice() -> str:
    """`agent.cli` from prefs.local.yaml: a tool name, "none", or "" (never chosen)."""
    try:
        own = config._load("prefs.local.yaml", optional=True) or {}
    except Exception:
        return ""
    return str(((own.get("agent") or {}).get("cli")) or "").lower()


def save_choice(cli: str) -> None:
    import yaml
    path = config.CONFIG_DIR / "prefs.local.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    own = (yaml.safe_load(text) if text.strip() else None) or {}
    if not isinstance(own, dict):
        raise ValueError("config/prefs.local.yaml is not a list of settings")
    if (own.get("agent") or {}).get("cli") == cli:
        return
    if "agent" not in own:
        if not text.strip():
            text = "# Your own settings. These win over config/prefs.yaml and survive updates.\n"
        new = text.rstrip("\n") + f"\n\n# Which AI tool writes the plan (chosen in setup).\nagent:\n  cli: {cli}\n"
    else:
        bak = path.with_name(f"prefs.local.yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        bak.write_text(text, encoding="utf-8")
        own["agent"] = {**(own.get("agent") or {}), "cli": cli}
        new = yaml.safe_dump(own, sort_keys=False, allow_unicode=True)
    config.write_atomic(path, new)


def _quiet(cmd: list[str], timeout: int = 20):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout)


def _check_signed_in(cli: str, path: str) -> bool | None:
    """True, False, or None when the tool gives no way to tell."""
    try:
        if cli == "claude":
            p = _quiet([path, "auth", "status", "--json"])
            try:
                return bool(json.loads(p.stdout).get("loggedIn"))
            except ValueError:
                return None
        if cli == "codex":
            return _quiet([path, "login", "status"]).returncode == 0
        if cli == "copilot":
            return copilot_signed_in()
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def copilot_signed_in() -> bool | None:
    """Copilot has no status command."""
    if sys.platform != "darwin":
        return None
    found = _quiet(["/usr/bin/security", "find-generic-password", "-s", "copilot-cli"])
    if found.returncode == 0:
        return True
    return None if (Path.home() / ".copilot" / "config.json").is_file() else False


def signed_in(cli: str, path: str | None = None, fresh: bool = False) -> bool | None:
    path = path or agent.find(cli)
    if not path:
        return False
    now = time.time()
    with _lock:
        at, answer = _signed.get(cli, (0.0, None))
    if not fresh and now - at < SIGNED_TTL:
        return answer
    answer = _check_signed_in(cli, path)
    with _lock:
        _signed[cli] = (now, answer)
    return answer


def status() -> dict:
    """Everything the setup page shows about the AI tools."""
    choice = saved_choice()
    tools = []
    for cli, t in TOOLS.items():
        path = agent.find(cli)
        with _lock:
            job = dict(_jobs.get(cli) or {"status": "idle", "detail": ""})
        if "started" in job:
            job["elapsed"] = int(time.time() - job.pop("started"))
        tools.append({"cli": cli, "name": t["name"], "needs": t["needs"],
                      "logo": t["logo"], "tiers": t["tiers"],
                      "account": t.get("account"), "can_switch": bool(t.get("signout")),
                      "installed": bool(path),
                      "signed_in": signed_in(cli, path) if path else False,
                      "install": job, "signin_help": t["signin_help"],
                      "manual": manual_steps(cli)})
    if choice not in TOOLS and choice != "none":
        ready = [t["cli"] for t in tools if t["installed"] and t["signed_in"] is not False]
        there = [t["cli"] for t in tools if t["installed"]]
        choice = (ready or there or [""])[0]
    return {"choice": choice, "tools": tools}


def manual_steps(cli: str) -> list[str]:
    """What to paste into Terminal to do it by hand."""
    t = TOOLS[cli]
    return [t["install"].replace("CODEX_NON_INTERACTIVE=1 ", ""),
            " ".join([cli, *t["signin"]])]


def how_to_get_text(indent: str = "  ") -> str:
    lines = ["Open the planner (./plan app) and go to /setup#ai to choose and install one, "
             "or do it by hand in Terminal:"]
    for cli, t in TOOLS.items():
        lines.append(f"{indent}{t['name']}: " + "   then: ".join(manual_steps(cli))
                     + f"   ({t['needs']})")
    return "\n".join(lines)


def _set_job(cli: str, status: str, detail: str = "") -> None:
    with _lock:
        _jobs[cli] = {"status": status, "detail": detail[-400:]}


def _run_install(cli: str) -> None:
    try:
        p = subprocess.run(["/bin/bash", "-c", TOOLS[cli]["install"]],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=900, cwd=str(Path.home()), start_new_session=True)
        ok = p.returncode == 0
        detail = ((p.stderr or "") + (p.stdout or "")).strip()
        if ok and not agent.find(cli):
            ok, detail = False, ("the installer finished but the `%s` command was not "
                                 "found afterwards. %s" % (cli, detail[-200:]))
    except subprocess.TimeoutExpired:
        ok, detail = False, "the installer did not finish within 15 minutes"
    except OSError as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    _set_job(cli, "ok" if ok else "failed", "" if ok else detail)


def install(cli: str) -> bool:
    """Start installing in the background. False if it was already running."""
    if cli not in TOOLS:
        raise ValueError(f"unknown tool: {cli}")
    with _lock:
        if (_jobs.get(cli) or {}).get("status") == "running":
            return False
        _jobs[cli] = {"status": "running", "detail": "", "started": time.time()}
    threading.Thread(target=_run_install, args=(cli,), daemon=True).start()
    return True


def sign_in_command(cli: str) -> list[str]:
    path = agent.find(cli)
    if not path:
        raise RuntimeError(f"{TOOLS[cli]['name']} is not installed yet")
    return [path, *TOOLS[cli]["signin"]]


def sign_out(cli: str) -> None:
    """Sign the tool out of the account it is using, so another can be used."""
    path = agent.find(cli)
    if not path:
        raise RuntimeError(f"{TOOLS[cli]['name']} is not installed yet")
    if not TOOLS[cli].get("signout"):
        raise RuntimeError(f"{TOOLS[cli]['name']} has no sign-out command. Run `{cli}` in "
                           f"Terminal and type /logout, then sign in again here.")
    try:
        _quiet([path, *TOOLS[cli]["signout"]], timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"could not sign {TOOLS[cli]['name']} out: {e}") from None
    with _lock:
        _signed.pop(cli, None)
    if signed_in(cli, path, fresh=True) is True:
        raise RuntimeError(f"{TOOLS[cli]['name']} still says it is signed in. In Terminal, "
                           f"run: {cli} {' '.join(TOOLS[cli]['signout'])}")


def open_sign_in(cli: str, switch: bool = False) -> str:
    """Open a Terminal window running the tool's sign-in."""
    if switch:
        sign_out(cli)
    cmd = sign_in_command(cli)
    line = " ".join(shlex.quote(c) for c in cmd)
    with _lock:
        _signed.pop(cli, None)
    if sys.platform != "darwin":
        return line
    name = TOOLS[cli]["name"]
    script = Path(tempfile.gettempdir()) / f"Sign in to {name}.command"
    script.write_text(
        "#!/bin/bash\nrm -f \"$0\"   # used once; bash keeps reading it after this\nclear\n"
        f"echo 'Signing in to {name} for Learning Cheat.'\n"
        f"echo '{TOOLS[cli]['signin_help']}'\n"
        "echo 'When it says you are signed in, close this window and go back to the setup page.'\n"
        f"echo\ncd \"$HOME\"\n{line}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    try:
        subprocess.run(["/usr/bin/open", str(script)], check=True, timeout=20,
                       stdin=subprocess.DEVNULL, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return line
    return ""
