"""./plan setup -- point the planner at one student's classes."""
from __future__ import annotations

import getpass
import os
import re
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
import yaml

from . import config, settings_edit as se

DEFAULT_CANVAS = "https://byu.instructure.com"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def course_key(text: str, taken: set[str] | tuple = ()) -> str:
    m = re.search(r"\b([A-Z]{1,5}(?: [A-Z]{1,2})?) ?-?(\d{3}[A-Z]?)\b", text or "")
    if m:
        key = m.group(1).replace(" ", "") + m.group(2)
    else:
        words = re.findall(r"[A-Za-z0-9]+", text or "")
        key = "".join(w[:4] for w in words[:2]).upper() or "COURSE"
    base, n = key, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


def parse_meeting(text: str) -> tuple[list[str], str, str] | None:
    text = (text or "").strip()
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|–|to)\s*"
                  r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    if not m:
        return None
    day_text = text[:m.start()].lower()
    days = [d for d in DAYS if re.search(rf"\b{d}", day_text)]
    if not days:
        letters = re.sub(r"[^a-z]", "", day_text)
        letters = letters.replace("th", "R").replace("su", "U").replace("sa", "S")
        code = {"m": "mon", "t": "tue", "w": "wed", "R": "thu", "r": "thu", "f": "fri",
                "S": "sat", "U": "sun"}
        days = [code[c] for c in letters if c in code]
    days = [d for d in DAYS if d in days]
    if not days:
        return None
    h1, m1, ap1, h2, m2, ap2 = (m.group(i) for i in range(1, 7))
    h1, h2, m1, m2 = int(h1), int(h2), int(m1 or 0), int(m2 or 0)
    ap1, ap2 = (ap1 or "").lower(), (ap2 or "").lower()
    if ap2 == "pm" and h2 < 12:
        h2 += 12
    if ap1 == "pm" and h1 < 12:
        h1 += 12
    if not ap1 and not ap2 and h1 < 8:
        h1, h2 = h1 + 12, h2 + (12 if h2 < 12 else 0)
    if not ap1 and h2 >= 12 and h1 + 12 <= h2 and h1 < 8:
        h1 += 12
    if not ap2 and h2 < h1:
        h2 += 12
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and m1 < 60 and m2 < 60) or (h2, m2) <= (h1, m1):
        return None
    return days, f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"


def parse_picks(answer: str, n: int) -> list[int] | None:
    """'' or 'all' -> everything; '1,3 5' -> [0, 2, 4]; 'none' -> []. None if bad."""
    a = (answer or "").strip().lower()
    if a in ("", "all", "a", "y", "yes"):
        return list(range(n))
    if a in ("none", "n", "no", "0"):
        return []
    try:
        picks = sorted({int(x) - 1 for x in re.split(r"[,\s]+", a) if x})
    except ValueError:
        return None
    return picks if all(0 <= i < n for i in picks) else None


def system_timezone(default: str = "America/Denver") -> str:
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return os.environ.get("TZ") or default


def merge_courses(canvas: list[dict], ls: list[dict]) -> list[dict]:
    """One row per course."""
    out: dict[str, dict] = {}
    for c in canvas:
        key = course_key(c.get("code") or c["name"], set(out))
        out[key] = {"key": key, "name": c["name"], "credits": 3,
                    "canvas_id": c["canvas_id"], "ls_course_id": None}
    for c in ls:
        plain = course_key(c["title"])
        if plain in out and not out[plain]["ls_course_id"]:
            out[plain]["ls_course_id"] = c["id"]
            continue
        key = course_key(c["title"], set(out))
        out[key] = {"key": key, "name": re.sub(r"\s+", " ", c["title"]).strip(),
                    "credits": 3,
                    "canvas_id": None, "ls_course_id": c["id"]}
    return list(out.values())


