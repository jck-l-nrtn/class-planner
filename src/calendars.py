"""Busy time the planner works around: your own entries and linked calendars."""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, time as dtime, timedelta, timezone
from urllib.parse import urlparse

import requests
from dateutil.rrule import rrulestr
from icalendar import Calendar

from . import config
from .model import Block
from . import settings_edit
from .settings_edit import DAYS, hhmm
from .timeline import MIN_GAP, build_day

HORIZON_DAYS = 120
MAX_LINKS = 8
MAX_BLOCKS = 200
MAX_BYTES = 15 * 1024 * 1024
STALE_HOURS = 36

_UNTIL = re.compile(r"UNTIL=(\d{8}(?:T\d{6}Z?)?)")
_memo: dict = {}


class Invalid(ValueError):
    pass


def _file():
    return config.DATA_DIR / "calendar.json"


def _cache(link_id: str):
    return config.DATA_DIR / "cache" / f"cal-{link_id}.ics"


def _status_file():
    return config.DATA_DIR / "cache" / "cal-status.json"


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load() -> dict:
    d = _read(_file(), {})
    return {"links": list(d.get("links") or []), "blocks": list(d.get("blocks") or []),
            "started": bool(d.get("started") or d.get("links") or d.get("blocks"))}


def _save(d: dict) -> None:
    _file().parent.mkdir(parents=True, exist_ok=True)
    # The links are private addresses: anyone holding one can read that calendar.
    config.write_atomic(_file(), json.dumps(d, indent=1), mode=0o600)


def _status() -> dict:
    return _read(_status_file(), {})


def _set_status(link_id: str, ok: bool, error: str = "") -> None:
    s = _status()
    row = s.get(link_id) or {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    s[link_id] = {"ok": ok, "error": error, "last_try": now,
                  "last_ok": now if ok else row.get("last_ok")}
    _status_file().parent.mkdir(parents=True, exist_ok=True)
    config.write_atomic(_status_file(), json.dumps(s))


def _download(url: str, timeout: int) -> str:
    """The calendar's text. Errors never repeat the link, which is a secret."""
    try:
        with requests.get(url, timeout=timeout, stream=True) as r:
            if r.status_code != 200:
                raise Invalid(f"The calendar's server answered {r.status_code}. Check the link.")
            raw = r.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.Timeout:
        raise Invalid("The calendar did not answer in time.") from None
    except requests.RequestException:
        raise Invalid("The calendar could not be reached.") from None
    if len(raw) > MAX_BYTES:
        raise Invalid("That calendar is too large to read.")
    text = raw.decode("utf-8", "replace")
    if "BEGIN:VCALENDAR" not in text[:2000]:
        raise Invalid("That link is a web page, not a calendar. Use the .ics address.")
    return text


def _readable(text: str) -> bool:
    try:
        Calendar.from_ical(text)
        return True
    except Exception:
        return False


def _aware(value, fallback) -> datetime:
    return value if value.tzinfo else _attach(value, fallback)


def _attach(naive: datetime, tz) -> datetime:
    return tz.localize(naive) if hasattr(tz, "localize") else naive.replace(tzinfo=tz)


def _until(value: str, tz) -> str:
    """UNTIL as wall-clock time in the event's zone, which is how the rule is expanded."""
    if len(value) == 8:
        return value + "T235959"
    when = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        when = when.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)
    return when.strftime("%Y%m%dT%H%M%S")


def _listed(comp, name: str) -> list:
    """Every date under EXDATE or RDATE, which may repeat and may hold several each."""
    prop = comp.get(name)
    if prop is None:
        return []
    out = []
    for item in prop if isinstance(prop, list) else [prop]:
        out += [d.dt for d in getattr(item, "dts", [])]
    return out


def _starts(comp, start: datetime, length: timedelta, tz, lo: datetime, hi: datetime):
    zone = start.tzinfo or tz
    first = start.replace(tzinfo=None)
    rule = comp.get("rrule")
    if rule is None:
        found = [first]
    else:
        text = _UNTIL.sub(lambda m: "UNTIL=" + _until(m.group(1), zone),
                          rule.to_ical().decode())
        a = lo.astimezone(zone).replace(tzinfo=None) - length
        b = hi.astimezone(zone).replace(tzinfo=None)
        try:
            found = rrulestr(text, dtstart=first).between(a, b, inc=True)
        except (ValueError, TypeError):
            found = [first]
    found = [_attach(n, zone) for n in found]
    found += [_aware(d, zone) for d in _listed(comp, "rdate") if isinstance(d, datetime)]

    skip_at, skip_on = set(), set()
    for d in _listed(comp, "exdate"):
        if isinstance(d, datetime):
            skip_at.add(_aware(d, zone).astimezone(timezone.utc))
        else:
            skip_on.add(d)
    return [s for s in found if s.astimezone(timezone.utc) not in skip_at
            and s.astimezone(zone).date() not in skip_on]


