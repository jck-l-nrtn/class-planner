"""Command-line entry point: ./plan <command>."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from .config import Config, OUT_DIR, ROOT, SetupNeeded
from .model import Status
from .store import NoSuchTask, Store, read_events
from .dates import fmt


AUTO_UPDATE = ("app", "sync", "today", "ls-refresh")


def cmd_courses(cfg, args):
    from .sources import canvas
    try:
        _, seen = canvas.fetch(cfg)
    except Exception as e:
        print(f"Canvas error: {e}", file=sys.stderr)
        return 1
    print(f"\n{'canvas_id':<12}{'code':<16}{'mapped to':<14}name")
    print("-" * 78)
    for c in sorted(seen, key=lambda x: x["name"]):
        print(f"{c['canvas_id']:<12}{(c['code'] or '')[:15]:<16}"
              f"{c['mapped_to'] or '(unmapped)':<14}{c['name']}")
    print("\nPaste the ids you want into config/courses.yaml as `canvas_id:`.\n")
    return 0


def cmd_sync(cfg, args):
    from . import calendars, sync
    code = sync.run(cfg)[0]
    for r in calendars.refresh():
        print(f"Calendar {r['name']}: " + ("ok" if r["ok"] else f"FAILED, {r['error']}"))
    return code


def cmd_ls_refresh(cfg, args):
    from .ls import refresh
    results = refresh.refresh(cfg, allow_browser=not args.no_browser,
                              interactive=getattr(args, "login", False),
                              via_browser=getattr(args, "via_browser", False),
                              linger=30 if getattr(args, "save_password", False) else 0)
    for r in results:
        print(r.line())
    code = refresh.exit_code(results)
    if code == 75:
        print("LearningSuite was not refreshed this run, and no re-login is needed; "
              "it will be retried.")
    if not args.no_sync:
        print()
        cmd_sync(cfg, args)
    return code


def cmd_ls_login(cfg, args):
    args.login, args.no_browser, args.via_browser, args.no_sync = True, False, False, False
    return cmd_ls_refresh(cfg, args)


def cmd_catch_up(cfg, args):
    """Set aside open work that was due before today (for a mid-term start)."""
    start_of_today = cfg.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if args.dry_run:
        tasks = Store().overdue_before(start_of_today)
        print(f"{len(tasks)} open assignment(s) were due before today:")
        for t in sorted(tasks, key=lambda t: t.due_at):
            print(f"  {fmt(t.due_at, '%b %-d')}  {t.course_key:<9} {t.title[:60]}")
        return 0
    with Store.edit("catch-up") as store:
        tasks = store.catch_up(start_of_today)
    print(f"Set aside {len(tasks)} assignment(s) that were due before today as already "
          f"handled." + (" Bring any of them back under Needs attention > Marked by you."
                         if tasks else ""))
    return 0


def cmd_update(cfg, args):
    """Get the newest published version now (it also happens by itself)."""
    from . import updater
    if updater.maybe_update(force=True):
        print("Restart the planner (close and reopen it) to use the new version.")
    return 0


def cmd_setup(cfg, args):
    from . import setup_wizard
    return setup_wizard.main(check_only=args.check)


def _model(cfg, args, which: str, default: str) -> str:
    """--model on the command line, else prefs `agent.<which>`, else the default."""
    return args.model or (cfg.prefs.get("agent") or {}).get(which) or default


def cmd_today(cfg, args):
    from . import daily
    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
    return daily.run(cfg, day, model=_model(cfg, args, "plan_model", "opus"))


def cmd_estimate(cfg, args):
    from . import estimate
    return estimate.run(cfg, model=_model(cfg, args, "estimate_model", "sonnet"),
                        batch=args.batch, limit=args.limit, redo=args.redo)


def cmd_app(cfg, args):
    from .web import server
    return server.serve(port=args.port, open_browser=not args.no_open)


def cmd_enrich(cfg, args):
    """Pull assignment text and feedback into the content store."""
    from .content import ContentStore
    from .enrich import enrich_canvas, enrich_learningsuite
    content = ContentStore()
    if cfg.env("CANVAS_TOKEN"):
        try:
            r = enrich_canvas(cfg, content)
            print(f"Canvas: {r['assignments']} assignments ({r['chars']:,} chars, "
                  f"{r['changed']} changed)")
        except Exception as e:
            print(f"Canvas enrich FAILED: {e}", file=sys.stderr)
    else:
        print("Canvas: skipped (no token)")
    r = enrich_learningsuite(cfg, content)
    print(f"LearningSuite: {r['assignments']} assignments ({r['chars']:,} chars, "
          f"{r['changed']} changed, {r['missing_desc']} with no description)")
    return 0


def _scored(cfg, store):
    from .prioritize import score_all
    from .workload import calibration
    tasks = store.open_tasks()
    from . import calendars
    calib = calibration(cfg, store, tasks, cfg.now().date())
    return score_all(cfg, tasks, external_by_day=calendars.busy_by_day(cfg),
                     calibration=calib)


def cmd_tasks(cfg, args):
    store = Store()
    print(f"\n{'course':<9}{'due':<14}{'left':>6} {'%grade':>7}  {'title':<46}uid")
    print("-" * 110)
    for s in _scored(cfg, store):
        t = s.task
        if not t.schedulable:
            continue
        due = f"{fmt(t.due_at, '%b %-d %H:%M')}" if t.due_at else "-"
        print(f"{t.course_key:<9}{due:<14}{t.remaining_minutes:>5}m "
              f"{s.grade_impact*100:>6.2f}%  {t.title[:44]:<46}{t.uid}")
    return 0


def cmd_why(cfg, args):
    """Explain one task's rank: every factor the score was built from."""
    store = Store()
    uid = store.resolve(args.uid)
    scored = _scored(cfg, store)
    ranked = [s for s in scored if s.task.schedulable]
    hit = next((s for s in scored if s.task.uid == uid), None)
    if not hit:
        t = store.tasks.get(uid)
        print(f"{args.uid}: " + (f"'{t.title}' is {t.status.value}, so it is not ranked"
                                 if t else "no such task"))
        return 1
    t = hit.task
    pos = ranked.index(hit) + 1 if hit in ranked else None
    print(f"\n{t.title}  [{t.course_key}]")
    print(f"  rank {pos} of {len(ranked)}   score {hit.score:.2f}" if pos
          else "  not schedulable (earned by attending)")
    print(f"  weight: {hit.grade_impact*100:.2f}% of grade, from {t.weight_source}")
    for r in hit.reasons:
        print(f"  - {r}")
    for e in [e for e in read_events(2000) if e["uid"] == uid][:8]:
        print(f"  history {e['ts'][:16]} {e['origin']}: {e['what']} "
              f"{e.get('old')} -> {e.get('new')}")
    return 0


