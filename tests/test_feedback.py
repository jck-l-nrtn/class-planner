from __future__ import annotations

from urllib.parse import unquote

from src import feedback


def test_no_address_means_no_links(monkeypatch):
    monkeypatch.setattr(feedback, "load_prefs", lambda: {})
    assert feedback.links() == {"email": "", "bug": "", "idea": ""}


def test_links_are_ready_made_emails_with_the_details_line(monkeypatch):
    monkeypatch.setattr(feedback, "load_prefs", lambda: {"feedback_email": "me@example.com"})
    monkeypatch.setattr(feedback, "details", lambda: "version abc1234 · macOS 26.1 arm64")
    r = feedback.links()
    assert r["email"] == "me@example.com"
    assert r["bug"].startswith("mailto:me@example.com?subject=Learning%20Cheat%20bug")
    assert "What happened?" in unquote(r["bug"])
    assert "What would you like" in unquote(r["idea"])
    for k in ("bug", "idea"):
        assert unquote(r[k]).endswith("---\nversion abc1234 · macOS 26.1 arm64\n")


def test_details_names_no_course(monkeypatch):
    class Cfg:
        prefs = {"agent": {"cli": "claude"}}
        def env(self, k):
            return "secret-token" if k == "CANVAS_TOKEN" else ""
        def ls_course_ids(self):
            return {"CHEM 105": "abc"}
    monkeypatch.setattr("src.config.Config", Cfg)
    d = feedback.details()
    assert "AI claude" in d and "Canvas on" in d and "LearningSuite on" in d
    assert "CHEM" not in d and "secret" not in d


def test_details_before_setup(monkeypatch):
    def boom():
        raise FileNotFoundError("courses.yaml")
    monkeypatch.setattr("src.config.Config", boom)
    assert "not set up yet" in feedback.details()