def courses_yaml(courses: list[dict], today: date) -> str:
    example = config.CONFIG_DIR / "examples" / "courses.yaml"
    defaults = {}
    if example.exists():
        defaults = (yaml.safe_load(example.read_text(encoding="utf-8")) or {}).get("defaults", {})
    rows = [{"key": c["key"], "name": c["name"], "credits": c["credits"],
             "canvas_id": c["canvas_id"], "ls_course_id": c["ls_course_id"],
             "weights": {}, "effort": {}} for c in courses]
    head = (f"# Your courses. Written by `./plan setup` on {today.isoformat()}; edit freely.\n"
            "# Every option (per-category effort, hand-set weights, prep tasks) is\n"
            "# explained in config/examples/courses.yaml. This file is not in git.\n\n")
    return head + yaml.safe_dump({"courses": rows, "defaults": defaults}, sort_keys=False,
                                 allow_unicode=True, width=100)


def commitments_yaml(tz: str, wake: str, sleep: str, term: tuple[date, date],
                     classes: list[dict], today: date) -> str:
    body = {
        "timezone": tz,
        "term": {"start": term[0], "end": term[1]},
        "day": {"wake": wake, "sleep": sleep, "wind_down": 45},
        "meals": [{"name": "breakfast", "at": "07:30", "minutes": 25, "flex": 45},
                  {"name": "lunch", "at": "12:30", "minutes": 30, "flex": 90},
                  {"name": "dinner", "at": "18:15", "minutes": 45, "flex": 90}],
        "locations": {"default": "home", "places": ["home", "campus"]},
        "travel": {"default_minutes": 15, "matrix": [], "buffer_before": 5},
        "recurring": classes,
        "one_offs": [],
    }
    head = (f"# Your fixed week. Written by `./plan setup` on {today.isoformat()}; edit freely.\n"
            "# Add work shifts, church, practice -- anything the planner must work\n"
            "# around -- under `recurring`. config/examples/commitments.yaml shows how.\n"
            "# This file is not in git.\n\n")
    return head + yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=100)


def guess_term(today: date) -> tuple[date, date]:
    y, m = today.year, today.month
    if m <= 4:
        return date(y, 1, 5), date(y, 4, 24)
    if m <= 6:
        return date(y, 4, 28), date(y, 6, 18)
    if m <= 8:
        return date(y, 6, 22), date(y, 8, 14)
    return date(y, 9, 1), date(y, 12, 18)


def write_env(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in .env, keeping every other line. Mode 600."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    left = dict(values)
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if k in left and not line.lstrip().startswith("#"):
            lines[i] = f"{k}={left.pop(k)}"
    lines += [f"{k}={v}" for k, v in left.items()]
    config.write_atomic(path, "\n".join(lines) + "\n", mode=0o600)


def read_env(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


TOKEN_RE = re.compile(r"\d{1,6}~[A-Za-z0-9]{30,}")
_ESCAPES = re.compile(r"\x1b\[[0-9;]*[~A-Za-z]")


def clean_token(text: str) -> str:
    text = _ESCAPES.sub("", text or "")
    m = TOKEN_RE.search(text)
    if m:
        return m.group(0)
    words = text.split()
    if len(words) == 1:
        return words[0]
    longest = max(words, key=len, default="")
    return longest if len(longest) >= 20 else ""


def looks_like_token(text: str) -> bool:
    return bool(TOKEN_RE.search(text or ""))


def assemble(read, write, pending=lambda: False) -> str:
    """Turn raw keystrokes into one hidden line, with feedback."""
    raw, hinted, in_escape = "", False, False
    while True:
        chunk = read()
        if not chunk:
            raise EOFError
        for i, ch in enumerate(chunk):
            if in_escape:
                raw += ch
                in_escape = not (ch.isalpha() or ch == "~")
            elif ch == "\x1b":
                raw, in_escape = raw + ch, True
            elif ch in "\r\n":
                more = bool(_ESCAPES.sub("", chunk[i + 1:]).strip()) or pending()
                if _ESCAPES.sub("", raw).strip() and not more:
                    write("\n")
                    return raw
                if raw.strip():
                    raw += " "
                elif not hinted and not more:
                    hinted = True
                    write("\n  Nothing has arrived yet. Paste with Command-V (\u2318V), "
                          "then press Return.\n  ")
            elif ch == "\x16":
                write("\n  That was Control-V, which does not paste in Terminal. "
                      "Use Command-V (\u2318V).\n  ")
            elif ch in "\x7f\x08":
                if raw:
                    raw = raw[:-1]
                    write("\b \b")
            elif ch == "\x04":
                raise EOFError
            elif ch.isprintable():
                raw += ch
                write("\u2022")


def read_secret(prompt: str) -> str:
    """Hidden input on the terminal: nothing typed is shown, only a dot per character."""
    try:
        import select
        import termios
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except (ImportError, OSError):
        return getpass.getpass(prompt)
    old = termios.tcgetattr(fd)
    try:
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN)
        new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        os.write(fd, prompt.encode())
        return assemble(lambda: os.read(fd, 4096).decode("utf-8", "ignore"),
                        lambda t: os.write(fd, t.encode()),
                        lambda: bool(select.select([fd], [], [], 0.08)[0]))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)


