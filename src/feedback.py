"""Report a bug / Request a feature: ready-made emails to the address in prefs `feedback_email`."""
from __future__ import annotations

import platform
import subprocess
from urllib.parse import quote

from .config import ROOT, load_prefs

BUG = """What happened?


What did you expect to happen instead?


What were you doing just before? (Screenshots help: attach them to this email.)


"""
IDEA = """What would you like the planner to do?


How would it help you?


"""


def _version() -> str:
    from . import updater
    sha = updater.installed().get("sha", "")
    if not sha and (ROOT / ".git").exists():
        try:
            sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            sha = ""
    return sha[:7] or "unknown"


def details() -> str:
    """One line about this copy; nothing personal."""
    parts = [f"version {_version()}"]
    mac = platform.mac_ver()[0]
    parts.append(f"macOS {mac} {platform.machine()}" if mac
                 else f"{platform.system()} {platform.machine()}")
    parts.append(f"Python {platform.python_version()}")
    try:
        from .config import Config
        cfg = Config()
        parts.append(f"AI {(cfg.prefs.get('agent') or {}).get('cli', 'auto')}")
        parts.append("Canvas " + ("on" if cfg.env("CANVAS_TOKEN") else "off"))
        parts.append("LearningSuite " + ("on" if cfg.ls_course_ids() else "off"))
    except Exception:
        parts.append("not set up yet")
    return " · ".join(parts)


def _mailto(to: str, subject: str, body: str) -> str:
    return f"mailto:{to}?subject={quote(subject)}&body={quote(body)}"


def links() -> dict:
    """{email, bug, idea}; empty when no feedback address is set."""
    to = str(load_prefs().get("feedback_email") or "").strip()
    if not to:
        return {"email": "", "bug": "", "idea": ""}
    tail = f"---\n{details()}\n"
    return {"email": to,
            "bug": _mailto(to, "Learning Cheat bug: ", BUG + tail),
            "idea": _mailto(to, "Learning Cheat idea: ", IDEA + tail)}
