"""Date formatting that works on every platform."""
from __future__ import annotations


def fmt(dt, pattern: str) -> str:
    return dt.strftime(pattern.replace("%-d", str(dt.day)))
