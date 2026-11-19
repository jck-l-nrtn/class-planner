"""Core domain objects."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time, timedelta
from enum import Enum
from typing import Optional


class Kind(str, Enum):
    HOMEWORK = "homework"
    QUIZ = "quizzes"
    EXAM = "exams"
    PROJECT = "project"
    READING = "reading"
    OTHER = "other"


class Status(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    GRADED = "graded"
    DROPPED = "dropped"


@dataclass
class Task:
    """One thing that must get done. Sources normalize into this."""
    uid: str
    source: str
    course_key: str
    title: str
    kind: Kind = Kind.HOMEWORK
    due_at: Optional[datetime] = None
    available_from: Optional[datetime] = None
    prep_by: Optional[datetime] = None
    points: float = 0.0
    category: str = "homework"
    est_minutes: int = 60
    spent_minutes: int = 0
    status: Status = Status.NOT_STARTED
    hard_deadline: bool = False
    group_work: bool = False
    url: str = ""
    notes: str = ""
    impact_override: Optional[float] = None
    category_drops: int = 0
    score: Optional[float] = None
    schedulable: bool = True
    agent_weight: float = 1.0
    agent_note: str = ""
    weight_source: str = "config"
    done_via: str = ""
    missing_since: Optional[str] = None

    @property
    def remaining_minutes(self) -> int:
        return max(0, self.est_minutes - self.spent_minutes)

    @property
    def is_done(self) -> bool:
        return self.status in (Status.SUBMITTED, Status.GRADED, Status.DROPPED)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["status"] = self.status.value
        for k in ("due_at", "available_from", "prep_by"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d)
        d["kind"] = Kind(d.get("kind", "homework"))
        d["status"] = Status(d.get("status", "not_started"))
        for k in ("due_at", "available_from", "prep_by"):
            d[k] = datetime.fromisoformat(d[k]) if d.get(k) else None
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Block:
    """A placed interval on the calendar."""
    start: datetime
    end: datetime
    title: str
    kind: str
    course_key: str = ""
    task_uid: str = ""
    location: str = ""
    movable: bool = False
    note: str = ""

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def overlaps(self, other: "Block") -> bool:
        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        d["minutes"] = self.minutes
        return d


@dataclass
class Interval:
    """Free time available for packing."""
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class ScoredTask:
    task: Task
    score: float
    slack_hours: float
    grade_impact: float
    urgency: float
    reasons: list[str] = field(default_factory=list)
