"""src/updater.py: an installed copy updating itself."""
from __future__ import annotations

import io
import json
import os
import tarfile
import time

import pytest

from src import config, updater

OLD, NEW = "a" * 40, "b" * 40


@pytest.fixture
def inst(tmp_path, monkeypatch):
    root = tmp_path / "install"
    for rel, text in {"plan": "#!/bin/sh\n", "src/cli.py": "old cli\n", "src/gone.py": "x\n",
                      "requirements.txt": "requests\n", "config/prefs.yaml": "a: 1\n",
                      ".env": "CANVAS_TOKEN=secret\n", "config/courses.yaml": "mine\n",
                      "config/prefs.local.yaml": "mine\n", "data/store.json": "{}\n"}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / ".version").write_text(json.dumps(
        {"sha": OLD, "files": ["plan", "src/cli.py", "src/gone.py", "requirements.txt",
                               "config/prefs.yaml"]}))
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(config, "CONFIG_DIR", root / "config")
    monkeypatch.setattr(config, "DATA_DIR", root / "data")
    monkeypatch.delenv("PLANNER_NO_UPDATE", raising=False)
    return root


def new_tree(tmp_path, files=None):
    tree = tmp_path / "dl" / "class-planner-new"
    files = files or {"plan": "#!/bin/sh\necho new\n", "src/cli.py": "new cli\n",
                      "src/added.py": "y\n", "requirements.txt": "requests\n",
                      "config/prefs.yaml": "a: 2\n",
                      ".env": "CANVAS_TOKEN=evil\n", "config/courses.yaml": "theirs\n",
                      "data/store.json": "theirs\n"}
    for rel, text in files.items():
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    os.chmod(tree / "plan", 0o755)
    return tree


@pytest.fixture
def github(inst, tmp_path, monkeypatch):
    calls = {"download": 0, "pip": 0}
    tree = new_tree(tmp_path)
    monkeypatch.setattr(updater, "latest", lambda timeout=6: NEW)

    def download(sha, into, timeout=60):
        calls["download"] += 1
        return tree
    monkeypatch.setattr(updater, "download", download)
    monkeypatch.setattr(updater, "smoke", lambda t: (True, ""))
    monkeypatch.setattr(updater, "install_requirements",
                        lambda t, log: calls.__setitem__("pip", calls["pip"] + 1) or True)
    return calls


def test_update_replaces_program_files_and_nothing_of_yours(inst, github):
    said = []
    assert updater.maybe_update(force=True, log=said.append) is True
    assert (inst / "src/cli.py").read_text() == "new cli\n"
    assert (inst / "src/added.py").exists()
    assert not (inst / "src/gone.py").exists()
    assert os.access(inst / "plan", os.X_OK)
    assert (inst / ".env").read_text() == "CANVAS_TOKEN=secret\n"
    assert (inst / "config/courses.yaml").read_text() == "mine\n"
    assert (inst / "config/prefs.local.yaml").read_text() == "mine\n"
    assert (inst / "data/store.json").read_text() == "{}\n"
    v = json.loads((inst / ".version").read_text())
    assert v["sha"] == NEW and ".env" not in v["files"] and "src/added.py" in v["files"]
    assert not list(inst.rglob("*.updating"))
    assert any("not touched" in s for s in said)


def test_same_version_does_nothing(inst, github, monkeypatch):
    monkeypatch.setattr(updater, "latest", lambda timeout=6: OLD)
    assert updater.maybe_update(force=True, log=lambda *_: None) is False
    assert github["download"] == 0 and (inst / "src/cli.py").read_text() == "old cli\n"


def test_version_that_does_not_start_is_not_installed(inst, github, monkeypatch):
    monkeypatch.setattr(updater, "smoke", lambda t: (False, "SyntaxError: invalid syntax"))
    said = []
    assert updater.maybe_update(force=True, log=said.append) is False
    assert (inst / "src/cli.py").read_text() == "old cli\n"
    assert json.loads((inst / ".version").read_text())["sha"] == OLD
    assert any("did not start" in s for s in said)


def test_failed_package_install_stops_the_update(inst, github, monkeypatch):
    monkeypatch.setattr(updater, "install_requirements", lambda t, log: False)
    assert updater.maybe_update(force=True, log=lambda *_: None) is False
    assert (inst / "src/cli.py").read_text() == "old cli\n"


def test_offline_is_quiet_and_harmless(inst, github, monkeypatch):
    def boom(timeout=6):
        raise ConnectionError("no network")
    monkeypatch.setattr(updater, "latest", boom)
    said = []
    assert updater.maybe_update(log=said.append) is False and said == []
    assert updater.maybe_update(force=True, log=said.append) is False
    assert "Could not update" in said[0]


