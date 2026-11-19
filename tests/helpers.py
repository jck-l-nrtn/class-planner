"""Small builders shared by the store/sync/daily tests."""
from __future__ import annotations

from src.model import Task, Status, Kind
from tests.conftest import at


def mk(uid="canvas:1", source="canvas", course="AAA101", title="HW 1", due="2026-10-10 23:59",
       **kw) -> Task:
    d = dict(uid=uid, source=source, course_key=course, title=title,
             due_at=at(due) if due else None)
    d.update(kw)
    return Task(**d)
