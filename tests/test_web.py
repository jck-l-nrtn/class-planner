"""src/web/server.py: plan selection, duplicate hints, actions, and POST guards."""
from __future__ import annotations

import http.client
import json
import threading
from datetime import date
from http.server import ThreadingHTTPServer

import pytest

from src import daily
from src import store as store_mod
from src.model import Status
from src.web import server
from tests.helpers import mk


@pytest.fixture
def plans(tmp_path, monkeypatch):
    d = tmp_path / "out"
    d.mkdir()
    monkeypatch.setattr(daily, "OUT_DIR", d)
    monkeypatch.setattr(server, "OUT_DIR", d)
    return d


def put_plan(d, day, **kw):
    (d / f"day-{day}.json").write_text(json.dumps({"date": day, **kw}))


def test_latest_plan_prefers_today(plans):
    put_plan(plans, "2026-11-09")
    put_plan(plans, "2026-11-10")
    put_plan(plans, "2026-11-12")
    p, is_today = server.latest_plan(date(2026, 11, 10))
    assert p["date"] == "2026-11-10" and is_today is True


def test_latest_plan_falls_back_to_most_recent_earlier(plans):
    put_plan(plans, "2026-11-02")
    put_plan(plans, "2026-11-09")
    put_plan(plans, "2026-11-12")
    p, is_today = server.latest_plan(date(2026, 11, 10))
    assert p["date"] == "2026-11-09" and is_today is False


def test_latest_plan_never_returns_future_file(plans):
    put_plan(plans, "2026-11-11")
    put_plan(plans, "2027-01-01")
    assert server.latest_plan(date(2026, 11, 10)) == (None, False)


def test_latest_plan_none_when_empty(plans):
    assert server.latest_plan(date(2026, 11, 10)) == (None, False)


def test_latest_plan_ignores_junk_filenames(plans):
    (plans / "day-latest.json").write_text("{}")
    (plans / "day-2026-13-45.json").write_text("{}")
    put_plan(plans, "2026-11-01")
    p, _ = server.latest_plan(date(2026, 11, 10))
    assert p["date"] == "2026-11-01"


def test_latest_plan_orders_by_date_not_name_length_or_mtime(plans):
    put_plan(plans, "2026-10-31")
    put_plan(plans, "2026-09-30")
    p, _ = server.latest_plan(date(2026, 11, 10))
    assert p["date"] == "2026-10-31"


def test_latest_plan_corrupt_today_file_falls_back(plans):
    (plans / "day-2026-11-10.json").write_text("{not json")
    put_plan(plans, "2026-11-09")
    p, is_today = server.latest_plan(date(2026, 11, 10))
    assert p["date"] == "2026-11-09" and not is_today


def test_find_duplicates_suggests_manual_vs_real():
    m = mk("manual:1", "manual", title="Midterm 1", due="2026-10-20 18:00")
    r = mk("ls:1", "learningsuite", title="Midterm 1 Exam", due="2026-10-20 23:59")
    other = mk("canvas:9", title="HW 9", due="2026-10-20 23:59")
    out = server.find_duplicates([m, r, other])
    assert out == [{"uid": "manual:1", "title": "Midterm 1", "into": "ls:1",
                    "into_title": "Midterm 1 Exam", "course": "AAA101"}]


def test_find_duplicates_none_for_distinct_or_non_manual_pairs():
    a = mk("manual:1", "manual", title="Ch 8 Quiz", due=None)
    b = mk("ls:1", "learningsuite", title="Ch 9 Quiz", due=None)
    c = mk("canvas:1", title="Ch 8 Quiz", due=None)
    d = mk("ls:2", "learningsuite", title="Ch 8 Quiz", due=None, course="BBB202")
    assert server.find_duplicates([a, b, d]) == []
    assert server.find_duplicates([b, c]) == []
    assert len(server.find_duplicates([a, c])) == 1


def test_find_duplicates_one_hit_per_manual_task():
    m = mk("manual:1", "manual", title="Ch 8 Quiz", due=None)
    r1 = mk("ls:1", "learningsuite", title="Ch 8 Quiz", due=None)
    r2 = mk("canvas:1", title="Chapter 8 Quiz", due=None)
    assert len(server.find_duplicates([m, r1, r2])) == 1


@pytest.fixture
def env(tmp_path, monkeypatch, cfg, plans):
    path = tmp_path / "data" / "store.json"

    class TmpStore(store_mod.Store):
        def __init__(self, p=None, origin="cli"):
            super().__init__(p or path, origin)

        @classmethod
        def edit(cls, origin, p=None):
            return super().edit(origin, p or path)

    monkeypatch.setattr(server, "Store", TmpStore)
    monkeypatch.setattr(server, "Config", lambda: cfg)
    with store_mod.Store.edit("seed", path) as st:
        st.upsert([mk("canvas:1", title="HW 1", due="2026-10-10 23:59"),
                   mk("m:1", "manual", title="Midterm 1", due="2026-10-20 18:00"),
                   mk("ls:1", "learningsuite", title="Midterm 1 Exam",
                      due="2026-10-20 23:59")])
    return path


