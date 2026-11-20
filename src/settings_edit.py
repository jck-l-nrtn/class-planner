"""Change an existing planner's courses and week without rebuilding them."""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import date
from pathlib import Path

import yaml

from . import config

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


class Invalid(ValueError):
    """Bad input. `errors` is {field: what is wrong}."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def _path(name: str) -> Path:
    return config.CONFIG_DIR / name


def _plain(name: str) -> dict:
    """The file as the planner itself reads it."""
    p = _path(name)
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def hhmm(value) -> str:
    """'9:00', '09:00', or the number YAML makes of an unquoted 10:30 -> 'HH:MM'."""
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value // 60:02d}:{value % 60:02d}"
    m = _HHMM.fullmatch(str(value or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def _days(value) -> list[str] | None:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return None
    out = [str(d).strip().lower()[:3] for d in value]
    return [d for d in DAYS if d in out] if out and all(d in DAYS for d in out) else None


def _class_rows(recurring, key: str) -> list:
    return [r for r in recurring or []
            if isinstance(r, dict) and r.get("kind") == "class" and r.get("course") == key]


def current() -> dict:
    """{"courses": [...], "week": {...}} as they are saved now."""
    com = _plain("commitments.yaml")
    recurring = com.get("recurring") or []
    courses = []
    for c in _plain("courses.yaml").get("courses") or []:
        rows = _class_rows(recurring, c.get("key"))
        meets, locked = None, len(rows) > 1
        if len(rows) == 1:
            days, start, end = _days(rows[0].get("days")), hhmm(rows[0].get("start")), \
                hhmm(rows[0].get("end"))
            if days and start and end:
                meets = {"days": days, "start": start, "end": end}
            else:
                locked = True
        courses.append({"key": c.get("key"), "name": c.get("name") or c.get("key"),
                        "credits": c.get("credits") or 3,
                        "canvas_id": c.get("canvas_id"), "ls_course_id": c.get("ls_course_id"),
                        "meets": meets, "meets_locked": locked})
    day, term = com.get("day") or {}, com.get("term") or {}
    return {"courses": courses,
            "week": {"timezone": com.get("timezone") or "",
                     "wake": hhmm(day.get("wake")), "sleep": hhmm(day.get("sleep")),
                     "term_start": str(term.get("start") or ""),
                     "term_end": str(term.get("end") or "")}}


def clean_meets(value, field: str, errors: dict) -> dict | None:
    """{"days": [...], "start": "HH:MM", "end": "HH:MM"} or None for no fixed time."""
    if not value:
        return None
    days = _days(value.get("days")) if isinstance(value, dict) else None
    start = hhmm(value.get("start")) if isinstance(value, dict) else ""
    end = hhmm(value.get("end")) if isinstance(value, dict) else ""
    if not days and not start and not end:
        return None
    if not days:
        errors[field] = "Pick the days it meets."
    elif not start or not end:
        errors[field] = "Give a start and an end time."
    elif end <= start:
        errors[field] = "It has to end after it starts."
    else:
        return {"days": days, "start": start, "end": end}
    return None


def clean_credits(value, field: str, errors: dict):
    try:
        n = float(value if value not in (None, "") else 3)
        if not 0 < n <= 12:
            raise ValueError
    except (TypeError, ValueError):
        errors[field] = "Credits should be a number like 3."
        return 3
    return int(n) if n == int(n) else n


def clean_week(week: dict, errors: dict) -> dict:
    out = {}
    if "timezone" in week:
        tz = str(week["timezone"] or "").strip()
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
            out["timezone"] = tz
        except Exception:
            errors["timezone"] = "Not a timezone name. Example: America/Denver."
    for f in ("wake", "sleep"):
        if f in week:
            out[f] = hhmm(week[f])
            if not out[f]:
                errors[f] = "Use a time like 07:00."
    for f in ("term_start", "term_end"):
        if f in week:
            try:
                out[f] = date.fromisoformat(str(week[f] or ""))
            except ValueError:
                errors[f] = "Dates look like 2026-09-01."
    if isinstance(out.get("term_start"), date) and isinstance(out.get("term_end"), date) \
            and out["term_end"] <= out["term_start"]:
        errors["term_end"] = "The term has to end after it starts."
    return out


def _child(node, key: str):
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if getattr(k, "value", None) == key:
                return v
    return None


def _true_end(node) -> int:
    """Where a value's text really stops."""
    if isinstance(node, yaml.ScalarNode) or node.flow_style or not node.value:
        return node.end_mark.index
    last = node.value[-1]
    return _true_end(last[1] if isinstance(last, tuple) else last)


