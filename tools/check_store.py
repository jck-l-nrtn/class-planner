"""Prove a change did not lose anything the user owns."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DONE = {"submitted", "graded", "dropped"}


def facts(store: dict) -> dict:
    """Reduce a store of any schema version to the user-owned facts in it."""
    tasks = store.get("tasks", {})
    overrides = store.get("overrides", {})
    moved = store.get("aliases", {})
    return {"tasks": tasks, "overrides": overrides, "log": store.get("log", []),
            "moved": moved}


def resolve(uid: str, moved: dict) -> str:
    seen = set()
    while uid in moved and uid not in seen:
        seen.add(uid)
        uid = moved[uid]
    return uid


def compare(old: dict, new: dict) -> tuple[list[str], list[str]]:
    lost, notes = [], []
    o, n = facts(old), facts(new)
    moved = n["moved"]

    new_log = {(resolve(e["uid"], moved), e["minutes"], e["at"]) for e in n["log"]}
    new_log |= {(e.get("orig_uid", e["uid"]), e["minutes"], e["at"]) for e in n["log"]}
    for e in o["log"]:
        if (resolve(e["uid"], moved), e["minutes"], e["at"]) not in new_log \
                and (e["uid"], e["minutes"], e["at"]) not in new_log:
            lost.append(f"time log entry lost: {e['uid']} {e['minutes']}m at {e['at']}")

    for uid, t in o["tasks"].items():
        nu = resolve(uid, moved)
        nt = n["tasks"].get(nu)
        ov = o["overrides"].get(uid, {})
        owned = t.get("spent_minutes", 0) or "status" in ov or "est_minutes" in ov
        if nt is None:
            if owned:
                lost.append(f"task with user data vanished: {uid} ({t['title'][:40]})")
            else:
                notes.append(f"task gone (no user data on it): {uid} {t['title'][:40]}")
            continue
        if nt.get("spent_minutes", 0) < t.get("spent_minutes", 0):
            lost.append(f"spent_minutes shrank on {uid}: "
                        f"{t['spent_minutes']} -> {nt.get('spent_minutes', 0)}")
        if ov.get("status") in DONE and nt.get("status") not in DONE:
            lost.append(f"manual '{ov['status']}' lost on {uid} ({t['title'][:40]}): "
                        f"now {nt.get('status')}")
        if ov.get("status") == "in_progress" and nt.get("status") == "not_started":
            lost.append(f"manual in_progress lost on {uid}")
        if "est_minutes" in ov and nt.get("est_minutes") != ov["est_minutes"]:
            notes.append(f"estimate changed on {uid}: {ov['est_minutes']} -> "
                         f"{nt.get('est_minutes')}")
    return lost, notes


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    backup = Path(sys.argv[1])
    old = json.loads((backup / "store.json").read_text())
    new = json.loads((ROOT / "data" / "store.json").read_text())
    lost, notes = compare(old, new)

    old_j = (backup / "journal.jsonl").read_text().splitlines()
    new_j = (ROOT / "data" / "journal.jsonl").read_text().splitlines()
    if new_j[:len(old_j)] != old_j:
        lost.append("journal.jsonl: earlier entries were altered or removed")

    o, n = facts(old), facts(new)
    print(f"backup : {len(o['tasks'])} tasks, {len(o['overrides'])} overrides, "
          f"{len(o['log'])} time logs, {len(old_j)} journal lines")
    print(f"live   : {len(n['tasks'])} tasks, {len(n['overrides'])} overrides, "
          f"{len(n['log'])} time logs, {len(new_j)} journal lines")
    for x in notes:
        print(f"  note: {x}")
    for x in lost:
        print(f"  LOST: {x}")
    print("RESULT:", "NOTHING LOST" if not lost else f"{len(lost)} LOSSES")
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(main())
