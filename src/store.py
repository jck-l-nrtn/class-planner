"""Durable task store."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR, write_atomic
from .model import Task, Status

STORE_PATH = DATA_DIR / "store.json"


class NoSuchTask(KeyError):
    """A task id that is not in the store (and is not an alias of one)."""

VERSION = 2
AGENT_FIELDS = ("est_minutes", "agent_weight", "agent_note")
LAYERED = ("status", "est_minutes", "agent_weight", "agent_note")
DONE = (Status.SUBMITTED, Status.GRADED, Status.DROPPED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if os.name == "nt":
    import msvcrt
    import time

    def _lock(f):
        f.seek(0)
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)


def migrate(raw: dict) -> dict:
    """Bring any older store up to the current shape. Never drops a key."""
    if raw.get("version", 1) >= VERSION:
        return raw
    estimates = raw.setdefault("estimates", {})
    for uid, ov in list(raw.get("overrides", {}).items()):
        if "agent_note" in ov or "agent_weight" in ov:
            estimates[uid] = {k: ov.pop(k) for k in AGENT_FIELDS if k in ov}
            if not ov:
                del raw["overrides"][uid]
    raw.setdefault("aliases", {})
    raw.setdefault("sources", {})
    raw["version"] = VERSION
    return raw


class Store:
    def __init__(self, path: Path = STORE_PATH, origin: str = "cli"):
        self.path = Path(path)
        self.origin = origin
        self.tasks: dict[str, Task] = {}
        self.overrides: dict[str, dict] = {}
        self.estimates: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.sources: dict[str, dict] = {}
        self.log: list[dict] = []
        self._pending_events: list[dict] = []
        self._src: dict[str, dict] = {}
        self.load()

    @classmethod
    @contextmanager
    def edit(cls, origin: str, path: Path = STORE_PATH):
        """Load under an exclusive lock, yield, save atomically."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a+") as lock:
            _lock(lock)
            try:
                st = cls(path, origin)
                yield st
                st.save()
            finally:
                _unlock(lock)

    def load(self):
        if not self.path.exists():
            return
        raw = migrate(json.loads(self.path.read_text()))
        self.tasks = {u: Task.from_dict(d) for u, d in raw.get("tasks", {}).items()}
        self.overrides = raw.get("overrides", {})
        self.estimates = raw.get("estimates", {})
        self.aliases = raw.get("aliases", {})
        self.sources = raw.get("sources", {})
        self.log = raw.get("log", [])
        saved = raw.get("source_fields", {})
        self._src = {u: saved.get(u) or self._fields(t) for u, t in self.tasks.items()}
        self.apply_overrides()

    @staticmethod
    def _fields(t: Task) -> dict:
        return {"status": t.status.value, "est_minutes": t.est_minutes,
                "agent_weight": t.agent_weight, "agent_note": t.agent_note}

    def source_task(self, uid: str) -> Task:
        """The task as its source last described it, without your edits."""
        d = self.tasks[uid].to_dict()
        d.update(self._src.get(uid, {}))
        return Task.from_dict(d)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.path, json.dumps({
            "version": VERSION,
            "updated_at": _now(),
            "tasks": {u: t.to_dict() for u, t in self.tasks.items()},
            "source_fields": {u: f for u, f in self._src.items()
                              if u in self.tasks and f != self._fields(self.tasks[u])},
            "overrides": self.overrides,
            "estimates": self.estimates,
            "aliases": self.aliases,
            "sources": self.sources,
            "log": self.log,
        }, indent=2))
        if self._pending_events:
            with (self.path.parent / "events.jsonl").open("a", encoding="utf-8") as f:
                for e in self._pending_events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            self._pending_events = []

    def event(self, uid: str, what: str, old=None, new=None, note: str = ""):
        t = self.tasks.get(uid)
        self._pending_events.append({
            "ts": _now(), "origin": self.origin, "uid": uid,
            "title": t.title if t else "", "course": t.course_key if t else "",
            "what": what, "old": old, "new": new, **({"note": note} if note else {})})

    def resolve(self, uid: str) -> str:
        seen = set()
        while uid in self.aliases and uid not in seen:
            seen.add(uid)
            uid = self.aliases[uid]
        return uid

    def upsert(self, incoming: list[Task],
               complete: set[tuple[str, str]] | None = None) -> dict:
        """Merge freshly-fetched tasks."""
        complete = complete or set()
        today = datetime.now().date().isoformat()
        seen, added, updated = set(), 0, 0
        changed_due, newly_graded, new_tasks, closed = [], [], [], []
        for t in incoming:
            seen.add(t.uid)
            old = self.tasks.get(t.uid)
            if old is None:
                self.tasks[t.uid] = t
                self._src[t.uid] = self._fields(t)
                added += 1
                new_tasks.append(t)
                self.event(t.uid, "appeared", new=t.due_at.isoformat() if t.due_at else None)
                continue
            t.spent_minutes = old.spent_minutes
            src_old = self.source_task(t.uid)
            if src_old.due_at != t.due_at and t.due_at:
                changed_due.append((t.title, src_old.due_at, t.due_at))
                self.event(t.uid, "due_changed",
                           src_old.due_at.isoformat() if src_old.due_at else None,
                           t.due_at.isoformat())
            if t.score is not None and src_old.score != t.score:
                newly_graded.append((t.title, t.course_key, t.score, t.points))
                self.event(t.uid, "graded", src_old.score, t.score)
            elif t.status in DONE and src_old.status not in DONE:
                closed.append((t.title, t.course_key, t.done_via))
                self.event(t.uid, "closed_by_source", src_old.status.value,
                           t.status.value, note=t.done_via)
            if src_old.missing_since:
                self.event(t.uid, "back_in_source", src_old.missing_since, None)
            if src_old.to_dict() != t.to_dict():
                updated += 1
            self.tasks[t.uid] = t
            self._src[t.uid] = self._fields(t)

        newly_missing = []
        for uid, t in self.tasks.items():
            if uid in seen or (t.source, t.course_key) not in complete:
                continue
            if self._src.get(uid, {}).get("status") in [s.value for s in DONE] \
                    or t.missing_since:
                continue
            t.missing_since = today
            newly_missing.append(uid)
            self.event(uid, "missing_from_source", None, today)

        self.apply_overrides()
        return {"added": added, "updated": updated, "changed_due": changed_due,
                "newly_graded": newly_graded, "new_tasks": new_tasks,
                "closed_by_source": closed, "newly_missing": newly_missing,
                "missing": [u for u, t in self.tasks.items()
                            if t.missing_since and not t.is_done
                            and not self.overrides.get(u, {}).get("keep")]}

    def apply_overrides(self):
        """Layer agent estimates, then your own settings, over source facts."""
        for uid, t in self.tasks.items():
            f = self._src.setdefault(uid, self._fields(t))
            t.status = Status(f["status"])
            t.est_minutes, t.agent_weight, t.agent_note = (
                f["est_minutes"], f["agent_weight"], f["agent_note"])
        for uid, est in self.estimates.items():
            t = self.tasks.get(uid)
            if t and t.schedulable:
                for k in AGENT_FIELDS:
                    if est.get(k) is not None:
                        setattr(t, k, est[k])
        for uid, ov in self.overrides.items():
            t = self.tasks.get(uid)
            if not t:
                continue
            if "est_minutes" in ov:
                t.est_minutes = int(ov["est_minutes"])
            if "status" in ov and t.status not in DONE:
                t.status = Status(ov["status"])

    def supersede(self, old_uid: str, new_uid: str) -> bool:
        """`old_uid` (a hand-entered placeholder) is now `new_uid` (the real row)."""
        old, new = self.tasks.get(old_uid), self.tasks.get(new_uid)
        if not old or not new or old_uid == new_uid:
            return False
        new.spent_minutes += old.spent_minutes
        for e in self.log:
            if e["uid"] == old_uid:
                e["orig_uid"], e["uid"] = old_uid, new_uid
        if old_uid in self.overrides:
            merged = {**self.overrides.pop(old_uid), **self.overrides.get(new_uid, {})}
            self.overrides[new_uid] = merged
        if old_uid in self.estimates and new_uid not in self.estimates:
            self.estimates[new_uid] = self.estimates.pop(old_uid)
        self.estimates.pop(old_uid, None)
        self.aliases[old_uid] = new_uid
        self.event(new_uid, "superseded_placeholder", old_uid, new_uid,
                   note=f"'{old.title}' is now the gradebook row '{new.title}'")
        del self.tasks[old_uid]
        self._src.pop(old_uid, None)
        self.apply_overrides()
        return True

    def _task(self, uid: str) -> Task:
        uid = self.resolve(uid)
        if uid not in self.tasks:
            raise NoSuchTask(uid)
        return self.tasks[uid]

    def set_status(self, uid: str, status: str):
        t = self._task(uid)
        old = t.status.value
        self.overrides.setdefault(t.uid, {})["status"] = Status(status).value
        self.apply_overrides()
        self.event(t.uid, "status", old, status)

    def overdue_before(self, cutoff) -> list[Task]:
        return [t for t in self.open_tasks()
                if t.schedulable and t.due_at and t.due_at < cutoff
                and "status" not in self.overrides.get(t.uid, {}) and not t.spent_minutes]

    def catch_up(self, cutoff) -> list[Task]:
        """Mark that work as handled."""
        tasks = self.overdue_before(cutoff)
        for t in tasks:
            self.set_status(t.uid, "submitted")
        return tasks

    def clear_status(self, uid: str) -> bool:
        """Undo a manual done / in-progress mark. Estimates are untouched."""
        uid = self.resolve(uid)
        ov = self.overrides.get(uid)
        if not ov or "status" not in ov:
            return False
        old = ov.pop("status")
        if not ov:
            del self.overrides[uid]
        self.apply_overrides()
        t = self.tasks.get(uid)
        self.event(uid, "status", old, t.status.value if t else None, note="undo")
        return True

    def set_estimate(self, uid: str, minutes: int):
        t = self._task(uid)
        old = t.est_minutes
        self.overrides.setdefault(t.uid, {})["est_minutes"] = int(minutes)
        self.apply_overrides()
        self.event(t.uid, "estimate", old, int(minutes))

    def set_agent_estimate(self, uid: str, fields: dict, content_hash: str | None):
        t = self._task(uid)
        self.estimates[t.uid] = {**{k: fields[k] for k in AGENT_FIELDS if k in fields},
                                 "content_hash": content_hash, "at": _now()}
        old = t.est_minutes
        self.apply_overrides()
        if "est_minutes" in fields and old != self.tasks[t.uid].est_minutes:
            self.event(t.uid, "agent_estimate", old, self.tasks[t.uid].est_minutes)

    def keep(self, uid: str):
        """You have looked at a task missing from its source and want it kept."""
        t = self._task(uid)
        self.overrides.setdefault(t.uid, {})["keep"] = True
        self.event(t.uid, "kept_despite_missing", t.missing_since, None)

    def log_time(self, uid: str, minutes: int, when: datetime | None = None):
        """Record real time spent. This is what makes week 8 smarter than week 2."""
        t = self._task(uid)
        t.spent_minutes += minutes
        self.log.append({"uid": t.uid, "minutes": minutes, "kind": t.kind.value,
                         "course": t.course_key,
                         "at": (when or datetime.now(timezone.utc)).isoformat()})
        self.event(t.uid, "time_logged", None, minutes)

    def open_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if not t.is_done]

    def user_estimated(self, uid: str) -> bool:
        return "est_minutes" in self.overrides.get(uid, {})

    def calibration(self) -> dict[str, float]:
        """Observed actual/estimated ratio per (course, kind), from completed work."""
        buckets: dict[str, list[float]] = {}
        for t in self.tasks.values():
            if t.is_done and t.spent_minutes > 0 and t.est_minutes > 0:
                key = f"{t.course_key}:{t.kind.value}"
                buckets.setdefault(key, []).append(t.spent_minutes / t.est_minutes)
        return {k: sum(v) / len(v) for k, v in buckets.items() if len(v) >= 2}


def read_events(limit: int = 200, path: Path = STORE_PATH) -> list[dict]:
    p = Path(path).parent / "events.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[::-1]