def test_git_checkout_never_updates_itself(inst, github):
    (inst / ".git").mkdir()
    said = []
    assert updater.maybe_update(force=True, log=said.append) is False
    assert github["download"] == 0 and "git checkout" in said[0]


def test_can_be_switched_off(inst, github, monkeypatch):
    (inst / "config/prefs.local.yaml").write_text("updates:\n  auto: false\n")
    assert updater.maybe_update(force=True, log=lambda *_: None) is False
    (inst / "config/prefs.local.yaml").write_text("mine: 1\n")
    monkeypatch.setenv("PLANNER_NO_UPDATE", "1")
    assert updater.maybe_update(force=True, log=lambda *_: None) is False
    assert github["download"] == 0


def test_checks_at_most_once_an_hour_unless_forced(inst, github, monkeypatch):
    monkeypatch.setattr(updater, "latest", lambda timeout=6: OLD)
    asked = []
    monkeypatch.setattr(updater, "latest", lambda timeout=6: asked.append(1) or OLD)
    updater.maybe_update(log=lambda *_: None)
    updater.maybe_update(log=lambda *_: None)
    assert len(asked) == 1
    updater.maybe_update(force=True, log=lambda *_: None)
    assert len(asked) == 2
    assert not updater.due(time.time()) and updater.due(time.time() + updater.CHECK_EVERY + 1)


def test_first_update_of_an_install_with_no_version_file(inst, github):
    (inst / ".version").unlink()
    assert updater.maybe_update(log=lambda *_: None) is True
    assert json.loads((inst / ".version").read_text())["sha"] == NEW
    assert (inst / "src/gone.py").exists()


@pytest.mark.parametrize("rel,want", [
    (".env", True), ("data/store.json", True), ("config/courses.yaml", True),
    ("config/prefs.local.yaml", True), ("config/courses.yaml.bak-20261118-101010", True),
    (".venv/bin/python", True), (".version", True), ("logs/daily.log", True),
    ("config/prefs.yaml", False), ("config/examples/courses.yaml", False),
    ("src/cli.py", False), ("plan", False), ("database.py", False), ("datatools/x.py", False),
])
def test_protected(rel, want):
    assert updater.protected(rel) is want


class FakeResponse:
    def __init__(self, body=b"", text=""):
        self.body, self.text = body, text

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        yield self.body


def tarball(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in entries:
            if data is None:
                info = tarfile.TarInfo(name)
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(info)
            else:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_download_unpacks_a_real_archive(tmp_path, monkeypatch):
    ball = tarball([("class-planner-bbb/src/cli.py", b"cli"), ("class-planner-bbb/plan", b"p")])
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(ball))
    tree = updater.download(NEW, tmp_path)
    assert updater.files_in(tree) == ["plan", "src/cli.py"]


@pytest.mark.parametrize("entries", [
    [("class-planner-bbb/src/cli.py", b"x"), ("../outside.txt", b"x")],
    [("class-planner-bbb/src/cli.py", b"x"), ("/abs/path", b"x")],
    [("class-planner-bbb/src/cli.py", b"x"), ("class-planner-bbb/link", None)],
    [("something-else/readme", b"x")],
])
def test_download_refuses_anything_odd(tmp_path, monkeypatch, entries):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(tarball(entries)))
    with pytest.raises(ValueError):
        updater.download(NEW, tmp_path)
    assert not (tmp_path.parent / "outside.txt").exists()


def test_latest_rejects_anything_that_is_not_a_commit_id(monkeypatch):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(text=NEW + "\n"))
    assert updater.latest() == NEW
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(text="<html>rate limited"))
    with pytest.raises(ValueError):
        updater.latest()


def test_smoke_catches_a_version_that_cannot_import(tmp_path):
    good = tmp_path / "good"
    (good / "src").mkdir(parents=True)
    bad = tmp_path / "bad" / "src"
    bad.mkdir(parents=True)
    (bad / "__init__.py").write_text("")
    (bad / "cli.py").write_text("def broken(:\n")
    ok, detail = updater.smoke(tmp_path / "bad")
    assert not ok and "SyntaxError" in detail
    assert updater.smoke(config.ROOT)[0]


def test_bytecode_and_test_folders_are_never_installed(tmp_path):
    tree = new_tree(tmp_path, {"plan": "p\n", "src/cli.py": "c\n",
                               "src/__pycache__/cli.cpython-312.pyc": "junk",
                               "_d/store.json": "{}", "_o/day.json": "{}", "stray.pyc": "junk"})
    assert updater.files_in(tree) == ["plan", "src/cli.py"]
