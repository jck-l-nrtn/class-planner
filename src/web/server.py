"""Local web app: a small JSON API plus static files."""
from __future__ import annotations

import json
import mimetypes
import subprocess
import threading
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import calendars, daily, health, journal, settings_edit, sync
from . import setup_api
from ..config import Config, OUT_DIR, ROOT
from ..model import Status
from ..prioritize import score_all
from ..store import Store, read_events
from ..timeline import free_minutes
from ..workload import calibration
from ..dates import fmt

STATIC = Path(__file__).parent / "static"
BIG_IMPACT_PCT = 5.0

JOBS = {
    "sync": ["sync"],
    "ls_refresh": ["ls-refresh"],
    "ls_login": ["ls-login", "--save-password"],
    "replan": ["today"],
}
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _run_job(job_id: str, name: str):
    try:
        p = subprocess.run([str(ROOT / "plan"), *JOBS[name]], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=900)
        out = ((p.stdout or "") + (p.stderr or ""))[-6000:]
        result = {"state": "done" if p.returncode == 0 else "error", "output": out}
    except Exception as e:
        result = {"state": "error", "output": f"{type(e).__name__}: {e}"}
    with _lock:
        _jobs[job_id] = {"name": name, **result}


def start_job(name: str) -> str:
    with _lock:
        running = next((j for j, v in _jobs.items()
                        if v["name"] == name and v["state"] == "running"), None)
        if running:
            return running
        job_id = datetime.now().strftime("%H%M%S%f")
        _jobs[job_id] = {"name": name, "state": "running", "output": ""}
    threading.Thread(target=_run_job, args=(job_id, name), daemon=True).start()
    return job_id


def latest_plan(today: date) -> tuple[dict | None, bool]:
    p = daily.read_plan(today)
    if p:
        return p, True
    past = []
    for f in OUT_DIR.glob("day-*.json"):
        try:
            d = date.fromisoformat(f.stem[4:])
        except ValueError:
            continue
        if d < today:
            past.append(d)
    return (daily.read_plan(max(past)), False) if past else (None, False)


def find_duplicates(tasks) -> list[dict]:
    """Open hand-entered tasks that look like the same work as another open task."""
    out = []
    manual = [t for t in tasks if t.source == "manual"]
    others = [t for t in tasks if t.source != "manual"]
    for m in manual:
        for o in others:
            if sync.same_work(m, o):
                out.append({"uid": m.uid, "title": m.title, "into": o.uid,
                            "into_title": o.title, "course": m.course_key})
                break
    return out


def state() -> dict:
    cfg = Config()
    store = Store()
    today = cfg.now().date()
    open_tasks = store.open_tasks()
    calib = calibration(cfg, store, open_tasks, today)
    busy = calendars.busy_by_day(cfg, today)
    scored = score_all(cfg, open_tasks, external_by_day=busy, calibration=calib)

    rows, by_uid, hidden = [], {}, 0
    for s in scored:
        t = s.task
        if not t.schedulable:
            hidden += 1
            continue
        r = daily.task_row(cfg, s, today)
        r.pop("detail")
        r.update({
            "in_progress": t.status == Status.IN_PROGRESS,
            "overdue": bool(t.due_at and t.due_at < cfg.now()),
            "slack_hours": None if s.slack_hours == float("inf") else round(s.slack_hours, 1),
            "behind": s.slack_hours < 0,
            "missing_since": t.missing_since,
            "est_by": ("you" if store.user_estimated(t.uid) else
                       "agent" if t.uid in store.estimates else "default"),
            "confirm_done": t.kind.value == "exams" or r["impact_pct"] >= BIG_IMPACT_PCT,
        })
        rows.append(r)
        by_uid[t.uid] = r

    plan, is_today = latest_plan(today)
    if plan:
        live = []
        for item in plan.get("ranked", []):
            cur = by_uid.get(store.resolve(item.get("uid", "")))
            if cur:
                live.append({**item, **cur})
        plan = {**plan, "ranked": live, "is_today": is_today,
                "later": [{**d, "open": store.resolve(d.get("uid") or "") in by_uid}
                          for d in plan.get("later", [])]}

    done = []
    for uid, ov in store.overrides.items():
        t = store.tasks.get(uid)
        if t and ov.get("status") in ("submitted", "dropped") and t.score is None \
                and not t.done_via:
            done.append({"uid": uid, "title": t.title, "course": t.course_key,
                         "status": ov["status"],
                         "due_label": fmt(t.due_at, "%a %b %-d") if t.due_at else ""})
    done.sort(key=lambda r: r["due_label"], reverse=True)

    start_of_today = cfg.now().replace(hour=0, minute=0, second=0, microsecond=0)
    missing = [r for r in rows if r["missing_since"]
               and not store.overrides.get(r["uid"], {}).get("keep")]
    sources = health.report(cfg, store)
    return {
        "now": cfg.now().isoformat(),
        "today": today.isoformat(),
        "plan": plan,
        "calendar": calendars.meta(cfg),
        "free_today": free_minutes(cfg, today, busy.get(today)),
        "tasks": rows,
        "courses": sorted({r["course"] for r in rows}),
        "hidden_attendance": hidden,
        "marked_done": done,
        "attention": {
            "missing": missing,
            "duplicates": find_duplicates([s.task for s in scored if s.task.schedulable]),
            "sources": sources,
            "problems": sum(1 for i in sources if i["stale"] or not i["ok"]),
            "overdue_untouched": len(store.overdue_before(start_of_today)),
        },
        "events": read_events(120),
        "notes": journal.recent(days=2, tz=cfg.tz),
        "totals": {"open": len(rows),
                   "hours": round(sum(r["remaining"] for r in rows) / 60, 1)},
    }


