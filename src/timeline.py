"""Turns config + external busy time into concrete blocks and free intervals."""
from __future__ import annotations

from datetime import datetime, date, time, timedelta

from .config import DAY_INDEX, parse_hhmm
from .model import Block, Interval

MIN_GAP = 30


def _off(row: dict, day: date) -> bool:
    """Skipped on this date, or the entry ended before it."""
    iso = day.isoformat()
    return iso in [str(x) for x in (row.get("skip_dates") or [])] \
        or bool(row.get("until")) and iso > str(row["until"])


def expand_recurring(cfg, day: date) -> list[Block]:
    blocks: list[Block] = []
    dow = day.weekday()
    for i, r in enumerate(cfg.commitments.get("recurring", []) or []):
        if str(r.get("name", "")).startswith("EXAMPLE"):
            continue
        if dow not in {DAY_INDEX[d] for d in r.get("days", [])}:
            continue
        if _off(r, day):
            continue
        blocks.append(Block(
            start=cfg.at(day, parse_hhmm(r["start"])),
            end=cfg.at(day, parse_hhmm(r["end"])),
            title=r["name"],
            kind=r.get("kind", "standing"),
            course_key=r.get("course", ""),
            location=r.get("location", ""),
            movable=bool(r.get("movable", False)),
            note=f"rec:{i}",
        ))
    return blocks


def expand_one_offs(cfg, day: date) -> list[Block]:
    out = []
    for o in cfg.commitments.get("one_offs", []) or []:
        if str(o.get("date")) != day.isoformat():
            continue
        out.append(Block(
            start=cfg.at(day, parse_hhmm(o["start"])),
            end=cfg.at(day, parse_hhmm(o["end"])),
            title=o.get("name", "Commitment"),
            kind=o.get("kind", "standing"),
            location=o.get("location", ""),
        ))
    return out


def insert_travel(cfg, blocks: list[Block], day: date) -> list[Block]:
    """Add door-to-door travel between consecutive blocks at different places."""
    if not blocks:
        return []
    placed = sorted(blocks, key=lambda b: b.start)
    home = cfg.commitments.get("locations", {}).get("default", "home")
    buffer = int(cfg.commitments.get("travel", {}).get("buffer_before", 0))
    travel: list[Block] = []

    prev_loc, prev_end = home, cfg.at(day, parse_hhmm(cfg.commitments["day"]["wake"]))
    for b in placed:
        if b.location and b.location != prev_loc:
            mins = cfg.travel_minutes(prev_loc, b.location) + buffer
            start = b.start - timedelta(minutes=mins)
            if start >= prev_end:
                travel.append(Block(start=start, end=b.start,
                                    title=f"Travel → {b.location}",
                                    kind="travel", location=b.location))
            prev_loc = b.location
        prev_end = max(prev_end, b.end)

    if prev_loc != home and placed:
        mins = cfg.travel_minutes(prev_loc, home)
        travel.append(Block(start=prev_end, end=prev_end + timedelta(minutes=mins),
                            title=f"Travel → {home}", kind="travel", location=home))
    return travel


def insert_meals(cfg, busy: list[Block], day: date,
                 unplaced: list[str] | None = None) -> list[Block]:
    """Place each meal as close to its preferred time as `flex` allows."""
    meals: list[Block] = []
    occupied = sorted(busy, key=lambda b: b.start)

    def collides(s: datetime, e: datetime) -> bool:
        return any(s < b.end and b.start < e for b in occupied + meals)

    for j, m in enumerate(cfg.commitments.get("meals", []) or []):
        if _off(m, day):
            continue
        dur = timedelta(minutes=int(m["minutes"]))
        want = cfg.at(day, parse_hhmm(m["at"]))
        flex = int(m.get("flex", 0))
        for offset in range(0, flex + 1, 15):
            for delta in ({0} if offset == 0 else {-offset, offset}):
                s = want + timedelta(minutes=delta)
                if not collides(s, s + dur):
                    meals.append(Block(start=s, end=s + dur,
                                       title=m["name"].title(), kind="meal", note=f"meal:{j}"))
                    break
            else:
                continue
            break
        else:
            if unplaced is not None:
                unplaced.append(m["name"])
    return meals


def free_intervals(cfg, day: date, busy: list[Block]) -> list[Interval]:
    d = cfg.commitments["day"]
    start = cfg.at(day, parse_hhmm(d["wake"]))
    end = cfg.at(day, parse_hhmm(d["sleep"])) - timedelta(minutes=int(d.get("wind_down", 0)))

    edges = sorted([b for b in busy if b.end > start and b.start < end],
                   key=lambda b: b.start)
    out, cursor = [], start
    for b in edges:
        if b.start > cursor:
            out.append(Interval(cursor, min(b.start, end)))
        cursor = max(cursor, b.end)
        if cursor >= end:
            break
    if cursor < end:
        out.append(Interval(cursor, end))
    return [i for i in out if i.minutes > 0]


def build_day(cfg, day: date, external_busy: list[Block] | None = None,
              unplaced_meals: list[str] | None = None
              ) -> tuple[list[Block], list[Interval]]:
    """Everything already spoken for on `day`, plus the gaps between."""
    fixed = expand_recurring(cfg, day) + expand_one_offs(cfg, day) + list(external_busy or [])
    fixed += insert_travel(cfg, fixed, day)
    fixed += insert_meals(cfg, fixed, day, unplaced_meals)
    fixed.sort(key=lambda b: b.start)
    return fixed, free_intervals(cfg, day, fixed)


def free_minutes(cfg, day: date, external_busy: list[Block] | None = None) -> int:
    """Study time in a day: gaps too short to settle into do not count."""
    return sum(g.minutes for g in build_day(cfg, day, external_busy)[1] if g.minutes >= MIN_GAP)


def capacity_minutes(cfg, start: datetime, until: datetime,
                     external_by_day: dict[date, list[Block]] | None = None) -> int:
    """Total free minutes between two instants. Drives the slack calculation."""
    if until <= start:
        return 0
    total, day = 0, start.date()
    while day <= until.date():
        _, gaps = build_day(cfg, day, (external_by_day or {}).get(day, []))
        for g in gaps:
            s, e = max(g.start, start), min(g.end, until)
            if e > s:
                total += int((e - s).total_seconds() // 60)
        day += timedelta(days=1)
    return total