def read_clipboard() -> str:
    """What is on the clipboard (macOS), or '' if that cannot be read."""
    if sys.platform != "darwin":
        return ""
    try:
        import subprocess
        return subprocess.run(["/usr/bin/pbpaste"], capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def canvas_courses(base: str, token: str) -> tuple[str, list[dict]]:
    """(your name on Canvas, active courses). Raises CanvasError."""
    from .sources.canvas import CanvasClient
    client = CanvasClient(base, token)
    me = next(iter(client._paged("users/self")), {})
    rows = [{"canvas_id": c["id"], "name": c.get("name") or "", "code": c.get("course_code") or "",
             "term": (c.get("term") or {}).get("name") or "",
             "start": (c.get("term") or {}).get("start_at"),
             "end": (c.get("term") or {}).get("end_at")} for c in client.courses()]
    return me.get("name") or "", rows


def current_term(ls: list[dict]) -> list[dict]:
    """parse_course_list is newest term first; keep that term only."""
    return [c for c in ls if c["term"] == ls[0]["term"]] if ls else []


def check() -> list[tuple[bool, str, str]]:
    """[(ok, what, how to fix)] for everything a first run needs."""
    from . import agent, ai_tools
    from . import updater
    out = []
    sha = updater.installed().get("sha")
    auto, why = updater.enabled()
    out.append((True, f"planner version {sha[:7] if sha else 'unknown'} "
                      f"({'updates itself' if auto else why})", ""))
    out.append((sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}",
                "Python 3.10 or newer is needed"))
    try:
        import playwright  # noqa: F401
        out.append((True, "browser automation (playwright) installed", ""))
    except ImportError:
        out.append((False, "browser automation (playwright) not installed", "run ./install"))
    try:
        cli, path = agent.chosen()
        out.append((True, f"AI tool for plans and estimates: {agent.label(cli)} at {path}", ""))
    except agent.AgentError as e:
        out.append((False, "no AI tool for plans and estimates",
                    f"{e}\n" + ai_tools.how_to_get_text("             ")))
    env = read_env(config.ROOT / ".env")
    out.append((bool(env.get("CANVAS_TOKEN")), "Canvas token saved in .env",
                "run ./plan setup (skip this if none of your classes use Canvas)"))
    for name in config.PERSONAL:
        out.append(((config.CONFIG_DIR / name).exists(), f"config/{name}", "run ./plan setup"))
    profile = config.DATA_DIR / ".browser-profile"
    out.append((profile.exists(), "LearningSuite browser profile",
                "run ./plan ls-login (skip if none of your classes use LearningSuite)"))
    return out


def print_check(out=print) -> int:
    rows = check()
    for ok, what, fix in rows:
        out(f"  {'ok     ' if ok else 'MISSING'} {what}" + ("" if ok or not fix else f"\n          -> {fix}"))
    return 0 if all(ok for ok, _, _ in rows) else 1