def _drop_from_plan(cfg, uid: str, dismiss: bool = False):
    day = cfg.now().date()
    plan = daily.read_plan(day)
    if not plan:
        return
    plan["ranked"] = [r for r in plan.get("ranked", []) if r.get("uid") != uid]
    if dismiss:
        plan["dismissed"] = sorted(set(plan.get("dismissed", [])) | {uid})
    daily.write_plan(day, plan)


def task_action(data: dict) -> dict:
    uid, action = data.get("uid"), data.get("action")
    cfg = Config()
    if action == "dismiss":
        _drop_from_plan(cfg, uid, dismiss=True)
        return {"ok": True}
    if action == "catch_up":
        cutoff = cfg.now().replace(hour=0, minute=0, second=0, microsecond=0)
        with Store.edit("app") as st:
            return {"ok": True, "count": len(st.catch_up(cutoff))}
    with Store.edit("app") as st:
        if st.resolve(uid or "") not in st.tasks:
            return {"ok": False, "error": "no such task"}
        if action == "done":
            st.set_status(uid, "submitted")
        elif action == "drop":
            st.set_status(uid, "dropped")
        elif action == "start":
            st.set_status(uid, "in_progress")
        elif action == "undone":
            if not st.clear_status(uid):
                return {"ok": False, "error": "that was not marked by you"}
        elif action == "log":
            m = int(data.get("minutes") or 0)
            if not 1 <= m <= 1440:
                return {"ok": False, "error": "minutes must be between 1 and 1440"}
            st.log_time(uid, m)
        elif action == "estimate":
            m = int(data.get("minutes") or 0)
            if not 5 <= m <= 3000:
                return {"ok": False, "error": "estimate must be between 5 and 3000 minutes"}
            st.set_estimate(uid, m)
        elif action == "keep":
            st.keep(uid)
        elif action == "merge":
            if not st.supersede(uid, st.resolve(data.get("into") or "")):
                return {"ok": False, "error": "could not merge those two tasks"}
        else:
            return {"ok": False, "error": f"unknown action {action!r}"}
    return {"ok": True}


def calendar_range(start: str, days: str) -> dict:
    try:
        first = date.fromisoformat(start)
    except ValueError:
        first = None
    return calendars.view(Config(), first, int(days) if days.isdigit() else 7)


def _week_action(action: str, data: dict) -> None:
    section = "meals" if data.get("section") == "meals" else "recurring"
    i, was = data.get("i"), data.get("was") if isinstance(data.get("was"), dict) else {}
    change = lambda c: settings_edit.edit_week(i, was, c, section)
    if action == "week_update":
        return change(data)
    if action == "week_skip":
        return change({"skip": data.get("on")})
    if action == "week_delete":
        return change({"delete": True})
    on = settings_edit._day(data.get("on"))
    if action == "week_detach":
        one = {"name": was.get("name"), "date": data.get("date") or on,
               "start": data.get("start"), "end": data.get("end")}
        calendars._clean_block(one)
        change({"skip": on})
        calendars.add_block(one)
        return
    tail = None
    if data.get("start"):
        tail = {"name": was.get("name"), "start": data.get("start"), "end": data.get("end"),
                "days": data.get("days") if section == "recurring" else list(settings_edit.DAYS),
                "from": on}
        calendars._clean_block(tail)
    change({"until": (date.fromisoformat(on) - timedelta(days=1)).isoformat()})
    if tail:
        calendars.add_block(tail)


