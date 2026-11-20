"""Setup in the browser: the same steps as `./plan setup`, as a small JSON API."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

from .. import config, setup_wizard as wiz

_lock = threading.Lock()
_canvas: dict = {"name": "", "courses": [], "error": ""}
_ls: dict = {"status": "idle", "detail": "", "courses": [], "term": "", "session": None}
_first: dict = {"status": "idle", "log": []}
# label, usual seconds
STEPS = {"ls": ("Grades", 25), "sync": ("Coursework", 20),
         "estimate": ("Time estimates", 180), "plan": ("Plan", 90)}


def needed() -> bool:
    return any(not (config.CONFIG_DIR / n).exists() for n in config.PERSONAL)


def _found() -> list[dict]:
    return wiz.merge_courses(_canvas["courses"], _ls["courses"])


def state() -> dict:
    env = wiz.read_env(config.ROOT / ".env")
    term = wiz.guess_term(date.today())
    from .. import ai_tools
    ai = ai_tools.status()
    current = None
    if not needed():
        try:
            from .. import settings_edit
            current = settings_edit.current()
        except Exception:
            current = None
    with _lock:
        return {
            "needed": needed(),
            "current": current,
            "ai": ai,
            "canvas": {"saved": bool(env.get("CANVAS_TOKEN")),
                       "base": env.get("CANVAS_BASE_URL") or wiz.DEFAULT_CANVAS,
                       "name": _canvas["name"], "count": len(_canvas["courses"]),
                       "verified": bool(_canvas["name"] or _canvas["courses"])},
            "learningsuite": {"status": _ls["status"], "detail": _ls["detail"],
                              "count": len(_ls["courses"]), "term": _ls["term"]},
            "courses": [{k: c[k] for k in ("key", "name", "credits", "canvas_id",
                                           "ls_course_id")} for c in _found()],
            "defaults": {"timezone": wiz.system_timezone(), "wake": "07:00",
                         "sleep": "23:00", "term_start": term[0].isoformat(),
                         "term_end": term[1].isoformat()},
            "first_run": _progress(),
            "schedule": {"supported": sys.platform == "darwin", "on": schedule_on()},
            "root": str(config.ROOT),
        }


def canvas(data: dict) -> tuple[dict, int]:
    env_path = config.ROOT / ".env"
    env = wiz.read_env(env_path)
    base = (data.get("base") or env.get("CANVAS_BASE_URL") or wiz.DEFAULT_CANVAS).strip()
    base = base.rstrip("/").removesuffix("/api/v1")
    if not base.startswith("https://"):
        base = "https://" + base.split("://")[-1]
    token = wiz.clean_token(data.get("token") or "") or env.get("CANVAS_TOKEN", "")
    if not token:
        return {"ok": False, "error": "Paste the token Canvas showed you."}, 400
    try:
        name, rows = wiz.canvas_courses(base, token)
    except Exception as e:
        return {"ok": False, "error": f"Canvas did not accept that: {str(e)[:160]}"}, 400
    wiz.write_env(env_path, {"CANVAS_BASE_URL": base, "CANVAS_TOKEN": token})
    os.environ["CANVAS_BASE_URL"], os.environ["CANVAS_TOKEN"] = base, token
    with _lock:
        _canvas.update(name=name, courses=rows)
    return {"ok": True, "name": name, "count": len(rows)}, 200


def _ls_login() -> None:
    from ..ls import browser, parse, session as sess_mod
    try:
        cookies, ua = browser.login(interactive=True, linger=30)
        s = sess_mod.new_session(cookies, ua)
        page, text = sess_mod.get_course_list(s)
        if page.kind != "ok":
            raise RuntimeError(f"signed in, but could not read your course list "
                               f"({page.kind}: {page.detail})")
        rows = wiz.current_term(parse.parse_course_list(text))
        done = {"status": "ok", "detail": "", "courses": rows, "session": s,
                "term": rows[0]["term"] if rows else ""}
    except Exception as e:
        done = {"status": "failed", "detail": str(e)[:240]}
    with _lock:
        _ls.update(done)


def ls_login(data: dict) -> tuple[dict, int]:
    with _lock:
        if _ls["status"] == "running":
            return {"ok": True}, 200
        _ls.update(status="running", detail="")
    threading.Thread(target=_ls_login, daemon=True).start()
    return {"ok": True}, 200


def _cli(data: dict, allow_none: bool = False) -> str:
    from .. import ai_tools
    cli = str(data.get("cli") or "").lower()
    if cli in ai_tools.TOOLS or (allow_none and cli in ("none", "auto")):
        return cli
    raise ValueError("Choose Claude, ChatGPT or GitHub Copilot.")


def ai_choose(data: dict) -> tuple[dict, int]:
    from .. import ai_tools
    try:
        ai_tools.save_choice(_cli(data, allow_none=True))
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)[:200]}, 400
    return {"ok": True}, 200


def ai_install(data: dict) -> tuple[dict, int]:
    from .. import ai_tools
    try:
        ai_tools.install(_cli(data))
    except ValueError as e:
        return {"ok": False, "error": str(e)[:200]}, 400
    return {"ok": True}, 200


def ai_signin(data: dict) -> tuple[dict, int]:
    from .. import ai_tools
    try:
        return {"ok": True, "command": ai_tools.open_sign_in(
            _cli(data), switch=data.get("switch") is True)}, 200
    except (ValueError, RuntimeError, OSError) as e:
        return {"ok": False, "error": str(e)[:200]}, 400


def _validate(data: dict) -> tuple[dict, dict]:
    """(clean values, {field: what is wrong})."""
    from .. import settings_edit as se
    errors: dict[str, str] = {}
    found = {c["key"]: c for c in _found()}
    chosen = []
    for row in data.get("courses") or []:
        c = found.get(row.get("key"))
        if not c:
            continue
        credits = se.clean_credits(row.get("credits"), f"credits:{c['key']}", errors)
        use = row.get("use")
        if use == "canvas" and c["canvas_id"]:
            c = {**c, "ls_course_id": None}
        elif use == "learningsuite" and c["ls_course_id"]:
            c = {**c, "canvas_id": None}
        raw = row.get("meets")
        if isinstance(raw, dict):
            m = se.clean_meets(raw, f"meets:{c['key']}", errors)
            meets = (m["days"], m["start"], m["end"]) if m else None
        else:
            meets_text = (raw or "").strip()
            meets = wiz.parse_meeting(meets_text) if meets_text else None
            if meets_text and not meets:
                errors[f"meets:{c['key']}"] = "Try the form MWF 9:00-9:50, or leave it empty."
        chosen.append({**c, "credits": credits, "meets": meets})
    if not chosen:
        errors["courses"] = "Choose at least one course."
    week = se.clean_week({
        "timezone": (data.get("timezone") or "").strip() or wiz.system_timezone(),
        "wake": data.get("wake") or "07:00", "sleep": data.get("sleep") or "23:00",
        "term_start": data.get("term_start") or "", "term_end": data.get("term_end") or "",
    }, errors)
    term = (week["term_start"], week["term_end"]) \
        if "term_start" in week and "term_end" in week else None
    return {"courses": chosen, "tz": week.get("timezone", ""), "wake": week.get("wake", ""),
            "sleep": week.get("sleep", ""), "term": term}, errors


def _progress() -> dict:
    out = {"status": _first["status"], "log": list(_first["log"])}
    steps = _first.get("steps") or []
    if _first["status"] == "running" and _first.get("step") in steps:
        out["steps"] = [{"label": STEPS[k][0], "secs": STEPS[k][1]} for k in steps]
        out["step"] = steps.index(_first["step"])
        out["elapsed"] = int(time.time() - _first["step_at"])
    return out


def _step(key: str) -> None:
    with _lock:
        _first.update(step=key, step_at=time.time())


def _log(line: str) -> None:
    with _lock:
        _first["log"].extend(str(line).splitlines() or [""])
        del _first["log"][:-200]


def _plan(*args: str, timeout: int = 900) -> bool:
    p = subprocess.run([str(config.ROOT / "plan"), *args], cwd=str(config.ROOT),
                       capture_output=True, text=True, timeout=timeout)
    _log(((p.stdout or "") + (p.stderr or "")).strip()[-3000:])
    return p.returncode == 0


def _first_run() -> None:
    from .. import agent
    status = "done"
    try:
        cfg = config.Config()
        with _lock:
            session = _ls["session"]
        use_ls = session is not None and bool(cfg.ls_course_ids())
        ai = agent.available()
        with _lock:
            _first["steps"] = ((["ls"] if use_ls else []) + ["sync"]
                               + (["estimate", "plan"] if ai else []))
        if use_ls:
            from ..ls import refresh
            _step("ls")
            _log("Reading your LearningSuite gradebooks...")
            for r in refresh.refresh(cfg, interactive=True, session=session, log=_log):
                _log(r.line())
        _step("sync")
        _log("Merging your coursework...")
        _plan("sync")
        if _first.get("catch_up"):
            _plan("catch-up")
        if not ai:
            _log("No AI tool found, so no time estimates or plan yet. Install one, "
                 "sign in, then press Replan in the app.")
        else:
            _step("estimate")
            _log(f"Asking {agent.label(ai[0])} for time estimates (a few minutes)...")
            _plan("estimate", timeout=1800)
            _step("plan")
            _log(f"Asking {agent.label(ai[0])} for today's plan...")
            _plan("today")
    except Exception as e:
        _log(f"Stopped: {type(e).__name__}: {e}")
        status = "error"
    with _lock:
        _first["status"] = status


def finish(data: dict) -> tuple[dict, int]:
    with _lock:
        if _first["status"] == "running":
            return {"ok": True}, 200
    clean, errors = _validate(data)
    if errors:
        return {"ok": False, "errors": errors}, 400
    today = date.today()
    classes = [{"name": c["key"], "kind": "class", "course": c["key"], "days": c["meets"][0],
                "start": c["meets"][1], "end": c["meets"][2], "location": "campus"}
               for c in clean["courses"] if c.get("meets")]
    saver = wiz.Wizard(ask=lambda p: "", out=lambda *a: None, clipboard=lambda: "")
    saver.save("courses.yaml", wiz.courses_yaml(clean["courses"], today))
    saver.save("commitments.yaml", wiz.commitments_yaml(
        clean["tz"], clean["wake"], clean["sleep"], clean["term"], classes, today))
    with _lock:
        _first.update(status="running", log=[], steps=[], step=None, catch_up=data.get("catch_up") is not False)
    threading.Thread(target=_first_run, daemon=True).start()
    return {"ok": True}, 200


def save(data: dict) -> tuple[dict, int]:
    from .. import settings_edit
    if needed():
        return {"ok": False, "errors": {"save": "Finish setup first."}}, 400
    week = {f: data[f] for f in ("timezone", "wake", "sleep", "term_start", "term_end")
            if f in data}
    try:
        done = settings_edit.apply({"update": data.get("courses") or [], "week": week})
    except settings_edit.Invalid as e:
        return {"ok": False, "errors": e.errors}, 400
    return {"ok": True, **done}, 200


def schedule_on() -> bool:
    """Are this folder's timers installed? (the same test the uninstall script uses)"""
    agents = Path.home() / "Library" / "LaunchAgents"
    mark = str(config.ROOT / "scripts" / "run-")
    for f in agents.glob("*classplanner.*.plist"):
        try:
            if mark in f.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
    return False


def schedule(data: dict) -> tuple[dict, int]:
    """Turn the timers on or off by running the planner's own script for it."""
    if sys.platform != "darwin":
        return {"ok": False, "error": "Automatic runs need a Mac."}, 400
    name = "install-schedule.sh" if data.get("on") is True else "uninstall-schedule.sh"
    try:
        p = subprocess.run(["/bin/bash", str(config.ROOT / "scripts" / name)],
                           cwd=str(config.ROOT), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200], "on": schedule_on()}, 500
    if p.returncode != 0:
        tail = ((p.stderr or "") + (p.stdout or "")).strip()[-300:]
        return {"ok": False, "error": tail or f"exit {p.returncode}", "on": schedule_on()}, 500
    return {"ok": True, "on": schedule_on()}, 200


ROUTES = {"/api/setup/schedule": schedule,
          "/api/setup/canvas": canvas, "/api/setup/ls-login": ls_login,
          "/api/setup/save": save,
          "/api/setup/ai": ai_choose, "/api/setup/ai-install": ai_install,
          "/api/setup/ai-signin": ai_signin, "/api/setup/finish": finish}