def reload(path):
    return store_mod.Store(path)


def test_task_action_done_undone_start(env):
    assert server.task_action({"uid": "canvas:1", "action": "done"}) == {"ok": True}
    assert reload(env).tasks["canvas:1"].status == Status.SUBMITTED
    assert server.task_action({"uid": "canvas:1", "action": "undone"}) == {"ok": True}
    assert reload(env).tasks["canvas:1"].status == Status.NOT_STARTED
    r = server.task_action({"uid": "canvas:1", "action": "undone"})
    assert r["ok"] is False
    server.task_action({"uid": "canvas:1", "action": "start"})
    assert reload(env).tasks["canvas:1"].status == Status.IN_PROGRESS


def test_task_action_unknown_task_and_action(env):
    assert server.task_action({"uid": "nope", "action": "done"})["ok"] is False
    assert server.task_action({"uid": "canvas:1", "action": "explode"})["ok"] is False
    assert server.task_action({"action": "done"})["ok"] is False


@pytest.mark.parametrize("action,minutes,ok", [
    ("log", 0, False), ("log", 1441, False), ("log", 1440, True), ("log", 30, True),
    ("estimate", 4, False), ("estimate", 3001, False), ("estimate", 5, True)])
def test_task_action_minute_ranges(env, action, minutes, ok):
    r = server.task_action({"uid": "canvas:1", "action": action, "minutes": minutes})
    assert r["ok"] is ok


def test_task_action_log_and_estimate_persist(env):
    server.task_action({"uid": "canvas:1", "action": "log", "minutes": 25})
    server.task_action({"uid": "canvas:1", "action": "estimate", "minutes": 45})
    st = reload(env)
    assert st.tasks["canvas:1"].spent_minutes == 25
    assert st.tasks["canvas:1"].est_minutes == 45 and st.user_estimated("canvas:1")


def test_task_action_merge(env):
    assert server.task_action({"uid": "m:1", "action": "merge", "into": "ls:1"})["ok"]
    st = reload(env)
    assert "m:1" not in st.tasks and st.resolve("m:1") == "ls:1"
    assert server.task_action({"uid": "m:1", "action": "merge", "into": "nope"})["ok"] is False


def test_task_action_writes_events_with_app_origin(env):
    server.task_action({"uid": "canvas:1", "action": "done"})
    lines = [json.loads(l) for l in (env.parent / "events.jsonl").read_text().splitlines()]
    assert lines[-1]["origin"] == "app" and lines[-1]["what"] == "status"
    assert lines[-1]["new"] == "submitted"