def cmd_log(cfg, args):
    with Store.edit("cli") as store:
        store.log_time(args.uid, args.minutes)
        t = store._task(args.uid)
        print(f"logged {args.minutes}m on '{t.title}' "
              f"({t.spent_minutes}/{t.est_minutes}m, {t.remaining_minutes}m left)")
    return 0


def cmd_set(cfg, args):
    with Store.edit("cli") as store:
        if args.undone:
            ok = store.clear_status(args.uid)
            print(f"{args.uid}: " + ("back on the open list" if ok
                                     else "was not manually marked"))
            return 0 if ok else 1
        if args.est is None and not args.status:
            print("nothing to set (use --est, --status or --undone)")
            return 1
        if args.est is not None:
            store.set_estimate(args.uid, args.est)
        if args.status:
            store.set_status(args.uid, args.status)
        t = store._task(args.uid)
        print(f"{t.uid}: '{t.title}' is {t.status.value}, estimate {t.est_minutes}m")
    return 0


def cmd_status(cfg, args):
    from . import health
    store = Store()
    print("Sources (when each last SUCCEEDED):")
    for i in health.report(cfg, store):
        mark = "ok  " if i["ok"] and not i["stale"] else "OLD " if i["ok"] else "FAIL"
        print(f"  {mark} {i['label']:<34} {i['last_ok']:<12} {i['detail'][:70]}")
        if i.get("fix"):
            print(f"       fix: {i['fix']}")
    missing = [t for t in store.open_tasks() if t.missing_since
               and not store.overrides.get(t.uid, {}).get("keep")]
    print(f"\n{len(store.open_tasks())} open tasks, {len(missing)} no longer listed "
          f"by their source")
    return 0