def _line_start(text: str, i: int) -> int:
    return text.rfind("\n", 0, i) + 1


def _line_end(text: str, i: int) -> int:
    j = text.find("\n", i)
    return len(text) if j == -1 else j


def _scalar(node, literal: str, what: str) -> tuple[int, int, str]:
    if not isinstance(node, yaml.ScalarNode) or node.start_mark.index == node.end_mark.index:
        raise Invalid({"save": f"Could not find `{what}` to change; edit the file by hand."})
    return node.start_mark.index, node.end_mark.index, literal


def _days_edit(node, days: list[str], what: str) -> tuple[int, int, str]:
    if isinstance(node, yaml.SequenceNode) and not node.flow_style and node.value:
        pad = "\n" + " " * node.start_mark.column
        return node.start_mark.index, _true_end(node), pad.join(f"- {d}" for d in days)
    if isinstance(node, (yaml.SequenceNode, yaml.ScalarNode)) \
            and node.start_mark.index != node.end_mark.index:
        return node.start_mark.index, node.end_mark.index, "[" + ", ".join(days) + "]"
    raise Invalid({"save": f"Could not find `{what}` to change; edit the file by hand."})


def _class_text(key: str, name: str, meets: dict, flow: bool, pad: str) -> str:
    fields = [("name", json.dumps(name, ensure_ascii=False)), ("kind", "class"), ("course", key),
              ("days", "[" + ", ".join(meets["days"]) + "]"),
              ("start", f'"{meets["start"]}"'), ("end", f'"{meets["end"]}"'),
              ("location", "campus")]
    if flow:
        return "- {" + ", ".join(f"{k}: {v}" for k, v in fields) + "}"
    return ("\n" + pad + "  ").join(("- " if i == 0 else "") + f"{k}: {v}"
                                    for i, (k, v) in enumerate(fields))


def _is_class(item, key: str) -> bool:
    return (isinstance(item, yaml.MappingNode)
            and getattr(_child(item, "kind"), "value", None) == "class"
            and getattr(_child(item, "course"), "value", None) == key)


def _commitment_edits(text: str, update: list, week: dict, names: dict, did: list) -> list:
    root = yaml.compose(text)
    edits: list[tuple[int, int, str]] = []
    rec = _child(root, "recurring")
    items = rec.value if isinstance(rec, yaml.SequenceNode) else []
    removed = 0
    added: list[str] = []
    for c in update:
        if "meets" not in c:
            continue
        key, meets = c["key"], c["meets"]
        mine = [it for it in items if _is_class(it, key)]
        if len(mine) > 1:
            continue
        if mine and meets:
            row = mine[0]
            d, s, e = _child(row, "days"), _child(row, "start"), _child(row, "end")
            before = next((x for x in current()["courses"] if x["key"] == key), {}).get("meets")
            if before == meets:
                continue
            edits.append(_days_edit(d, meets["days"], f"{key} days"))
            edits.append(_scalar(s, f'"{meets["start"]}"', f"{key} start"))
            edits.append(_scalar(e, f'"{meets["end"]}"', f"{key} end"))
            did.append(f"{key}: meets {'/'.join(meets['days'])} {meets['start']}-{meets['end']}")
        elif mine and not meets:
            row = mine[0]
            if len(items) - removed <= 1:
                raise Invalid({f"meets:{key}": "This is the only entry in your week; "
                               "remove it in config/commitments.yaml by hand."})
            start = _line_start(text, row.start_mark.index)
            end = min(_line_end(text, _true_end(row)) + 1, len(text))
            edits.append((start, end, ""))
            removed += 1
            did.append(f"{key}: no fixed meeting time")
        elif meets:
            if not items:
                raise Invalid({f"meets:{key}": "Your week has no `recurring` list to add "
                               "this to; add one in config/commitments.yaml."})
            pad = " " * rec.start_mark.column
            added.append(pad + _class_text(key, names.get(key) or key, meets,
                                           bool(items[-1].flow_style), pad))
            did.append(f"{key}: meets {'/'.join(meets['days'])} {meets['start']}-{meets['end']}")
    if added:
        at = _line_end(text, _true_end(items[-1]))
        edits.append((at, at, "".join("\n" + a for a in added)))
    cur = current()["week"]
    if "timezone" in week and week["timezone"] != cur["timezone"]:
        node = _child(root, "timezone")
        quote = node.style if isinstance(node, yaml.ScalarNode) and node.style in ('"', "'") else ""
        edits.append(_scalar(node, f"{quote}{week['timezone']}{quote}", "timezone"))
        did.append(f"timezone {week['timezone']}")
    for f in ("wake", "sleep"):
        if f in week and week[f] != cur[f]:
            edits.append(_scalar(_child(_child(root, "day"), f), f'"{week[f]}"', f"day.{f}"))
            did.append(f"{f} {week[f]}")
    for f, k in (("term_start", "start"), ("term_end", "end")):
        if f in week and week[f].isoformat() != cur[f]:
            edits.append(_scalar(_child(_child(root, "term"), k), week[f].isoformat(),
                                 f"term.{k}"))
            did.append(f"term {k} {week[f].isoformat()}")
    return edits