def events(text: str, tz, lo: datetime, hi: datetime, source: str = "") -> list[Block]:
    """Timed, busy events between lo and hi. All-day and free-marked ones are left out."""
    cal = Calendar.from_ical(text)
    comps = list(cal.walk("VEVENT"))

    moved = set()
    for c in comps:
        rid = getattr(c.get("recurrence-id"), "dt", None)
        if isinstance(rid, datetime):
            moved.add((str(c.get("uid", "")), _aware(rid, tz).astimezone(timezone.utc)))

    out = []
    for c in comps:
        start = getattr(c.get("dtstart"), "dt", None)
        if not isinstance(start, datetime):
            continue
        if str(c.get("status", "")).upper() == "CANCELLED" \
                or str(c.get("transp", "")).upper() == "TRANSPARENT":
            continue
        end = getattr(c.get("dtend"), "dt", None)
        if isinstance(end, datetime):
            length = _aware(end, start.tzinfo or tz) - _aware(start, tz)
        else:
            length = getattr(c.get("duration"), "dt", None) or timedelta(0)
        if length <= timedelta(0):
            continue
        uid = str(c.get("uid", ""))
        title = " ".join(str(c.get("summary", "")).split())[:120] or "Busy"
        if c.get("recurrence-id") is not None:
            starts = [_aware(start, tz)]
        else:
            starts = [s for s in _starts(c, start, length, tz, lo, hi)
                      if (uid, s.astimezone(timezone.utc)) not in moved]
        for s in starts:
            s = s.astimezone(tz)
            if s < hi and s + length > lo:
                out.append(Block(start=s, end=s + length, title=title, kind="calendar",
                                 note=source))
    return sorted(out, key=lambda b: b.start)


def _own(cfg, first: date, last: date) -> list[Block]:
    out = []
    for b in load()["blocks"]:
        try:
            start, end = config.parse_hhmm(b["start"]), config.parse_hhmm(b["end"])
            if b.get("date"):
                days = [date.fromisoformat(b["date"])]
            else:
                want = {config.DAY_INDEX[d] for d in b.get("days") or []}
                skip = set(b.get("skip") or [])
                days = [first + timedelta(days=i) for i in range((last - first).days + 1)
                        if (first + timedelta(days=i)).weekday() in want]
                days = [d for d in days if d.isoformat() not in skip
                        and (not b.get("from") or d.isoformat() >= b["from"])
                        and (not b.get("until") or d.isoformat() <= b["until"])]
        except (KeyError, ValueError, TypeError):
            continue
        out += [Block(start=cfg.at(d, start), end=cfg.at(d, end), title=b.get("name") or "Busy",
                      kind="busy", note=b.get("id", "")) for d in days if first <= d <= last]
    return out


def _linked(cfg, first: date, last: date) -> list[Block]:
    lo, hi = cfg.at(first, dtime(0, 0)), cfg.at(last + timedelta(days=1), dtime(0, 0))
    out = []
    for link in load()["links"]:
        try:
            out += events(_cache(link["id"]).read_text(encoding="utf-8"), cfg.tz, lo, hi,
                          source=link.get("name", ""))
        except Exception:
            # One unreadable calendar must not take the plan down with it.
            continue
    return out