def cmd_events(cfg, args):
    for e in read_events(args.n):
        print(f"{e['ts'][:16]}  {e['origin']:<15} {e['what']:<24} "
              f"{str(e.get('old'))[:12]:>12} -> {str(e.get('new'))[:12]:<12} "
              f"{e.get('course', ''):<8} {e.get('title', '')[:40]}")
    return 0


def cmd_tidy(cfg, args):
    """Retention: logs and regenerable output older than --days."""
    import time
    cutoff = time.time() - args.days * 86400
    victims = [p for d, pats in ((ROOT / "logs", ["daily-*.log", "midday-*.log",
                                                   "weekly-*.log"]),
                                 (OUT_DIR, ["day-*.html", "day-*.ics", "plan-*.md"]))
               for pat in pats for p in d.glob(pat) if p.stat().st_mtime < cutoff]
    size = sum(p.stat().st_size for p in victims)
    if args.dry_run:
        print(f"would remove {len(victims)} files ({size/1024:.0f} KB) older than "
              f"{args.days} days; day-*.json plans are always kept")
        return 0
    for p in victims:
        p.unlink()
    print(f"removed {len(victims)} files ({size/1024:.0f} KB) older than {args.days} days")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="plan", description="Course-aware daily planner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update", help="get the newest version now (also automatic)")
    p = sub.add_parser("catch-up", help="starting mid-term: set aside work that was "
                                        "due before today")
    p.add_argument("--dry-run", action="store_true", help="only list what it would set aside")
    p = sub.add_parser("setup", help="connect your classes (run this first)")
    p.add_argument("--check", action="store_true",
                   help="change nothing; report what is ready and what is missing")
    sub.add_parser("courses", help="list Canvas courses and their ids")
    sub.add_parser("sync", help="merge Canvas + LearningSuite dumps + feeds into the store")
    p = sub.add_parser("ls-refresh", help="fetch LearningSuite gradebooks, then sync")
    p.add_argument("--no-browser", action="store_true",
                   help="never open a browser window to sign in")
    p.add_argument("--via-browser", action="store_true",
                   help="fallback: read pages through the browser instead of HTTP")
    p.add_argument("--no-sync", action="store_true")
    p = sub.add_parser("ls-login", help="sign in to LearningSuite by hand, then refresh")
    p.add_argument("--save-password", action="store_true",
                   help="after sign-in, wait (up to 30s) for you to click Save on the "
                        "browser's save-password offer, unless one is saved already")
    p = sub.add_parser("today", help="build today's ranked plan with the agent")
    p.add_argument("--date", default=None)
    p.add_argument("--model", default=None, help="default: prefs agent.plan_model (opus)")
    p = sub.add_parser("estimate", help="agent effort estimates for tasks that need one")
    p.add_argument("--model", default=None,
                   help="default: prefs agent.estimate_model (sonnet)")
    p.add_argument("--batch", type=int, default=20)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--redo", action="store_true", help="re-estimate everything")
    sub.add_parser("enrich", help="pull assignment text and feedback")
    p = sub.add_parser("app", help="launch the local web app")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--no-open", action="store_true")
    sub.add_parser("tasks", help="open tasks by priority")
    p = sub.add_parser("why", help="explain why a task ranks where it does")
    p.add_argument("uid")
    p = sub.add_parser("log", help="record time actually spent")
    p.add_argument("uid"); p.add_argument("minutes", type=int)
    p = sub.add_parser("set", help="set an estimate or status by hand")
    p.add_argument("uid")
    p.add_argument("--est", type=int)
    p.add_argument("--status", choices=[s.value for s in Status])
    p.add_argument("--undone", action="store_true", help="undo a manual status")
    sub.add_parser("status", help="source health and freshness")
    p = sub.add_parser("events", help="audit trail: what changed, when, and who did it")
    p.add_argument("-n", type=int, default=40)
    p = sub.add_parser("tidy", help="remove old logs and regenerable output")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--dry-run", action="store_true")

    args = ap.parse_args(argv)
    fn = globals()["cmd_" + args.cmd.replace("-", "_")]
    if args.cmd in AUTO_UPDATE and argv is None and not os.environ.get("PLANNER_UPDATED"):
        from . import updater
        if updater.maybe_update():
            updater.restart()
    try:
        return fn(None if args.cmd in ("setup", "app", "update") else Config(), args)
    except SetupNeeded as e:
        print(f"Not set up yet: {e}", file=sys.stderr)
        return 2
    except NoSuchTask as e:
        print(f"no such task: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
