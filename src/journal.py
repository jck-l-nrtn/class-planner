"""A running log of what the student tells the planner, in their own words."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import DATA_DIR

JOURNAL = DATA_DIR / "journal.jsonl"
_TZ = ZoneInfo("America/Denver")


def add(text: str, tz: ZoneInfo = _TZ) -> dict:
    """Append one note. Returns the stored entry (or raises on empty text)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty note")
    now = datetime.now(tz)
    entry = {"ts": now.isoformat(timespec="seconds"),
             "date": now.date().isoformat(), "text": text}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _all() -> list[dict]:
    if not JOURNAL.exists():
        return []
    out = []
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def recent(days: int = 2, tz: ZoneInfo = _TZ) -> list[dict]:
    """Entries from the last `days` calendar days, oldest first."""
    cutoff = (datetime.now(tz).date() - timedelta(days=days - 1)).isoformat()
    return [e for e in _all() if e.get("date", "") >= cutoff]