def _stamp(path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def busy_by_day(cfg, first: date | None = None, last: date | None = None
                ) -> dict[date, list[Block]]:
    first = first or cfg.now().date()
    last = last or first + timedelta(days=HORIZON_DAYS)
    key = (first, last, str(cfg.tz), str(_file()), _stamp(_file()),
           tuple(_stamp(_cache(link["id"])) for link in load()["links"]))
    if key in _memo:
        return _memo[key]
    out: dict[date, list[Block]] = {}
    for b in _own(cfg, first, last) + _linked(cfg, first, last):
        day = b.start.date()
        while day <= (b.end - timedelta(microseconds=1)).date():
            if first <= day <= last:
                out.setdefault(day, []).append(b)
            day += timedelta(days=1)
    if len(_memo) > 8:
        _memo.clear()
    _memo[key] = out
    return out


def refresh(max_age_minutes: float = 0, timeout: int = 20) -> list[dict]:
    """Fetch each linked calendar; a failed fetch keeps the last good copy."""
    done = []
    for link in load()["links"]:
        cached = _cache(link["id"])
        age = (datetime.now().timestamp() - _stamp(cached)) / 60
        if max_age_minutes and cached.exists() and age < max_age_minutes:
            continue
        try:
            text = _download(link["url"], timeout)
            if not _readable(text):
                raise Invalid("The calendar could not be read.")
        except Invalid as e:
            _set_status(link["id"], False, str(e))
            done.append({"name": link.get("name", ""), "ok": False, "error": str(e)})
            continue
        cached.parent.mkdir(parents=True, exist_ok=True)
        config.write_atomic(cached, text, mode=0o600)
        _set_status(link["id"], True)
        done.append({"name": link.get("name", ""), "ok": True, "error": ""})
    return done


def _clean_url(value) -> str:
    url = str(value or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parts = urlparse(url)
    if parts.scheme != "https" or not parts.hostname:
        raise Invalid("The link starts with https:// or webcal://.")
    return url


def add_link(name, url, timeout: int = 20) -> dict:
    d = load()
    if len(d["links"]) >= MAX_LINKS:
        raise Invalid(f"You can link up to {MAX_LINKS} calendars. Remove one first.")
    url = _clean_url(url)
    if any(link["url"] == url for link in d["links"]):
        raise Invalid("That calendar is already linked.")
    text = _download(url, timeout)
    if not _readable(text):
        raise Invalid("That calendar could not be read.")
    link = {"id": secrets.token_hex(4),
            "name": " ".join(str(name or "").split())[:40] or urlparse(url).hostname,
            "url": url}
    cache = _cache(link["id"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    config.write_atomic(cache, text, mode=0o600)
    d["links"].append(link)
    _save(d)
    _set_status(link["id"], True)
    return link


def remove_link(link_id) -> bool:
    d = load()
    keep = [link for link in d["links"] if link["id"] != link_id]
    if len(keep) == len(d["links"]):
        return False
    d["links"] = keep
    _save(d)
    _cache(str(link_id)).unlink(missing_ok=True)
    s = _status()
    if s.pop(link_id, None) is not None:
        config.write_atomic(_status_file(), json.dumps(s))
    return True


def _clean_block(data: dict, block_id: str | None = None) -> dict:
    name = " ".join(str(data.get("name") or "").split())[:60]
    if not name:
        raise Invalid("Give it a name, like Gym or Dentist.")
    start, end = hhmm(data.get("start")), hhmm(data.get("end"))
    if not start or not end:
        raise Invalid("Give a start and an end time.")
    if end <= start:
        raise Invalid("It has to end after it starts.")
    block = {"id": block_id or secrets.token_hex(4), "name": name, "start": start, "end": end}
    if data.get("date"):
        try:
            block["date"] = date.fromisoformat(str(data["date"])).isoformat()
        except ValueError:
            raise Invalid("Pick the date.") from None
        return block
    picked = [str(x).strip().lower()[:3] for x in data.get("days") or []]
    days = [x for x in DAYS if x in picked]
    if not days or len(days) != len(set(picked)):
        raise Invalid("Pick a date, or the days of the week it repeats on.")
    block["days"] = days
    skip = []
    for s in data.get("skip") or []:
        try:
            skip.append(date.fromisoformat(str(s)).isoformat())
        except ValueError:
            continue
    if skip:
        block["skip"] = sorted(set(skip))[-120:]
    for k in ("from", "until"):
        if data.get(k):
            try:
                block[k] = date.fromisoformat(str(data[k])).isoformat()
            except ValueError:
                pass
    return block


def add_block(data: dict) -> dict:
    d = load()
    if len(d["blocks"]) >= MAX_BLOCKS:
        raise Invalid("Too many busy times. Remove some first.")
    block = _clean_block(data)
    d["blocks"].append(block)
    _save(d)
    return block


def _find(d: dict, block_id) -> int:
    for i, b in enumerate(d["blocks"]):
        if b.get("id") == block_id:
            return i
    raise Invalid("That entry is already gone.")


def update_block(block_id, data: dict) -> dict:
    d = load()
    i = _find(d, block_id)
    merged = {**d["blocks"][i], **{k: data[k] for k in ("name", "start", "end") if k in data}}
    if data.get("date"):
        merged = {**merged, "date": data["date"], "days": None, "skip": None}
    elif data.get("days"):
        merged = {**merged, "days": data["days"], "date": None}
    d["blocks"][i] = _clean_block(merged, block_id)
    _save(d)
    return d["blocks"][i]


def skip_block(block_id, on) -> None:
    """Drop one day of a weekly entry; a one-time entry is removed."""
    d = load()
    i = _find(d, block_id)
    b = d["blocks"][i]
    if b.get("date"):
        del d["blocks"][i]
    else:
        d["blocks"][i] = _clean_block({**b, "skip": [*(b.get("skip") or []), on]}, block_id)
    _save(d)


def detach_block(block_id, on, data: dict) -> dict:
    """Change one day of a weekly entry: that day is skipped and a one-time entry added."""
    d = load()
    b = d["blocks"][_find(d, block_id)]
    one = _clean_block({"name": data.get("name") or b["name"], "date": data.get("date") or on,
                        "start": data.get("start") or b["start"],
                        "end": data.get("end") or b["end"]})
    if not b.get("date"):
        skip_block(block_id, on)
        d = load()
    else:
        d["blocks"] = [x for x in d["blocks"] if x.get("id") != block_id]
    d["blocks"].append(one)
    _save(d)
    return one


def split_block(block_id, on, data: dict | None = None) -> None:
    """This day and after: the entry ends the day before; `data` starts a changed one there."""
    d = load()
    i = _find(d, block_id)
    b = d["blocks"][i]
    try:
        day = date.fromisoformat(str(on))
    except ValueError:
        raise Invalid("Pick the date.") from None
    tail = None
    if data is not None:
        changed = {k: data[k] for k in ("name", "start", "end", "days") if data.get(k)}
        tail = _clean_block({**b, **changed, "from": day.isoformat()}
                            if not b.get("date") else {**b, **changed})
    prev = (day - timedelta(days=1)).isoformat()
    if b.get("date") or (b.get("from") or "") > prev:
        del d["blocks"][i]
    else:
        d["blocks"][i] = _clean_block({**b, "until": prev}, block_id)
    if tail:
        d["blocks"].append(tail)
    _save(d)


def set_blocks(blocks) -> None:
    d = load()
    d["blocks"] = [_clean_block(b, str(b.get("id") or "") or None)
                   for b in list(blocks or [])[:MAX_BLOCKS] if isinstance(b, dict)]
    d["started"] = True
    _save(d)


def start() -> None:
    d = load()
    d["started"] = True
    _save(d)


def remove_block(block_id) -> bool:
    d = load()
    keep = [b for b in d["blocks"] if b.get("id") != block_id]
    if len(keep) == len(d["blocks"]):
        return False
    d["blocks"] = keep
    _save(d)
    return True


def links_public() -> list[dict]:
    """Linked calendars for the page and the status report, without their addresses."""
    status, out = _status(), []
    for link in load()["links"]:
        s = status.get(link["id"]) or {}
        out.append({"id": link["id"], "name": link.get("name", ""),
                    "host": urlparse(link.get("url", "")).hostname or "",
                    "ok": bool(s.get("ok")), "error": s.get("error", ""),
                    "last_ok": s.get("last_ok")})
    return out


def meta(cfg) -> dict:
    saved = load()
    hours = cfg.commitments.get("day") or {}
    return {"started": saved["started"],
            "wake": hhmm(hours.get("wake")) or "07:00",
            "sleep": hhmm(hours.get("sleep")) or "23:00",
            "blocks": sorted(saved["blocks"], key=lambda b: (b.get("date") or "", b["start"])),
            "week": settings_edit.week_rows(),
            "meals": settings_edit.meal_rows(),
            "links": links_public()}


def view(cfg, first: date | None = None, days: int = 7) -> dict:
    today = cfg.now().date()
    days = min(max(int(days), 1), 42)
    first = min(max(first or today, today - timedelta(days=400)),
                today + timedelta(days=HORIZON_DAYS))
    busy = busy_by_day(cfg, first, first + timedelta(days=days - 1))
    out = []
    for i in range(days):
        day = first + timedelta(days=i)
        fixed, gaps = build_day(cfg, day, busy.get(day, []))
        out.append({
            "date": day.isoformat(),
            "free_minutes": sum(g.minutes for g in gaps if g.minutes >= MIN_GAP),
            "blocks": [{"start": b.start.isoformat(), "end": b.end.isoformat(),
                        "title": b.title, "kind": b.kind,
                        "id": b.note if b.kind == "busy" else "",
                        "rec": int(b.note[4:]) if b.note.startswith("rec:") else None,
                        "meal": int(b.note[5:]) if b.note.startswith("meal:") else None,
                        "from": b.note if b.kind == "calendar" else ""} for b in fixed],
        })
    return {"start": first.isoformat(), "days": out}
