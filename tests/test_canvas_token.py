"""Canvas tokens expire (30 days at many schools): find out when, warn ahead."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import health
from src.sources.canvas import CanvasClient

TOKEN = "7407~" + "aB3" * 22
EXP = "2026-12-01T07:00:00Z"


def client(rows):
    c = CanvasClient("https://canvas.example", TOKEN)
    c._paged = lambda path, **kw: iter(rows)
    return c


def test_expiry_is_read_from_the_matching_token():
    rows = [{"visible_token": "9999~zzzzz...", "expires_at": "2030-01-01T00:00:00Z"},
            {"visible_token": TOKEN[:12] + "...", "expires_at": EXP}]
    assert client(rows).token_expiry(TOKEN) == EXP


def test_expiry_unknown_is_none_never_an_error():
    assert client([]).token_expiry(TOKEN) is None
    assert client([{"visible_token": "", "expires_at": EXP}]).token_expiry(TOKEN) is None
    assert client([{"visible_token": TOKEN[:12], "expires_at": None}]).token_expiry(TOKEN) is None
    c = client([])

    def boom(path, **kw):
        raise RuntimeError("404")
    c._paged = boom
    assert c.token_expiry(TOKEN) is None


def src(ok=True, message="26 tasks", expires=EXP):
    return {"ok": ok, "message": message, "extra": {"token_expires": expires}}


def at_days_before(days):
    return datetime(2026, 12, 1, 7, tzinfo=timezone.utc) - timedelta(days=days)


def test_no_row_while_the_token_has_more_than_a_week():
    assert health.canvas_token(src(), now=at_days_before(13)) is None
    assert health.canvas_token(src(expires=None)) is None
    assert health.canvas_token(src(expires="garbage")) is None
    assert health.canvas_token({"ok": True, "message": "ok"}) is None


def test_warns_within_a_week_and_counts_as_a_problem():
    row = health.canvas_token(src(), now=at_days_before(6.5))
    assert row["ok"] is True and row["stale"] is True and "in 6 days" in row["detail"]
    assert row["action"] == "canvas_token" and "new one" in row["fix"]
    assert "tomorrow" in health.canvas_token(src(), now=at_days_before(1.5))["detail"]
    assert "today" in health.canvas_token(src(), now=at_days_before(0.4))["detail"]


def test_expired_or_refused_token_is_a_failure_with_the_same_fix():
    row = health.canvas_token(src(), now=at_days_before(-2))
    assert row["ok"] is False and "expired on" in row["detail"]
    refused = health.canvas_token(src(ok=False, message="CanvasError: Canvas rejected the "
                                                        "token (401). It has most likely"))
    assert refused["ok"] is False and refused["action"] == "canvas_token"
    assert health.canvas_token({"ok": False, "message": "ConnectionError: no network"}) is None
