"""Effort estimates from an agent that has read the assignment."""
from __future__ import annotations

import time

from . import agent
from .content import ContentStore
from .store import Store

EST_RANGE = (5, 720)
WEIGHT_RANGE = (0.1, 5.0)

SCHEMA = {
    "type": "object",
    "properties": {"estimates": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "uid": {"type": "string"},
            "est_minutes": {"type": "integer"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "agent_weight": {"type": "number"},
            "agent_note": {"type": "string"},
        },
        "required": ["uid", "est_minutes", "confidence", "agent_weight", "agent_note"],
        "additionalProperties": False}}},
    "required": ["estimates"],
    "additionalProperties": False,
}

PREAMBLE = """You are estimating how long university coursework will take one \
specific student, so a scheduler can protect the right amount of time.

For each assignment, return:
  est_minutes   focused working minutes, excluding breaks. Be concrete: think in
                terms of the actual steps, then add them up.
  confidence    low / medium / high -- low when the description is thin or the
                assignment type is unfamiliar.
  agent_weight  priority multiplier. 1.0 means "the computed ranking is right".
                Above 1.0 only for something structurally more urgent than its
                grade weight implies (it gates later work, it needs a group, it
                needs materials the student must obtain first). Below 1.0 for
                busywork worth less attention than its points suggest.
  agent_note    ONE sentence, written to the student, saying what drove the
                number. This is shown in their plan.

Rules:
- grade_impact_pct is already exact. Do not recompute or second-guess it.
- Where a description is empty, say so in the note and lower confidence rather
  than inventing detail.
- Do not pad. A 10-point reading quiz is usually 20-40 minutes, not 2 hours.
- Exam entries are TOTAL preparation time across all sessions, not sitting time.
"""


def pending(store: Store, content: ContentStore, redo: bool = False) -> list[dict]:
    rows = []
    for t in store.open_tasks():
        if not t.schedulable or store.user_estimated(t.uid):
            continue
        rec = content.get_task(t.uid) or {}
        have = store.estimates.get(t.uid)
        if have and not redo and ("content_hash" not in have
                                  or have["content_hash"] == rec.get("hash")):
            continue
        rows.append({
            "uid": t.uid, "course": t.course_key, "title": t.title,
            "kind": t.kind.value, "category": t.category, "points": t.points,
            "grade_impact_pct": round((t.impact_override or 0) * 100, 3),
            "due_at": t.due_at.isoformat() if t.due_at else None,
            "hard_deadline": t.hard_deadline,
            "description": rec.get("description", ""), "content_hash": rec.get("hash"),
            "current": store.source_task(t.uid).est_minutes,
        })
    rows.sort(key=lambda r: (r["due_at"] or "9999", -r["grade_impact_pct"]))
    return rows


def build_prompt(rows: list[dict], calibration: dict) -> str:
    parts = [PREAMBLE]
    if calibration:
        parts.append("\nThis student's measured pace on finished work "
                     "(actual / estimated). Weight this heavily:")
        parts += [f"  {k}: {v:.2f}x" for k, v in sorted(calibration.items())]
    parts.append("\n--- ASSIGNMENTS ---")
    for r in rows:
        d = (r["description"] or "").strip()
        parts.append(
            f"\nuid: {r['uid']}\ncourse: {r['course']}  |  category: {r['category']}"
            f"  |  kind: {r['kind']}\ntitle: {r['title']}\n"
            f"points: {r['points']}  |  worth {r['grade_impact_pct']}% of the final "
            f"grade  |  due {r['due_at'] or 'no date'}"
            f"{'  |  NO LATE SUBMISSIONS' if r['hard_deadline'] else ''}\n"
            f"category default: {r['current']} min (replace it)\n"
            f"description: {d[:1400] if d else '(none provided)'}")
    parts.append(f"\n--- END ({len(rows)} assignments) ---\n"
                 f"Return an estimate for every uid listed.")
    return "\n".join(parts)


def check(row: dict) -> tuple[dict, list[str]]:
    """Range-check one answer before it can touch a schedule."""
    fields, problems = {}, []
    est = row.get("est_minutes")
    if isinstance(est, (int, float)) and EST_RANGE[0] <= est <= EST_RANGE[1]:
        fields["est_minutes"] = int(est)
    else:
        problems.append(f"{row.get('uid')}: est_minutes {est!r} outside "
                        f"{EST_RANGE[0]}..{EST_RANGE[1]}, ignored")
    w = row.get("agent_weight")
    if isinstance(w, (int, float)):
        fields["agent_weight"] = max(WEIGHT_RANGE[0], min(WEIGHT_RANGE[1], float(w)))
    if row.get("agent_note"):
        fields["agent_note"] = str(row["agent_note"])[:300]
    return fields, problems


def run(cfg, model: str = "sonnet", batch: int = 20, limit: int | None = None,
        redo: bool = False, timeout: int = 600, out=print) -> int:
    content = ContentStore()
    store = Store()
    rows = pending(store, content, redo)[:limit]
    skipped_user = sum(1 for t in store.open_tasks() if store.user_estimated(t.uid))
    if not rows:
        out(f"nothing to estimate: every open task has a current estimate"
            + (f" ({skipped_user} set by you, left alone)" if skipped_user else ""))
        return 0
    calibration = store.calibration()
    chunks = [rows[i:i + batch] for i in range(0, len(rows), batch)]
    out(f"{len(rows)} task(s) need an estimate, {len(chunks)} batch(es), asking {agent.describe(model, 'estimate')}"
        + (f"; {skipped_user} hand-set estimate(s) left alone" if skipped_user else ""))
    applied = failed = 0
    for i, chunk in enumerate(chunks, 1):
        t0 = time.time()
        try:
            answers = agent.run_json(build_prompt(chunk, calibration), SCHEMA,
                                     model, timeout, which="estimate").get("estimates", [])
        except agent.AgentError as e:
            failed += len(chunk)
            out(f"  batch {i}/{len(chunks)}: FAILED -- {e}")
            continue
        asked = {r["uid"]: r for r in chunk}
        got = 0
        with Store.edit("estimate-agent") as st:
            for a in answers:
                src = asked.get(a.get("uid"))
                fields, problems = check(a) if src else ({}, [])
                for p in problems:
                    out(f"    ! {p}")
                if src and fields and a["uid"] in st.tasks:
                    st.set_agent_estimate(a["uid"], fields, src["content_hash"])
                    got += 1
        applied += got
        out(f"  batch {i}/{len(chunks)}: {got}/{len(chunk)} applied in {time.time()-t0:.0f}s")
    out(f"{applied} estimate(s) applied, {failed} failed")
    return 0 if applied or not failed else 1
