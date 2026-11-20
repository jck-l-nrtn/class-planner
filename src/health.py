"""How fresh, and how healthy, each input is."""
from __future__ import annotations

from datetime import datetime, timezone

from . import calendars
from .ls.refresh import read_status, FIX
from .store import Store

STALE_HOURS = {"canvas": 12, "learningsuite": 36, "ls_ics": 36, "ls_prep": 36,
               "manual": 10 ** 6}
LABEL = {"canvas": "Canvas", "learningsuite": "LearningSuite gradebooks",
         "ls_ics": "LearningSuite iCal feeds", "ls_prep": "Class prep (schedule feed)",
         "manual": "Hand-entered assignments"}


def _age_h(iso: str | None) -> float | None:
    if not iso:
        return None
    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


def ago(hours: float | None) -> str:
    if hours is None:
        return "never"
    if hours < 1:
        return f"{max(int(hours * 60), 1)}m ago"
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f} days ago"


TOKEN_WARN_DAYS = 7
TOKEN_FIX = ("Canvas tokens stop working after a set time (30 days at many schools). "
             "Make a new one and paste it in; nothing else changes.")


def canvas_token(s: dict, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    row = {"name": "canvas_token", "label": "Canvas token", "last_ok": "",
           "stale": True, "fix": TOKEN_FIX, "action": "canvas_token"}
    if not s.get("ok") and "401" in s.get("message", ""):
        return {**row, "ok": False, "detail": "Canvas is refusing the saved token."}
    expires = (s.get("extra") or {}).get("token_expires")
    if not expires:
        return None
    try:
        when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError:
        return None
    days = (when - now).total_seconds() / 86400
    if days > TOKEN_WARN_DAYS:
        return None
    day = f"{when.astimezone():%b} {when.astimezone().day}"
    if days <= 0:
        return {**row, "ok": False, "detail": f"expired on {day}"}
    left = "today" if days < 1 else "tomorrow" if days < 2 else f"in {int(days)} days"
    return {**row, "ok": True, "detail": f"expires {left} ({day})"}


def report(cfg, store: Store | None = None) -> list[dict]:
    store = store or Store()
    out = []
    for name, s in store.sources.items():
        if s.get("message", "").startswith("skipped"):
            continue
        age = _age_h(s.get("last_ok"))
        out.append({
            "name": name, "label": LABEL.get(name, name), "ok": bool(s.get("ok")),
            "last_ok": ago(age), "detail": s.get("message", ""),
            "stale": age is None or age > STALE_HOURS.get(name, 36),
        })
        if name == "canvas" and (token := canvas_token(s)):
            out.append(token)
    for course, st in read_status().items():
        if course not in cfg.courses:
            continue
        age = _age_h(st.get("last_ok"))
        ok = st.get("state") in ("ok", "empty_course")
        out.append({
            "name": f"ls:{course}", "label": f"LearningSuite · {course}", "ok": ok,
            "last_ok": ago(age),
            "detail": "" if ok else f"{st.get('state')}: {st.get('detail', '')}",
            "fix": "" if ok else FIX.get(st.get("state"), ""),
            "stale": age is None or age > STALE_HOURS["learningsuite"],
        })
    for link in calendars.links_public():
        age = _age_h(link["last_ok"])
        out.append({
            "name": f"cal:{link['id']}", "label": f"Calendar · {link['name']}",
            "ok": link["ok"], "last_ok": ago(age), "detail": link["error"],
            "fix": "" if link["ok"] else "Remove it on the Calendar page and add the link again.",
            "stale": age is None or age > calendars.STALE_HOURS,
        })
    return out


def warnings(items: list[dict]) -> list[str]:
    return [f"{i['label']}: last good {i['last_ok']}"
            + (f" ({i['detail']})" if i["detail"] and not i["ok"] else "")
            for i in items if i["stale"] or not i["ok"]]