def _course_edits(text: str, update: list, did: list) -> list:
    root = yaml.compose(text)
    rows = _child(root, "courses")
    edits = []
    for c in update:
        if "credits" not in c:
            continue
        row = next((r for r in (rows.value if isinstance(rows, yaml.SequenceNode) else [])
                    if getattr(_child(r, "key"), "value", None) == c["key"]), None)
        node = _child(row, "credits")
        if node is not None and str(yaml.safe_load(node.value)) != str(c["credits"]):
            edits.append(_scalar(node, str(c["credits"]), f"{c['key']} credits"))
            did.append(f"{c['key']}: credits {c['credits']}")
    return edits


def _splice(text: str, edits: list) -> str:
    last = len(text) + 1
    for start, end, new in sorted(edits, reverse=True):
        if end > last:
            raise Invalid({"save": "Two changes touch the same place; save them one at a time."})
        text = text[:start] + new + text[end:]
        last = start
    return text


def _write(name: str, new: str) -> tuple[Path, Path]:
    path = _path(name)
    bak = path.with_name(f"{name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, bak)
    config.write_atomic(path, new)
    return path, bak


def apply(changes: dict) -> dict:
    errors: dict[str, str] = {}
    now = current()
    have = {c["key"]: c for c in now["courses"]}
    update = []
    for c in changes.get("update") or []:
        key = c.get("key")
        if key not in have:
            continue
        row = {"key": key}
        if "credits" in c:
            row["credits"] = clean_credits(c["credits"], f"credits:{key}", errors)
        if "meets" in c and not have[key]["meets_locked"]:
            row["meets"] = clean_meets(c["meets"], f"meets:{key}", errors)
        update.append(row)
    week = clean_week(changes.get("week") or {}, errors)
    if errors:
        raise Invalid(errors)

    did: list[str] = []
    names = {k: v["name"] for k, v in have.items()}
    plans = []
    for name, find in (("courses.yaml", lambda t: _course_edits(t, update, did)),
                       ("commitments.yaml",
                        lambda t: _commitment_edits(t, update, week, names, did))):
        path = _path(name)
        if not path.exists():
            continue
        old = path.read_text(encoding="utf-8")
        new = _splice(old, find(old))
        if new != old:
            plans.append((name, new))
    written = []
    try:
        for name, new in plans:
            written.append(_write(name, new))
        if written:
            config.Config()
            after = {c["key"]: c for c in current()["courses"]}
            for c in update:
                if "meets" in c and after[c["key"]]["meets"] != c["meets"]:
                    raise ValueError(f"{c['key']} meeting time did not save as intended")
                if "credits" in c and after[c["key"]]["credits"] != c["credits"]:
                    raise ValueError(f"{c['key']} credits did not save as intended")
    except Exception as e:
        for path, bak in written:
            shutil.copy2(bak, path)
        raise Invalid({"save": f"Nothing was changed: the result could not be read back "
                               f"({type(e).__name__}: {str(e)[:160]})."}) from e
    return {"changed": did, "backups": [bak.name for _, bak in written]}


_undo: dict = {}


def _dates(value) -> list[str]:
    return sorted(str(x) for x in value or [])


def week_rows() -> list[dict]:
    """The weekly entries of commitments.yaml, each with its position in the file."""
    out = []
    for i, r in enumerate(_plain("commitments.yaml").get("recurring") or []):
        if not isinstance(r, dict) or str(r.get("name", "")).startswith("EXAMPLE"):
            continue
        days, start, end = _days(r.get("days")), hhmm(r.get("start")), hhmm(r.get("end"))
        if days and start and end:
            out.append({"i": i, "name": str(r.get("name") or ""),
                        "kind": r.get("kind") or "standing", "days": days,
                        "start": start, "end": end, "skip": _dates(r.get("skip_dates")),
                        "until": str(r.get("until") or "")})
    return out


def meal_rows() -> list[dict]:
    out = []
    for i, m in enumerate(_plain("commitments.yaml").get("meals") or []):
        if not isinstance(m, dict):
            continue
        start = hhmm(m.get("at"))
        try:
            end = int(start[:2]) * 60 + int(start[3:]) + int(m.get("minutes"))
        except (TypeError, ValueError):
            continue
        if start and end < 1440:
            out.append({"i": i, "name": str(m.get("name") or "").title(), "start": start,
                        "end": hhmm(end), "skip": _dates(m.get("skip_dates")),
                        "until": str(m.get("until") or "")})
    return out


def _key_edit(text: str, row, key: str, value: str) -> tuple[int, int, str]:
    """Set `key` on one entry, adding it in the entry's own style if it is not there."""
    node = _child(row, key)
    if node is not None and node.start_mark.index != node.end_mark.index:
        return node.start_mark.index, _true_end(node), value
    if row.flow_style:
        at = row.end_mark.index - 1
        if text[at] != "}":
            raise Invalid({"save": "Could not change that entry; edit the file by hand."})
        return at, at, f", {key}: {value}"
    at = _line_end(text, _true_end(row))
    return at, at, "\n" + " " * row.value[0][0].start_mark.column + f"{key}: {value}"


def _day(value) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise Invalid({"save": "Pick the date."}) from None


def edit_week(index, was: dict, change: dict, section: str = "recurring") -> None:
    """Change one entry of `recurring` or `meals` in place.

    change is {"delete": True}, {"skip": date}, {"until": date} or new days/start/end.
    """
    meals = section == "meals"
    read = meal_rows if meals else week_rows
    path = _path("commitments.yaml")
    old = path.read_text(encoding="utf-8")
    row = next((r for r in read() if r["i"] == index), None)
    was = was if isinstance(was, dict) else {}
    if not row or (row["name"], row["start"], row["end"]) != (
            was.get("name"), was.get("start"), was.get("end")):
        raise Invalid({"save": "Your week changed since this page loaded. Reload it."})
    seq = _child(yaml.compose(old), "meals" if meals else "recurring")
    node = seq.value[index]
    want, gone, edits = dict(row), False, []
    if change.get("delete"):
        if seq.flow_style or (not meals and len(seq.value) <= 1):
            raise Invalid({"save": "That one can only be removed in config/commitments.yaml."})
        edits.append((_line_start(old, node.start_mark.index),
                      min(_line_end(old, _true_end(node)) + 1, len(old)), ""))
        gone = True
    elif change.get("skip"):
        want["skip"] = sorted({*row["skip"], _day(change["skip"])})
        edits.append(_key_edit(old, node, "skip_dates", "[" + ", ".join(want["skip"]) + "]"))
    elif change.get("until"):
        want["until"] = _day(change["until"])
        edits.append(_key_edit(old, node, "until", want["until"]))
    else:
        errors: dict[str, str] = {}
        meets = clean_meets({"days": change.get("days") or row.get("days") or list(DAYS),
                             "start": change.get("start") or row["start"],
                             "end": change.get("end") or row["end"]}, "save", errors)
        if errors or not meets:
            raise Invalid(errors or {"save": "Pick the days and times."})
        want.update({k: meets[k] for k in ("start", "end")})
        if meals:
            minutes = (int(want["end"][:2]) * 60 + int(want["end"][3:])
                       - int(want["start"][:2]) * 60 - int(want["start"][3:]))
            if want["start"] != row["start"]:
                edits.append(_scalar(_child(node, "at"), f'"{want["start"]}"', "at"))
            if (want["start"], want["end"]) != (row["start"], row["end"]):
                edits.append(_scalar(_child(node, "minutes"), str(minutes), "minutes"))
        else:
            want["days"] = meets["days"]
            if want["days"] != row["days"]:
                edits.append(_days_edit(_child(node, "days"), want["days"], "days"))
            for f in ("start", "end"):
                if want[f] != row[f]:
                    edits.append(_scalar(_child(node, f), f'"{want[f]}"', f))
    new = _splice(old, edits)
    if new == old:
        return
    count = len(read())
    bak = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d')}")
    if not bak.exists():
        shutil.copy2(path, bak)
    config.write_atomic(path, new)
    try:
        config.Config()
        after = read()
        ok = len(after) == count - 1 if gone else \
            next((r for r in after if r["i"] == index), None) == want
        if not ok:
            raise ValueError("it did not read back as intended")
    except Exception as e:
        config.write_atomic(path, old)
        raise Invalid({"save": f"Nothing was changed ({str(e)[:120]})."}) from e
    _undo.update(before=old, after=new)


def undo_week() -> bool:
    """Put commitments.yaml back as it was before the last calendar change, if untouched since."""
    path = _path("commitments.yaml")
    if not _undo or path.read_text(encoding="utf-8") != _undo["after"]:
        return False
    config.write_atomic(path, _undo["before"])
    _undo.clear()
    return True