@pytest.fixture
def http_server(env):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def post(port, path, body, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = body if isinstance(body, (bytes, str)) else json.dumps(body)
    c.request("POST", path, body=data, headers=h)
    r = c.getresponse()
    out = (r.status, json.loads(r.read() or b"{}"))
    c.close()
    return out


DONE = {"uid": "canvas:1", "action": "done"}


def test_post_foreign_origin_is_403_and_changes_nothing(http_server, env):
    code, body = post(http_server, "/api/task", DONE, {"Origin": "http://evil.example"})
    assert code == 403 and body["ok"] is False
    assert reload(env).tasks["canvas:1"].status == Status.NOT_STARTED
    assert "canvas:1" not in reload(env).overrides


def test_post_other_local_port_origin_is_403(http_server, env):
    code, _ = post(http_server, "/api/task", DONE, {"Origin": "http://127.0.0.1:1"})
    assert code == 403


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data"])
def test_post_non_json_content_type_is_403(http_server, env, ctype):
    code, _ = post(http_server, "/api/task", json.dumps(DONE), {"Content-Type": ctype})
    assert code == 403
    assert "canvas:1" not in reload(env).overrides


def test_post_rebinding_host_is_403(http_server, env):
    code, _ = post(http_server, "/api/task", DONE, {"Host": "evil.example"})
    assert code == 403


@pytest.mark.parametrize("path", ["/api/state", "/api/setup/state", "/api/id", "/"])
def test_get_rebinding_host_is_403(http_server, env, path):
    c = http.client.HTTPConnection("127.0.0.1", http_server, timeout=5)
    c.request("GET", path, headers={"Host": "evil.example"})
    assert c.getresponse().status == 403
    c.close()


def test_post_same_origin_json_succeeds(http_server, env):
    code, body = post(http_server, "/api/task", DONE,
                      {"Origin": f"http://127.0.0.1:{http_server}"})
    assert code == 200 and body == {"ok": True}
    assert reload(env).tasks["canvas:1"].status == Status.SUBMITTED


def test_post_no_origin_header_json_succeeds(http_server, env):
    code, _ = post(http_server, "/api/task", {"uid": "canvas:1", "action": "start"})
    assert code == 200


def test_post_bad_action_is_400(http_server, env):
    code, body = post(http_server, "/api/task", {"uid": "canvas:1", "action": "zzz"})
    assert code == 400 and body["ok"] is False


def test_post_unknown_job_rejected_without_spawning(http_server, env):
    code, body = post(http_server, "/api/job", {"name": "rm -rf"})
    assert code == 400 and body["error"] == "unknown job"


def test_get_path_traversal_blocked(http_server):
    c = http.client.HTTPConnection("127.0.0.1", http_server, timeout=5)
    c.request("GET", "/../../src/config.py")
    r = c.getresponse()
    r.read()
    assert r.status == 404


def _get(path, monkeypatch, needed):
    import io
    monkeypatch.setattr(server.setup_api, "needed", lambda: needed)
    h = server.Handler.__new__(server.Handler)
    h.path, h.headers, h.wfile = path, {"Host": "127.0.0.1:8787"}, io.BytesIO()
    sent = {}
    h.send_response = lambda code: sent.setdefault("code", code)
    h.send_header = lambda k, v: sent.setdefault(k, v)
    h.end_headers = lambda: None
    h.do_GET()
    return sent, h.wfile.getvalue().decode("utf-8", "replace")


def test_root_serves_setup_until_set_up_then_the_app(monkeypatch):
    sent, body = _get("/", monkeypatch, needed=True)
    assert sent["code"] == 200 and "Set up your planner" in body
    sent, body = _get("/", monkeypatch, needed=False)
    assert sent["code"] == 200 and "Set up your planner" not in body and "app.js" in body


def test_setup_page_is_always_reachable_and_loads_no_outside_files(monkeypatch):
    sent, body = _get("/setup", monkeypatch, needed=False)
    assert sent["code"] == 200 and "Set up your planner" in body
    import re
    for name in ("setup.html", "setup.css", "setup.js"):
        text = (server.STATIC / name).read_text()
        outside = re.findall(r"""<(?:script|link|img|iframe)\b[^>]*(?:src|href)=["'](?:https?:)?//[^"']+"""
                             r"""|url\(["']?(?:https?:)?//|@import|fetch\(["']https?:""", text)
        assert not outside, (name, outside)


def test_second_copy_takes_the_next_free_port(monkeypatch):
    first, p1 = server._bind(18787)
    try:
        monkeypatch.setattr(server, "_who_is_on", lambda port: "/some/other/planner")
        second, p2 = server._bind(18787)
        try:
            assert p1 == 18787 and p2 == 18788 and second is not None
        finally:
            second.server_close()
    finally:
        first.server_close()


def test_same_copy_started_twice_does_not_start_a_second_server(monkeypatch):
    first, p1 = server._bind(18790)
    try:
        monkeypatch.setattr(server, "_who_is_on", lambda port: str(server.ROOT))
        second, p2 = server._bind(18790)
        assert second is None and p2 == 18790
    finally:
        first.server_close()


def test_who_is_on_tells_planner_from_stranger_from_nothing():
    import threading
    srv, port = server._bind(18795)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert server._who_is_on(port) == str(server.ROOT)
    finally:
        srv.shutdown(); srv.server_close()
    assert server._who_is_on(18999) is None


def test_busy_waits_for_an_ai_install_and_a_half_done_setup(monkeypatch):
    from src import ai_tools
    monkeypatch.setattr(server, "_jobs", {})
    monkeypatch.setattr(server.setup_api, "_first", {"status": "idle", "log": []})
    monkeypatch.setattr(server.setup_api, "_ls", {"status": "idle", "courses": []})
    monkeypatch.setattr(server.setup_api, "_canvas", {"courses": []})
    monkeypatch.setattr(server.setup_api, "needed", lambda: True)
    monkeypatch.setattr(ai_tools, "_jobs", {})
    assert server._busy() is False
    monkeypatch.setattr(ai_tools, "_jobs", {"claude": {"status": "running"}})
    assert server._busy() is True
    monkeypatch.setattr(ai_tools, "_jobs", {})
    monkeypatch.setattr(server.setup_api, "_canvas", {"courses": [{"id": 1}]})
    assert server._busy() is True
    monkeypatch.setattr(server.setup_api, "needed", lambda: False)
    assert server._busy() is False


def test_feedback_route_and_both_pages_load_its_links(monkeypatch):
    from src import feedback
    monkeypatch.setattr(feedback, "load_prefs", lambda: {"feedback_email": "me@example.com"})
    monkeypatch.setattr(feedback, "details", lambda: "version x")
    sent, body = _get("/api/feedback", monkeypatch, needed=False)
    assert sent["code"] == 200 and json.loads(body)["email"] == "me@example.com"
    for path, needed in (("/", False), ("/setup", False)):
        _, page = _get(path, monkeypatch, needed=needed)
        assert 'id="feedback"' in page and 'src="feedback.js"' in page
    sent, _ = _get("/feedback.js", monkeypatch, needed=False)
    assert sent["code"] == 200
