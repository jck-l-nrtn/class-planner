"""src/setup_wizard.py: the helpers are pure; the conversation runs scripted."""
from __future__ import annotations

from datetime import date

import pytest
import yaml

from src import config, setup_wizard as w


@pytest.mark.parametrize("text,key", [
    ("BIO 100-002", "BIO100"),
    ("BIO 100: Principles of Biology", "BIO100"),
    ("REL A 275 (012)  - World Religions", "RELA275"),
    ("SPAN 105 (002)  - Section 002", "SPAN105"),
    ("C S 142 - Intro to Programming", "CS142"),
    ("WRTG 150H Honors Writing", "WRTG150H"),
    ("Intro to BIO 100", "BIO100"),
    ("Chinese Conversation", "CHINCONV"),
    ("", "COURSE"),
])
def test_course_key(text, key):
    assert w.course_key(text) == key


def test_course_key_is_unique():
    assert w.course_key("BIO 100-002", {"BIO100"}) == "BIO100_2"
    assert w.course_key("BIO 100-002", {"BIO100", "BIO100_2"}) == "BIO100_3"


@pytest.mark.parametrize("text,want", [
    ("MWF 9:00-9:50", (["mon", "wed", "fri"], "09:00", "09:50")),
    ("TTh 12:30-1:45", (["tue", "thu"], "12:30", "13:45")),
    ("TTh 12:30-1:45pm", (["tue", "thu"], "12:30", "13:45")),
    ("MW 11-12:15", (["mon", "wed"], "11:00", "12:15")),
    ("mon wed 3:30 - 4:45", (["mon", "wed"], "15:30", "16:45")),
    ("Tuesday Thursday 15:30-16:45", (["tue", "thu"], "15:30", "16:45")),
    ("MTWThF 9-9:50am", (["mon", "tue", "wed", "thu", "fri"], "09:00", "09:50")),
    ("F 1-2:15pm", (["fri"], "13:00", "14:15")),
    ("R 8:00-9:15", (["thu"], "08:00", "09:15")),
    ("T 6pm-8:30pm", (["tue"], "18:00", "20:30")),
])
def test_parse_meeting(text, want):
    assert w.parse_meeting(text) == want


@pytest.mark.parametrize("text", ["", "whenever", "9:00-9:50", "MWF", "MWF 10:00-9:00am",
                                  "MWF 25:00-26:00"])
def test_parse_meeting_rejects(text):
    assert w.parse_meeting(text) is None


def test_parse_picks():
    assert w.parse_picks("", 3) == [0, 1, 2]
    assert w.parse_picks("all", 3) == [0, 1, 2]
    assert w.parse_picks("1, 3", 3) == [0, 2]
    assert w.parse_picks("3 1 1", 3) == [0, 2]
    assert w.parse_picks("none", 3) == []
    assert w.parse_picks("4", 3) is None
    assert w.parse_picks("two", 3) is None


CANVAS = [{"canvas_id": 111, "name": "BIO 100: Principles of Biology",
           "code": "BIO 100-002", "term": "", "start": None, "end": None}]
LS = [{"id": "aaa", "title": "SPAN 105 (002)  - Section 002", "term": "Fall 2026"},
      {"id": "bbb", "title": "REL A 275 (012)  - World Religions", "term": "Fall 2026"}]


def test_merge_courses_one_row_each():
    rows = w.merge_courses(CANVAS, LS)
    assert [(r["key"], r["canvas_id"], r["ls_course_id"]) for r in rows] == [
        ("BIO100", 111, None), ("SPAN105", None, "aaa"), ("RELA275", None, "bbb")]
    assert rows[1]["name"] == "SPAN 105 (002) - Section 002"


def test_merge_courses_same_course_on_both_sites_is_one_course():
    rows = w.merge_courses(CANVAS, [{"id": "zzz", "title": "BIO 100 (002) - Intro", "term": "F"}])
    assert len(rows) == 1 and rows[0]["canvas_id"] == 111 and rows[0]["ls_course_id"] == "zzz"


def test_current_term_keeps_only_the_newest():
    ls = [{"id": "a", "title": "x", "term": "Fall 2026"}, {"id": "b", "title": "y", "term": "Fall 2026"},
          {"id": "c", "title": "z", "term": "Summer 2026"}]
    assert [c["id"] for c in w.current_term(ls)] == ["a", "b"]
    assert w.current_term([]) == []


def test_guess_term():
    assert w.guess_term(date(2026, 11, 18)) == (date(2026, 9, 1), date(2026, 12, 18))
    assert w.guess_term(date(2027, 2, 1))[0] == date(2027, 1, 5)