def calendar_action(data: dict) -> dict:
    action = data.get("action")
    try:
        if action == "add_block":
            calendars.add_block(data)
        elif action == "update_block":
            calendars.update_block(data.get("id"), data)
        elif action == "skip_block":
            calendars.skip_block(data.get("id"), data.get("on"))
        elif action == "detach_block":
            calendars.detach_block(data.get("id"), data.get("on"), data)
        elif action == "set_blocks":
            calendars.set_blocks(data.get("blocks"))
        elif action == "split_block":
            calendars.split_block(data.get("id"), data.get("on"),
                                  data if data.get("start") else None)
        elif action in ("week_update", "week_skip", "week_delete", "week_detach", "week_split"):
            _week_action(action, data)
        elif action == "week_undo":
            if not settings_edit.undo_week():
                return {"ok": False, "error": "Your week changed since then, so it was left alone."}
            if "blocks" in data:
                calendars.set_blocks(data.get("blocks"))
        elif action == "add_link":
            calendars.add_link(data.get("name"), data.get("url"))
        elif action == "remove_block":
            if not calendars.remove_block(data.get("id")):
                return {"ok": False, "error": "that entry is already gone"}
        elif action == "remove_link":
            if not calendars.remove_link(data.get("id")):
                return {"ok": False, "error": "that calendar is already gone"}
        elif action == "start":
            calendars.start()
        elif action == "refresh":
            failed = [r for r in calendars.refresh() if not r["ok"]]
            if failed:
                return {"ok": False, "error": f"{failed[0]['name']}: {failed[0]['error']}"}
        else:
            return {"ok": False, "error": f"unknown action {action!r}"}
    except (calendars.Invalid, settings_edit.Invalid) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _local_host(self) -> bool:
        """DNS-rebinding guard: only answer requests addressed to this Mac."""
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def _same_origin(self) -> bool:
        if not self._local_host():
            return False
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://{host}",)

    def do_GET(self):
        if not self._local_host():
            return self._send({"error": "request refused"}, 403)
        url = urlparse(self.path)
        try:
            if url.path == "/api/id":
                return self._send({"root": str(ROOT)})
            if url.path == "/api/setup/state":
                return self._send(setup_api.state())
            if url.path == "/api/state":
                return self._send(state())
            if url.path == "/api/calendar":
                q = parse_qs(url.query)
                return self._send(calendar_range(q.get("start", [""])[0], q.get("days", ["7"])[0]))
            if url.path == "/api/feedback":
                from .. import feedback
                return self._send(feedback.links())
            if url.path == "/api/job":
                jid = parse_qs(url.query).get("id", [""])[0]
                with _lock:
                    return self._send(_jobs.get(jid, {"state": "unknown"}))
        except Exception as e:
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        if url.path == "/":
            rel = "setup.html" if setup_api.needed() else "index.html"
        else:
            rel = "setup.html" if url.path in ("/setup", "/settings") else url.path.lstrip("/")
        f = (STATIC / rel).resolve()
        if STATIC.resolve() in f.parents and f.is_file():
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
                ctype += "; charset=utf-8"
            return self._send(f.read_bytes(), ctype=ctype)
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._same_origin() or "application/json" not in \
                self.headers.get("Content-Type", ""):
            return self._send({"ok": False, "error": "cross-origin or non-JSON "
                               "request refused"}, 403)
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            path = urlparse(self.path).path
            if path in setup_api.ROUTES:
                res, code = setup_api.ROUTES[path](data)
                return self._send(res, code)
            if path == "/api/task":
                res = task_action(data)
                return self._send(res, 200 if res["ok"] else 400)
            if path == "/api/calendar":
                res = calendar_action(data)
                return self._send(res, 200 if res["ok"] else 400)
            if path == "/api/note":
                try:
                    return self._send({"ok": True, "entry": journal.add(data.get("text", ""), tz=Config().tz)})
                except ValueError as e:
                    return self._send({"ok": False, "error": str(e)}, 400)
            if path == "/api/job":
                if data.get("name") not in JOBS:
                    return self._send({"ok": False, "error": "unknown job"}, 400)
                return self._send({"ok": True, "job": start_job(data["name"])})
        except Exception as e:
            return self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        return self._send({"error": "not found"}, 404)


def _busy() -> bool:
    with _lock:
        if any(v["state"] == "running" for v in _jobs.values()):
            return True
    if "running" in (setup_api._first["status"], setup_api._ls["status"]):
        return True
    from .. import ai_tools
    if any((j or {}).get("status") == "running" for j in list(ai_tools._jobs.values())):
        return True
    # Half-finished setup lives only in memory; don't restart under it.
    return setup_api.needed() and bool(setup_api._canvas["courses"] or setup_api._ls["courses"])


def _keep_current(port: int, poll: int = 300) -> None:
    """The app can stay open for days."""
    import time
    from .. import updater
    started = updater.installed().get("sha")
    while True:
        time.sleep(poll)
        updater.maybe_update(log=lambda *a: None)
        if updater.installed().get("sha") == started:
            continue
        while _busy():
            time.sleep(20)
        updater.restart(["app", "--port", str(port), "--no-open"])


def _who_is_on(port: int) -> str | None:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/id", timeout=1.5) as r:
            return str(json.loads(r.read()).get("root") or "")
    except OSError as e:
        return "" if getattr(e, "code", None) else None
    except ValueError:
        return ""


def _bind(port: int, tries: int = 20):
    """Listen on `port`, or the next free one after it."""
    for p in range(port, port + tries):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), Handler), p
        except OSError:
            if _who_is_on(p) == str(ROOT):
                return None, p
    raise OSError(f"no free port between {port} and {port + tries - 1}")


def serve(port: int = 8787, open_browser: bool = True) -> int:
    srv, port = _bind(port)
    url = f"http://127.0.0.1:{port}/"
    if srv is None:
        print(f"The planner is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    threading.Thread(target=_keep_current, args=(port,), daemon=True).start()
    print(f"Class planner running at {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0
