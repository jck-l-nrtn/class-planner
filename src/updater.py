"""Keep an installed copy current with the published one, by itself."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import requests

from . import config

REPO = "jck-l-nrtn/learning-cheat"
BRANCH = "main"
CHECK_EVERY = 3600
# Never written or removed by an update.
PROTECTED = (".env", ".version", ".venv", ".tools", ".git", "data", "out", "logs",
             "config/courses.yaml", "config/commitments.yaml",
             "config/manual_assignments.yaml", "config/prefs.local.yaml")


def _version_file() -> Path:
    return config.ROOT / ".version"


def _check_file() -> Path:
    return config.DATA_DIR / ".update-check"


def protected(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return any(rel == p or rel.startswith(p + "/") for p in PROTECTED) \
        or ".bak-" in rel


def installed() -> dict:
    try:
        return json.loads(_version_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def enabled() -> tuple[bool, str]:
    """(may this copy update itself?, why not)."""
    if (config.ROOT / ".git").exists():
        return False, "this is a git checkout; update it with git"
    if os.environ.get("PLANNER_NO_UPDATE"):
        return False, "PLANNER_NO_UPDATE is set"
    try:
        prefs = config.load_prefs()
    except Exception:
        prefs = {}
    if (prefs.get("updates") or {}).get("auto") is False:
        return False, "updates.auto is false in prefs"
    return True, ""


def due(now: float | None = None) -> bool:
    try:
        last = float(_check_file().read_text())
    except (OSError, ValueError):
        return True
    return (now or time.time()) - last >= CHECK_EVERY


def _mark_checked() -> None:
    try:
        _check_file().parent.mkdir(parents=True, exist_ok=True)
        _check_file().write_text(str(time.time()))
    except OSError:
        pass


def latest(timeout: int = 6) -> str:
    """The newest published commit id. Raises on any network trouble."""
    r = requests.get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
                     headers={"Accept": "application/vnd.github.sha"}, timeout=timeout)
    r.raise_for_status()
    sha = r.text.strip()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("GitHub did not return a commit id")
    return sha


def download(sha: str, into: Path, timeout: int = 60) -> Path:
    """Unpack that commit under `into`; returns the folder holding its files."""
    r = requests.get(f"https://github.com/{REPO}/archive/{sha}.tar.gz", timeout=timeout,
                     stream=True)
    r.raise_for_status()
    ball = into / "update.tar.gz"
    with ball.open("wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    out = into / "tree"
    out.mkdir()
    with tarfile.open(ball) as tar:
        for m in tar.getmembers():
            p = Path(m.name)
            if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
                raise ValueError(f"refusing odd entry in the download: {m.name}")
        tar.extractall(out)
    tops = [p for p in out.iterdir() if p.is_dir()]
    if len(tops) != 1 or not (tops[0] / "src" / "cli.py").is_file():
        raise ValueError("the download is not a copy of the planner")
    return tops[0]


def files_in(tree: Path) -> list[str]:
    out = []
    for p in tree.rglob("*"):
        rel = p.relative_to(tree)
        if p.is_file() and "__pycache__" not in rel.parts and rel.parts[0] not in ("_d", "_o") \
                and p.suffix != ".pyc":
            out.append(str(rel).replace(os.sep, "/"))
    return sorted(out)


def smoke(tree: Path) -> tuple[bool, str]:
    """Does the new version at least import?"""
    env = {**os.environ, "PLANNER_DATA_DIR": str(tree / "_d"),
           "PLANNER_OUT_DIR": str(tree / "_o"), "PLANNER_NO_UPDATE": "1"}
    try:
        p = subprocess.run(
            [sys.executable, "-B", "-c", "import src.cli, src.web.server, src.setup_wizard, "
                                   "src.sync, src.daily, src.estimate, src.ls.refresh, "
                                   "src.agent, src.ai_tools"],
            cwd=str(tree), env=env, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"
    return p.returncode == 0, (p.stderr or p.stdout or "").strip()[-400:]


def install_requirements(tree: Path, log) -> bool:
    """New packages first, so the new code never runs without them."""
    new = tree / "requirements.txt"
    old = config.ROOT / "requirements.txt"
    if not new.exists() or (old.exists() and old.read_bytes() == new.read_bytes()):
        return True
    log("  installing updated packages...")
    env = dict(os.environ)
    if (config.ROOT / ".tools" / "browsers").is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(config.ROOT / ".tools" / "browsers")
    for cmd in (["-m", "pip", "install", "--quiet", "-r", str(new)],
                ["-m", "playwright", "install", "chromium"]):
        try:
            p = subprocess.run([sys.executable, *cmd], capture_output=True, text=True,
                               timeout=900, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"  could not install packages: {e}")
            return False
        if p.returncode != 0:
            log(f"  could not install packages: {(p.stderr or '').strip()[-300:]}")
            return False
    return True


def apply(tree: Path, sha: str) -> tuple[int, int]:
    root = config.ROOT
    new = [f for f in files_in(tree) if not protected(f)]
    written = 0
    for rel in new:
        src, dst = tree / rel, root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".updating")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        written += 1
    removed = _remove_dropped(new)
    record(sha, new)
    return written, removed


def _remove_dropped(new: list[str]) -> int:
    """Delete files the installed version had and `new` no longer has."""
    removed = 0
    for rel in installed().get("files", []):
        if rel not in new and not protected(rel):
            try:
                (config.ROOT / rel).unlink()
                removed += 1
            except OSError:
                pass
    return removed


def record(sha: str, files: list[str]) -> None:
    """Remember which version is installed and which files it brought."""
    config.write_atomic(_version_file(), json.dumps(
        {"sha": sha, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "files": files}))


def maybe_update(force: bool = False, log=print) -> bool:
    """Update if there is something newer."""
    ok, why = enabled()
    if not ok:
        if force:
            log(f"Not updating: {why}.")
        return False
    if not force and not due():
        return False
    try:
        return _update(force, log)
    except Exception as e:
        if force:
            log(f"Could not update: {type(e).__name__}: {str(e)[:200]}")
        return False


def _update(force: bool, log) -> bool:
    from .store import _lock, _unlock
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.DATA_DIR / ".update.lock", "a+") as lock:
        _lock(lock)
        try:
            _mark_checked()
            sha = latest()
            if installed().get("sha") == sha:
                if force:
                    log(f"Already the newest version ({sha[:7]}).")
                return False
            log(f"Updating the planner to the newest version ({sha[:7]})...")
            with tempfile.TemporaryDirectory(prefix="planner-update-") as tmp:
                tree = download(sha, Path(tmp))
                if not install_requirements(tree, log):
                    return False
                good, detail = smoke(tree)
                if not good:
                    log(f"  the new version did not start, so it was not installed: "
                        f"{detail[-200:]}")
                    return False
                written, removed = apply(tree, sha)
            log(f"  updated ({written} files" + (f", {removed} removed" if removed else "")
                + "). Your classes and data were not touched.")
            return True
        finally:
            _unlock(lock)


def restart(argv: list[str] | None = None) -> None:
    """Start the same command again on the new code."""
    os.environ["PLANNER_UPDATED"] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(config.ROOT)
    os.execv(sys.executable, [sys.executable, "-m", "src.cli",
                              *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 4 and sys.argv[1] == "record":
        files = [f for f in files_in(Path(sys.argv[3])) if not protected(f)]
        _remove_dropped(files)
        record(sys.argv[2], files)
