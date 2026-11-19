from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml

from .. import config
from ..model import Kind, Status, Task


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def fetch(cfg, path: Path | None = None) -> list[Task]:
    """The hand-entered assignments. A missing or empty file is not an error."""
    path = path or config.CONFIG_DIR / "manual_assignments.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tasks = []

    for course_key, spec in data.items():
        cats = spec.get("categories", {}) or {}
        items = spec.get("assignments", []) or []
        counts: dict[str, int] = {}
        for a in items:
            counts[a["category"]] = counts.get(a["category"], 0) + 1

        for a in items:
            cat = a["category"]
            cmeta = cats.get(cat, {})
            n = counts.get(cat, 1)
            weight = float(cmeta.get("weight", 0.0))
            impact = (weight / n) if n else 0.0
            kind = Kind(cmeta.get("kind", "homework"))
            due = a.get("due")
            due_at = (datetime.strptime(due, "%Y-%m-%d %H:%M").replace(tzinfo=cfg.tz)
                      if due else None)
            tasks.append(Task(
                uid=f"manual:{course_key}-{_slug(a['title'])}",
                source="manual",
                course_key=course_key,
                title=a["title"],
                kind=kind,
                due_at=due_at,
                category=cat,
                est_minutes=int(a.get("est_minutes", cmeta.get("est_minutes", 60))),
                schedulable=True,
                status=Status.NOT_STARTED,
                hard_deadline=bool(a.get("hard_deadline", cmeta.get("hard_deadline", True))),
                impact_override=impact,
                weight_source="manual",
            ))
    return tasks