def test_write_env_keeps_other_lines_and_is_private(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nSPAN105_ICS=https://x\nCANVAS_TOKEN=old\n")
    w.write_env(p, {"CANVAS_BASE_URL": "https://c", "CANVAS_TOKEN": "new"})
    assert w.read_env(p) == {"SPAN105_ICS": "https://x", "CANVAS_TOKEN": "new",
                             "CANVAS_BASE_URL": "https://c"}
    assert p.read_text().startswith("# comment\n")
    assert (p.stat().st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


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
    return tmp_path


def test_config_without_setup_says_run_setup(home):
    with pytest.raises(config.SetupNeeded, match=r"\./plan setup"):
        config.Config()


def test_prefs_local_overrides_key_by_key(cfg, tmp_path):
    (tmp_path / "config" / "prefs.local.yaml").write_text(
        "daily_focus_cap_minutes: 120\npriority:\n  lookahead_days: 3\n")
    c = config.Config()
    assert c.prefs["daily_focus_cap_minutes"] == 120
    assert c.prefs["priority"]["lookahead_days"] == 3
    assert c.prefs["priority"]["multipliers"] == cfg.prefs["priority"]["multipliers"]


def scripted(answers, secrets=(), clipboard=""):
    answers, secrets, said = list(answers), list(secrets), []
    clips = [clipboard] if isinstance(clipboard, str) else list(clipboard)
    wiz = w.Wizard(ask=lambda prompt: (said.append(prompt), answers.pop(0))[1],
                   secret=lambda prompt: secrets.pop(0),
                   out=lambda *a: said.append(" ".join(str(x) for x in a)),
                   today=date(2026, 11, 18),
                   clipboard=lambda: clips.pop(0) if len(clips) > 1 else clips[0])
    return wiz, said, answers


TOKEN = "7407~" + "aB3" * 22


def test_whole_setup_writes_loadable_config(home, monkeypatch):
    monkeypatch.setattr(w, "canvas_courses", lambda base, token: ("Pat Student", CANVAS))
    monkeypatch.setattr(w.Wizard, "learningsuite", lambda self: (LS, None))
    ran = []
    monkeypatch.setattr(w.Wizard, "first_run",
                        lambda self, session, uses_ls: ran.append(uses_ls) or 0)
    wiz, said, left = scripted([
        "y",
        "",
        "",
        "1,3",
        "", "MW 11-12:15",
        "2", "nonsense", "",
        "America/Denver", "", "",
        "", "",
    ], clipboard=TOKEN)
    assert wiz.run() == 0 and left == [] and ran == [True]

    assert w.read_env(home / ".env") == {"CANVAS_BASE_URL": w.DEFAULT_CANVAS,
                                         "CANVAS_TOKEN": TOKEN}
    assert TOKEN not in "\n".join(said)
    c = config.Config()
    assert list(c.courses) == ["BIO100", "RELA275"]
    assert c.canvas_map == {111: "BIO100"} and c.ls_course_ids() == {"RELA275": "bbb"}
    assert c.courses["RELA275"]["credits"] == 2
    assert c.course_defaults["effort"]["homework"] == 120
    assert c.term_bounds() == (date(2026, 9, 1), date(2026, 12, 18))
    rec = c.commitments["recurring"]
    assert rec == [{"name": "BIO100", "kind": "class", "course": "BIO100",
                    "days": ["mon", "wed"], "start": "11:00", "end": "12:15",
                    "location": "campus"}]
    from src.timeline import build_day
    assert any(b.kind == "class" for b in build_day(c, date(2026, 11, 18))[0])


def test_bad_canvas_token_is_asked_again_and_never_saved(home, monkeypatch):
    calls = []
    bad = "1~" + "x" * 40

    def canvas(base, token):
        calls.append(token)
        if token == bad:
            raise RuntimeError("Canvas rejected the token (401). Is it expired?")
        return "Pat", CANVAS
    monkeypatch.setattr(w, "canvas_courses", canvas)
    wiz, said, left = scripted(["y", "", "", ""], clipboard=[bad, TOKEN])
    assert wiz.canvas() == CANVAS and calls == [bad, TOKEN] and left == []
    assert w.read_env(home / ".env")["CANVAS_TOKEN"] == TOKEN
    assert any("did not accept" in s for s in said)


def test_token_comes_from_the_clipboard_with_no_pasting(home, monkeypatch):
    monkeypatch.setattr(w, "canvas_courses", lambda base, token: ("Pat", CANVAS))
    wiz, said, _ = scripted(["y", "", ""], clipboard=f"\nToken:\n  {TOKEN}\n")
    assert wiz.canvas() == CANVAS
    assert w.read_env(home / ".env")["CANVAS_TOKEN"] == TOKEN
    assert any("from the clipboard" in s for s in said) and TOKEN not in "\n".join(said)


def test_token_pasted_at_the_return_prompt_is_used(home, monkeypatch):
    monkeypatch.setattr(w, "canvas_courses", lambda base, token: ("Pat", CANVAS))
    wiz, _, _ = scripted(["y", "", TOKEN], clipboard="something else entirely")
    assert wiz.canvas() == CANVAS
    assert w.read_env(home / ".env")["CANVAS_TOKEN"] == TOKEN


def test_unrelated_clipboard_falls_back_to_a_hidden_paste(home, monkeypatch):
    monkeypatch.setattr(w, "canvas_courses", lambda base, token: ("Pat", CANVAS))
    wiz, said, _ = scripted(["y", "", ""], secrets=[f"\x1b[200~{TOKEN}\x1b[201~"],
                            clipboard="my grocery list")
    assert wiz.canvas() == CANVAS
    assert w.read_env(home / ".env")["CANVAS_TOKEN"] == TOKEN
    assert any("Command-V" in s for s in said)


def test_no_token_three_times_offers_to_skip_canvas(home, monkeypatch):
    monkeypatch.setattr(w, "canvas_courses", lambda *a: pytest.fail("no token to try"))
    wiz, said, left = scripted(["y", "", "", "", "", "n"], secrets=["", "", ""], clipboard="")
    assert wiz.canvas() == [] and left == []
    assert not (home / ".env").exists()


@pytest.mark.parametrize("text,want", [
    (TOKEN, TOKEN),
    (f"  {TOKEN}\n", TOKEN),
    (f"\n\nToken: {TOKEN}\nExpires: never\n", TOKEN),
    (f"\x1b[200~{TOKEN}\x1b[201~", TOKEN),
    ("selfhostedtokenwithnoshardprefix0123456789", "selfhostedtokenwithnoshardprefix0123456789"),
    ("", ""), ("\n", ""), ("two words", ""),
])
def test_clean_token(text, want):
    assert w.clean_token(text) == want


def typed(*chunks, pending=()):
    feed, shown, more = list(chunks), [], list(pending)
    out = w.assemble(lambda: feed.pop(0) if feed else "", shown.append,
                     lambda: more.pop(0) if more else False)
    return out, "".join(shown)


def test_hidden_input_shows_a_dot_per_character_never_the_text():
    out, shown = typed("abc123", "\r")
    assert out == "abc123" and shown.count("\u2022") == 6 and "abc" not in shown


def test_return_before_pasting_explains_and_keeps_waiting():
    out, shown = typed("\r", "\n", TOKEN + "\r")
    assert w.clean_token(out) == TOKEN
    assert shown.count("Nothing has arrived yet") == 1 and "Command-V" in shown


def test_control_v_is_explained_not_swallowed():
    out, shown = typed("\x16", "\r", TOKEN, "\r")
    assert w.clean_token(out) == TOKEN and "Control-V" in shown


def test_paste_that_starts_with_a_blank_line_and_arrives_in_two_pieces():
    out, shown = typed("\nToken:\n", f"{TOKEN}\n", pending=[True, False])
    assert w.clean_token(out) == TOKEN and "Nothing has arrived" not in shown


def test_multi_line_paste_arriving_in_one_piece_needs_no_second_return():
    out, shown = typed(f"\x1b[200~\nToken:\n{TOKEN}\n\x1b[201~")
    assert w.clean_token(out) == TOKEN
    assert shown.count("\u2022") == len("Token:") + len(TOKEN)
    assert "Nothing has arrived" not in shown


def test_bracketed_paste_markers_and_backspace():
    out, shown = typed(f"\x1b[200~{TOKEN}\x1b[201~", "x\x7f", "\r")
    assert w.clean_token(out) == TOKEN and "\b \b" in shown


def test_closed_input_is_an_error_not_an_empty_token():
    with pytest.raises(EOFError):
        typed("abc")


def test_existing_config_is_left_alone_unless_asked(home):
    (home / "config" / "courses.yaml").write_text("courses: []\n")
    wiz, said, _ = scripted(["n"])
    assert wiz.run() == 0
    assert (home / "config" / "courses.yaml").read_text() == "courses: []\n"


def test_save_keeps_a_backup(home):
    wiz, _, _ = scripted([])
    (home / "config" / "courses.yaml").write_text("old\n")
    wiz.save("courses.yaml", "new\n")
    assert (home / "config" / "courses.yaml").read_text() == "new\n"
    assert [p.read_text() for p in (home / "config").glob("courses.yaml.bak-*")] == ["old\n"]


def test_no_courses_anywhere_writes_nothing(home, monkeypatch):
    monkeypatch.setattr(w.Wizard, "learningsuite", lambda self: ([], None))
    wiz, _, _ = scripted(["n"])
    assert wiz.run() == 1
    assert not (home / "config" / "courses.yaml").exists()


def test_check_reports_a_fresh_install(home):
    rows = {what: ok for ok, what, _ in w.check()}
    assert rows["config/courses.yaml"] is False and rows["Canvas token saved in .env"] is False


def test_setup_needs_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(w.sys.stdin, "isatty", lambda: False, raising=False)
    assert w.main() == 2
    assert "real terminal" in capsys.readouterr().err