class Wizard:
    def __init__(self, ask=input, secret=read_secret, out=print, today: date | None = None,
                 clipboard=read_clipboard):
        self.ask_raw, self.secret, self.out = ask, secret, out
        self.clipboard = clipboard
        self.today = today or date.today()

    def ask(self, prompt: str, default: str = "") -> str:
        ans = self.ask_raw(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
        return ans or default

    def yes(self, prompt: str, default: bool = True) -> bool:
        ans = self.ask_raw(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not ans else ans.startswith("y")

    def step(self, n: int, title: str) -> None:
        self.out(f"\n[{n}/5] {title}\n" + "-" * (len(title) + 6))

    def canvas(self) -> list[dict]:
        env_path = config.ROOT / ".env"
        env = read_env(env_path)
        base, token = env.get("CANVAS_BASE_URL") or DEFAULT_CANVAS, env.get("CANVAS_TOKEN", "")
        if token and not self.yes("A Canvas token is already saved. Use it?"):
            token = ""
        if not token:
            if not self.yes("Do any of your classes use Canvas?"):
                return []
            base = self.ask("Canvas address", base).rstrip("/").removesuffix("/api/v1")
            if not base.startswith("https://"):
                base = "https://" + base.split("://")[-1]
            self.out(f"  1. Open {base}/profile/settings in your web browser.\n"
                     "  2. Scroll to 'Approved Integrations', click '+ New Access Token',\n"
                     "     give it any name, set the expiration date as late as it allows,\n"
                     "     and click 'Generate Token'.\n"
                     "  3. Select the long token it shows and copy it (Command-C).\n"
                     "  (Tokens expire, often after 30 days. The planner warns you a week\n"
                     "  ahead and takes a new one under Needs attention.)")
        tries = 0
        while True:
            if not token:
                token = self.get_token()
                if not token:
                    tries += 1
                    if tries >= 3 and not self.yes("  Still no token. Keep trying?"):
                        self.out("  Skipping Canvas for now; run ./plan setup again to add it.")
                        return []
                    continue
            try:
                name, rows = canvas_courses(base, token)
            except Exception as e:
                self.out(f"  Canvas did not accept that: {str(e)[:160]}\n"
                         "  Copy the token again (the whole thing) and retry.")
                token = ""
                continue
            write_env(env_path, {"CANVAS_BASE_URL": base, "CANVAS_TOKEN": token})
            os.environ["CANVAS_BASE_URL"], os.environ["CANVAS_TOKEN"] = base, token
            self.out(f"  Signed in to Canvas as {name or 'you'}. Token saved to .env "
                     f"(only on this computer). {len(rows)} active course(s).")
            return rows

    def get_token(self) -> str:
        typed = self.ask_raw("  When the token is copied, come back here and press Return: ")
        if clean_token(typed) and len(clean_token(typed)) >= 20:
            return clean_token(typed)
        clip = self.clipboard()
        if looks_like_token(clip):
            token = clean_token(clip)
            self.out(f"  Read a {len(token)}-character token from the clipboard "
                     f"(not shown).")
            return token
        self.out("  The clipboard does not hold a Canvas token, so paste it here instead.\n"
                 "  Use Command-V (\u2318V), then Return. You will see one dot per character.")
        token = clean_token(self.secret("  Canvas token: "))
        if not token:
            self.out("  Nothing arrived.")
        return token

    def learningsuite(self):
        """(courses, live session or None)."""
        if not self.yes("Do any of your classes use LearningSuite?"):
            return [], None
        from .ls import browser, parse, session as sess_mod
        self.out("  A browser window will open. Sign in with your Net ID and approve Duo.\n"
                 "  Two things make the planner able to refresh by itself afterwards:\n"
                 "    * tick 'Yes, this is my device' / 'Remember me' on the Duo screen\n"
                 "    * click SAVE when the browser offers to save your password\n"
                 "  The window stays open for about 30 seconds after you are in, so you\n"
                 "  have time to click Save. It closes by itself.")
        self.ask_raw("  Press Enter to open the window... ")
        try:
            cookies, ua = browser.login(interactive=True, linger=30)
        except browser.LoginFailed as e:
            self.out(f"  Sign-in did not finish: {e}\n  Skipping LearningSuite for now; "
                     f"run ./plan setup again to add it.")
            return [], None
        s = sess_mod.new_session(cookies, ua)
        state, text = sess_mod.get_course_list(s)
        if state.kind != "ok":
            self.out(f"  Signed in, but could not read your course list ({state.kind}: "
                     f"{state.detail}). Skipping LearningSuite for now.")
            return [], None
        try:
            rows = current_term(parse.parse_course_list(text))
        except parse.PageChanged as e:
            self.out(f"  Signed in, but the course list page was not as expected ({e}).")
            return [], None
        self.out(f"  Found {len(rows)} LearningSuite course(s)"
                 + (f" for {rows[0]['term']}." if rows else "."))
        return rows, s

    def choose(self, found: list[dict]) -> list[dict]:
        if not found:
            return []
        for i, c in enumerate(found, 1):
            where = " + ".join(w for w, v in (("Canvas", c["canvas_id"]),
                                              ("LearningSuite", c["ls_course_id"])) if v)
            self.out(f"  {i}. {c['key']:<10} {c['name'][:52]:<52} ({where})")
        while True:
            picks = parse_picks(self.ask_raw(
                "  Which should the planner track? Enter = all, or numbers like 1,3,4: "),
                len(found))
            if picks is not None:
                break
            self.out("  Type numbers from the list, separated by commas.")
        chosen = [found[i] for i in picks]
        if chosen:
            self.out("\n  For each course: credit hours, and when it meets (so the planner\n"
                     "  knows when you are in class). Examples: MWF 9:00-9:50, TTh 12:30-1:45.\n"
                     "  Press Enter to skip a meeting time; you can add it later.")
        for c in chosen:
            if c["canvas_id"] and c["ls_course_id"]:
                where = self.ask(f"  {c['key']} is on both Canvas and LearningSuite. Where is "
                                 f"its work posted? (c)anvas, (l)earningsuite, (b)oth", "b").lower()
                if where.startswith("c"):
                    c["ls_course_id"] = None
                elif where.startswith("l"):
                    c["canvas_id"] = None
            while True:
                bad: dict = {}
                c["credits"] = se.clean_credits(self.ask(f"  {c['key']} credits", "3"), "c", bad)
                if not bad:
                    break
                self.out("  " + bad["c"])
            while True:
                raw = self.ask_raw(f"  {c['key']} meets: ").strip()
                c["meets"] = parse_meeting(raw) if raw else None
                if not raw or c["meets"]:
                    break
                self.out("  Could not read that. Try the form 'MWF 9:00-9:50', or Enter to skip.")
        return chosen

    def week(self, chosen: list[dict], canvas_rows: list[dict]) -> str:
        tz = self.ask("Your timezone", system_timezone())
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:
            self.out(f"  '{tz}' is not a timezone name; using America/Denver.")
            tz = "America/Denver"
        times = {}
        for label, prompt, default in (("wake", "You are usually up by (24-hour HH:MM)", "07:00"),
                                       ("sleep", "and done for the day by", "23:00")):
            val = self.ask(prompt, default)
            times[label] = se.hhmm(val)
            if not times[label]:
                self.out(f"  '{val}' is not HH:MM; using the default for {label}.")
                times[label] = default
        wake, sleep = times["wake"], times["sleep"]
        term = guess_term(self.today)
        for c in canvas_rows:
            try:
                term = (datetime.fromisoformat(c["start"].replace("Z", "+00:00")).date(),
                        datetime.fromisoformat(c["end"].replace("Z", "+00:00")).date())
                break
            except (AttributeError, ValueError, TypeError):
                continue
        while True:
            try:
                term = (date.fromisoformat(self.ask("First day of the term", term[0].isoformat())),
                        date.fromisoformat(self.ask("Last day of finals", term[1].isoformat())))
                if term[1] > term[0]:
                    break
            except ValueError:
                pass
            self.out("  Dates look like 2026-09-01, and the term must end after it starts.")
        classes = [{"name": c["key"], "kind": "class", "course": c["key"], "days": c["meets"][0],
                    "start": c["meets"][1], "end": c["meets"][2], "location": "campus"}
                   for c in chosen if c.get("meets")]
        return commitments_yaml(tz, wake, sleep, term, classes, self.today)

    def save(self, name: str, text: str) -> None:
        path = config.CONFIG_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            bak = path.with_name(f"{name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, bak)
            self.out(f"  kept the old one as config/{bak.name}")
        path.write_text(text, encoding="utf-8")
        self.out(f"  wrote config/{name}")

    def run(self) -> int:
        self.out("Class planner setup. About five minutes; you can stop with Ctrl-C and "
                 "run it again.")
        self.step(1, "What is installed")
        print_check(self.out)

        have = [n for n in config.PERSONAL if (config.CONFIG_DIR / n).exists()]
        if have and not self.yes(f"\nYou already have config/{' and config/'.join(have)}. "
                                 f"Rebuild from scratch?", default=False):
            self.out("Left as they are. `./plan setup --check` shows what is ready.")
            return 0

        self.step(2, "Canvas")
        canvas_rows = self.canvas()
        self.step(3, "LearningSuite")
        ls_rows, session = self.learningsuite()
        self.step(4, "Your courses")
        found = merge_courses(canvas_rows, ls_rows)
        if not found:
            self.out("  No courses found on either site, so there is nothing to track yet.\n"
                     "  Run ./plan setup again when you can sign in to one of them.")
            return 1
        chosen = self.choose(found)
        if not chosen:
            self.out("  No courses chosen; nothing written.")
            return 1
        self.step(5, "Your week")
        commitments = self.week(chosen, canvas_rows)
        self.out("")
        self.save("courses.yaml", courses_yaml(chosen, self.today))
        self.save("commitments.yaml", commitments)
        return self.first_run(session, bool(ls_rows))

    def first_run(self, session, uses_ls: bool) -> int:
        from . import agent, ai_tools, sync
        from .ls import refresh
        cfg = config.Config()
        self.out("\nPulling your coursework for the first time...")
        if session is not None and cfg.ls_course_ids():
            for r in refresh.refresh(cfg, interactive=True, session=session, log=self.out):
                self.out(r.line())
        sync.run(cfg)
        from .store import Store
        start_of_today = cfg.now().replace(hour=0, minute=0, second=0, microsecond=0)
        behind = Store().overdue_before(start_of_today)
        if behind and self.yes(
                f"\n{len(behind)} assignments were due before today and show no score. If you "
                f"are starting partway through the term, they would crowd out current work.\n"
                f"Set them aside as already handled? (You can bring any of them back.)"):
            with Store.edit("setup") as store:
                store.catch_up(start_of_today)
        ai = agent.available()
        if ai and self.yes(f"\nAsk {agent.label(ai[0])} to estimate the work and build "
                           f"today's plan now? (a few minutes)"):
            from . import daily, estimate
            models = cfg.prefs.get("agent") or {}
            estimate.run(cfg, model=models.get("estimate_model", "sonnet"), batch=20,
                         limit=None, redo=False)
            daily.run(cfg, None, model=models.get("plan_model", "opus"))
        self.out("\nDone. Open the planner with:   ./plan app")
        if not ai:
            self.out("No AI tool was found, so there are no plans or time estimates yet.\n"
                     + ai_tools.how_to_get_text() + "\nAfter that: `./plan estimate` and "
                     "`./plan today`.")
        if uses_ls:
            self.out("To check that LearningSuite can refresh without you:  ./plan ls-refresh\n"
                     "If it says the browser has no saved password, run "
                     "`./plan ls-login --save-password` and click Save.")
        self.out("On a Mac, `scripts/install-schedule.sh` makes it refresh every morning.")
        return 0


def main(check_only: bool = False) -> int:
    if check_only:
        return print_check()
    if not sys.stdin.isatty():
        print("`./plan setup` asks questions, so it needs a real terminal window. Open "
              "Terminal, cd to this folder and run it there.\n(`./plan setup --check` "
              "works anywhere and only reports what is ready.)", file=sys.stderr)
        return 2
    try:
        return Wizard().run()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Nothing half-written: run ./plan setup again any time.")
        return 130
