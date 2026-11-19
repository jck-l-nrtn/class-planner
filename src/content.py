"""Content store: the raw material an estimating agent actually reads."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR

CONTENT_DIR = DATA_DIR / "content"
TASK_DIR = CONTENT_DIR / "tasks"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK = re.compile(r"\n{3,}")


def html_to_text(html: str | None) -> str:
    """Flatten Canvas/LearningSuite HTML to something a model can read cheaply."""
    if not html:
        return ""
    s = html.replace("\r\n", "\n")
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|blockquote)>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = _TAG.sub(" ", s)
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(ent, ch)
    s = _WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return _BLANK.sub("\n\n", s).strip()


def text_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
    return h.hexdigest()[:16]


@dataclass
class TaskContent:
    uid: str
    description: str = ""
    rubric: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def hash(self) -> str:
        vol = {k: self.extra.get(k) for k in
               ("score", "grade", "feedback", "rubric_assessment",
                "workflow_state", "late", "missing")}
        return text_hash(self.description,
                         json.dumps(self.rubric, sort_keys=True),
                         json.dumps(vol, sort_keys=True, default=str))

    @property
    def chars(self) -> int:
        return len(self.description)


class ContentStore:
    def __init__(self, root: Path = CONTENT_DIR):
        self.root = root
        self.tasks = root / "tasks"

    def _safe(self, uid: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", uid)

    def put_task(self, c: TaskContent) -> bool:
        """Write task content. Returns True if the text actually changed."""
        self.tasks.mkdir(parents=True, exist_ok=True)
        p = self.tasks / f"{self._safe(c.uid)}.json"
        new = {"uid": c.uid, "hash": c.hash, "chars": c.chars,
               "description": c.description, "rubric": c.rubric, **c.extra}
        if p.exists():
            try:
                if json.loads(p.read_text(encoding="utf-8")).get("hash") == c.hash:
                    return False
            except Exception:
                pass
        p.write_text(json.dumps(new, indent=1, ensure_ascii=False), encoding="utf-8")
        return True

    def get_task(self, uid: str) -> dict | None:
        p = self.tasks / f"{self._safe(uid)}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
