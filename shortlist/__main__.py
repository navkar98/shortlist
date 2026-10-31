"""CLI: python -m shortlist init | check | run [--dry-run] [--limit N] [--if-due] | discover | sites | web"""
import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, notify
from .config import Config, HtmlBoard, Schedule, load_config
from .details import fetch_details
from .discover import discover_sites, site_job_counts, site_sources
from .fetchers import Job, fetch_all
from .html_boards import board_id, scrape_boards
from .listings import fetch_listings
from .notify import LIMITS, format_digest
from .prefilter import build_filter
from .scoring import INSTRUCTIONS, fingerprint, score_jobs

if os.name == "nt":
    import msvcrt
else:
    import fcntl

log = logging.getLogger("shortlist")

FULL_SCAN_EVERY = timedelta(days=7)  # paged boards otherwise stop at the first page with nothing new
SEEN_WINDOW = timedelta(hours=48)    # a match is sent only if its board listed it this recently


def group_roles(jobs: list[Job]) -> list[tuple[Job, list[Job]]]:
    """Group identical postings of one role; the first posting represents the group and lists every location."""
    groups: dict[str, list[Job]] = {}
    for job in jobs:
        groups.setdefault(job.role_hash, []).append(job)
    return [(js[0].model_copy(update={"location": "; ".join(dict.fromkeys(j.location for j in js)),
                                      "remote": any(j.remote for j in js)}), js[1:]) for js in groups.values()]


def done_keys(conn: sqlite3.Connection, reject) -> set[str]:
    """Stored jobs that need no more work: handled, or rejected by the current filters. Paged boards stop at a page
    made only of these, so candidates left unscored by an earlier run (--limit, a failed batch) are fetched again."""
    pending = set()
    for r in db.unscored_jobs(conn):
        job = Job(ats=r["ats"], slug=r["slug"], job_id=r["job_id"], company=r["company"], title=r["title"],
                  location=r["location"], remote="remote" in r["location"].lower(), url=r["url"], description="")
        if reject(job) is None:
            pending.add(job.key)
    return db.known_keys(conn) - pending


def boards_with_jobs(companies: list[str], recipes: list[HtmlBoard], jobs: list[Job]) -> set[str]:
    """Board ids (as in the full-scan schedule) that returned at least one job this run."""
    seen = {(j.ats, j.slug) for j in jobs}
    ids = {c for c in companies if (c.partition(":")[0], "amazon" if c.startswith("amazon:") else c.partition(":")[2]) in seen}
    return ids | {board_id(b) for b in recipes if ("html", re.sub(r"[^a-z0-9]+", "-", b.name.lower()).strip("-")) in seen}


def skip_reason(conn: sqlite3.Connection, schedule: Schedule, now: datetime) -> str | None:
    """Why a scheduled (--if-due) run should not happen now, or None when it is due."""
    window = timedelta(hours=max(48.0, schedule.min_hours_between_runs))
    runs = [datetime.fromisoformat(s) for s in db.completed_runs(conn, (now - window).isoformat(timespec="seconds"))]
    today = [r for r in runs if r.astimezone().date() == now.astimezone().date()]
    if len(today) >= schedule.max_runs_per_day:
        return f"already ran {len(today)} time(s) today (schedule.max_runs_per_day = {schedule.max_runs_per_day})"
    if runs and now - runs[0] < timedelta(hours=schedule.min_hours_between_runs):
        return (f"last run was {(now - runs[0]).total_seconds() / 3600:.1f}h ago "
                f"(schedule.min_hours_between_runs = {schedule.min_hours_between_runs:g})")
    return None


@contextmanager
def single_run(lock_path: str):
    """An exclusive lock for a run or a discovery, so a scheduled run and one started from the web app never overlap.
    Yields False when another process holds it. The operating system drops the lock if the process dies."""
    handle = open(lock_path, "a+")
    locked = False
    try:
        handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            pass
        yield locked
    finally:
        if locked and os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def run(cfg: Config, *, dry_run: bool = False, limit: int | None = None, if_due: bool = False) -> int:
    missing = notify.missing_env(cfg.notify)
    if not dry_run and missing:
        log.error("%s must be set (in the environment or .env) to notify by %s; use --dry-run to print the digest "
                  "instead", ", ".join(missing), ", ".join(cfg.notify))
        return 2
    caps = [n for n in (limit, cfg.scoring.max_jobs_per_run) if n]
    with single_run(f"{cfg.db_path}.lock") as locked:
        if not locked:
            log.info("another run or discovery is in progress; skipping this one")
            return 0
        return _run(cfg, dry_run=dry_run, limit=min(caps) if caps else None, if_due=if_due)


def _run(cfg: Config, *, dry_run: bool, limit: int | None, if_due: bool) -> int:
    resume = Path(cfg.resume_path).read_text(encoding="utf-8")
    started = datetime.now(timezone.utc)
    now = started.isoformat(timespec="seconds")
    conn = db.connect(cfg.db_path)
    if if_due and (reason := skip_reason(conn, cfg.schedule, started)):
        log.info("not due: %s; skipping this run", reason)
        return 0
    site_rows = discover_sites(cfg, conn, started)  # new or broken career pages only; the rest come from the database
    site_companies, site_recipes = site_sources(site_rows)
    companies, recipes = list(dict.fromkeys(cfg.companies + site_companies)), cfg.html_boards + site_recipes
    full_scan = db.full_scans_due(conn, companies + [board_id(b) for b in recipes],
                                  (started - FULL_SCAN_EVERY).isoformat(timespec="seconds"))
    done = done_keys(conn, build_filter(cfg.filters))
    board_jobs = (fetch_all(companies, known=done, full_scan=full_scan)
                  + scrape_boards(recipes, known=done, full_scan=full_scan))
    db.record_full_scans(conn, full_scan & boards_with_jobs(companies, recipes, board_jobs), now)
    db.record_site_results(conn, site_job_counts(site_rows, board_jobs), now)
    listed = fetch_listings(cfg.github_listings,
                            skip_greenhouse_ids={j.job_id for j in board_jobs if j.ats == "greenhouse"})
    merged: dict[str, Job] = {}
    for job in board_jobs:  # boards first: their copy has the full description
        merged.setdefault(job.key, job)
    from_boards = len(merged)  # fewer than len(board_jobs) when several searches (e.g. Amazon's) find the same job
    for job in listed:
        merged.setdefault(job.key, job)
    jobs = list(merged.values())
    log.info("fetched %d jobs: %d from %d company boards, %d from listings (%d of those already on a board)",
             len(jobs), from_boards, len({(j.ats, j.slug) for j in board_jobs}), len(listed),
             len(listed) - (len(jobs) - from_boards))
    stats: dict = {}
    code = process(cfg, conn, jobs, resume, now, dry_run=dry_run, limit=limit, stats=stats)
    # Dry runs, and runs that fetched nothing or failed, don't count toward the schedule limits (see completed_runs).
    db.record_run(conn, now, len(jobs), code, dry_run=dry_run, stats=stats,
                  finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return code


def process(cfg: Config, conn: sqlite3.Connection, jobs: list[Job], resume: str, now: str, *, dry_run: bool,
            limit: int | None, stats: dict | None = None) -> int:
    """Store, filter, score, and notify for one run's fetched jobs. Returns the exit code; fills `stats`."""
    stats = {} if stats is None else stats
    first_seen = db.upsert_jobs(conn, jobs, now)
    stats.update(first_seen=first_seen, scored=0, matches=0, sent=0)
    unscored = db.unscored_keys(conn)
    reject = build_filter(cfg.filters)
    new = [(job, reject(job)) for job in jobs if job.key in unscored]
    reasons = Counter(reason for _, reason in new)
    log.info("%d jobs (%d first seen); %d not yet scored: %d pass the pre-filter, "
             "rejected %d by title, %d by excluded title, %d by location", len(jobs), first_seen, len(new),
             reasons[None], reasons["title"], reasons["excluded title"], reasons["location"])

    # Full descriptions for listing-only jobs are fetched here, for new jobs that passed the filter only.
    candidates = fetch_details([job for job, reason in new if reason is None][:limit])
    handled = db.handled_roles(conn)
    roles = []
    for rep, copies in group_roles(candidates):
        if rep.role_hash in handled:  # a new posting of a role scored in an earlier run
            db.mark_duplicates(conn, [rep, *copies], handled[rep.role_hash])
        else:
            roles.append((rep, copies))
    log.info("scoring %d roles (%d identical postings folded in)", len(roles), sum(len(c) for _, c in roles))
    feedback = db.feedback_examples(conn, cfg.min_score, cfg.scoring.feedback_examples) \
        if cfg.scoring.feedback_examples and roles else []
    if feedback:
        log.info("showing the scorer %d jobs you rated (%d of them disagree with their score)", len(feedback),
                 sum(e["disagrees"] for e in feedback))
    scores, failed = score_jobs([rep for rep, _ in roles], resume, cfg.preferences, cfg.scoring, feedback=feedback,
                                recheck_from=cfg.min_score - 15 if cfg.scoring.recheck_alone else None)
    scored_with = fingerprint(resume, cfg.preferences, cfg.scoring.instructions or INSTRUCTIONS)
    for rep, copies in roles:
        if rep.key in scores or rep.key in failed:
            result = scores.get(rep.key)
            db.save_result(conn, rep, result and result.model_dump_json(), result and result.score, now, scored_with)
            db.mark_duplicates(conn, copies, rep.key)
    log.info("scored %d, failed %d, left for next run %d", len(scores), len(failed), len(roles) - len(scores) - len(failed))

    seen_since = (datetime.fromisoformat(now) - SEEN_WINDOW).isoformat(timespec="seconds")
    matches = db.pending_matches(conn, cfg.min_score, seen_since)
    stats.update(scored=len(scores), matches=len(matches))
    if not matches:
        log.info("no new matches at or above %d; nothing to send", cfg.min_score)
        return 0
    if dry_run:
        print("\n" + "\n\n".join(text for text, _ in format_digest(matches)))
        return 0
    if not cfg.notify:
        log.info("%d new matches; no notification channel is set (`notify` in config.yaml), so they are only in the "
                 "web app", len(matches))
        return 0
    # A match counts as sent once any channel delivered it; a channel that fails stops there, and its rest stays
    # pending for the next run.
    code, sent = 0, set()
    for channel in cfg.notify:
        messages = format_digest(matches, LIMITS[channel])
        for i, (text, rows) in enumerate(messages):
            if i:
                time.sleep(1)  # brief pause between the parts of a long digest
            try:
                notify.send(channel, text)
            except RuntimeError as e:
                log.error("%s: %s; %d matches not sent this way (the next run sends those no channel delivered)",
                          channel, e, sum(len(r) for _, r in messages[i:]))
                code = 1
                break
            db.mark_notified(conn, rows, now)
            sent |= {(r["ats"], r["slug"], r["job_id"]) for r in rows}
        else:
            log.info("sent the %s digest: %d matches in %d message(s)", channel, len(matches), len(messages))
    stats["sent"] = len(sent)
    return code


def detail_url(row: sqlite3.Row) -> str:
    """Where a stored job's description can be read: the board's own API for Greenhouse, else the job's page."""
    if row["ats"] == "greenhouse":
        return f"https://job-boards.greenhouse.io/{row['slug']}/jobs/{row['job_id']}"
    return row["url"]


def rescore(cfg: Config, *, from_score: int | None, everything: bool, limit: int | None) -> int:
    """Score open jobs again, each on its own, with the current resume, preferences, prompt, and ratings: by default
    those at min_score - 15 or above that were scored with an earlier version of any of these."""
    with single_run(f"{cfg.db_path}.lock") as locked:
        if not locked:
            log.info("a run or discovery is in progress; try again when it finishes")
            return 0
        conn = db.connect(cfg.db_path)
        resume = Path(cfg.resume_path).read_text(encoding="utf-8")
        current = fingerprint(resume, cfg.preferences, cfg.scoring.instructions or INSTRUCTIONS)
        from_score = cfg.min_score - 15 if from_score is None else from_score
        rows = db.rescore_candidates(conn, from_score, None if everything else current)[:limit]
        log.info("re-scoring %d open jobs scoring %d+%s, each on its own", len(rows), from_score,
                 "" if everything else " that were scored with an earlier resume, preferences, or prompt")
        before = {f"{r['ats']}:{r['slug']}:{r['job_id']}": r["score"] for r in rows}
        # Jobs scored before descriptions were kept have none stored: their description is read again.
        jobs = fetch_details([Job(ats=r["ats"], slug=r["slug"], job_id=r["job_id"], company=r["company"],
                                  title=r["title"], location=r["location"], remote="remote" in r["location"].lower(),
                                  url=r["url"], description=r["description"] or "",
                                  detail_url="" if r["description"] else detail_url(r)) for r in rows])
        if unread := sum(1 for job in jobs if not job.description):
            log.info("%d jobs are scored from their listing alone: their description could not be read", unread)
        feedback = db.feedback_examples(conn, cfg.min_score, cfg.scoring.feedback_examples) \
            if cfg.scoring.feedback_examples else []
        by_key = {job.key: job for job in jobs}

        def save(got):  # as each job is scored, so stopping a long re-score keeps what it did
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for key, result in got.items():
                db.save_result(conn, by_key[key], result.model_dump_json(), result.score, now, current)

        scores, _ = score_jobs(jobs, resume, cfg.preferences, cfg.scoring.model_copy(update={"batch_size": 1}),
                               feedback=feedback, on_scores=save)
        line = cfg.min_score
        up = sum(1 for k, s in scores.items() if s.score >= line > before[k])
        down = sum(1 for k, s in scores.items() if before[k] >= line > s.score)
        moved = [abs(s.score - before[k]) for k, s in scores.items()]
        log.info("re-scored %d of %d: %d rose to %d+, %d fell below it, scores moved %.1f points on average",
                 len(scores), len(rows), up, line, down, sum(moved) / max(1, len(moved)))
        return 0 if len(scores) == len(jobs) else 1


def discover_now(cfg: Config, *, site: str | None, max_calls: int | None) -> int:
    """Discover due career pages (or one named site, from scratch) without fetching jobs, and print the results."""
    with single_run(f"{cfg.db_path}.lock") as locked:
        if not locked:
            log.info("another run or discovery is in progress; try again when it finishes")
            return 0
        return _discover_now(cfg, site=site, max_calls=max_calls)


def _discover_now(cfg: Config, *, site: str | None, max_calls: int | None) -> int:
    conn = db.connect(cfg.db_path)
    if site:
        pages = [p for p in cfg.career_pages if site.lower() in (p.name.lower(), p.url.lower())]
        if not pages:
            log.error("no career page named %r in the config", site)
            return 2
        db.forget_site(conn, pages[0].url)
        cfg = cfg.model_copy(update={"career_pages": pages})
    if max_calls is not None:
        cfg = cfg.model_copy(update={"discovery": cfg.discovery.model_copy(
            update={"max_agent_calls_per_site": max_calls, "max_agent_calls_per_run": max(max_calls, 10_000)})})
    discover_sites(cfg, conn, datetime.now(timezone.utc))
    return show_sites(conn)


def show_sites(conn: sqlite3.Connection) -> int:
    rows = list(db.sites(conn).values())
    for r in rows:
        found = r["board"] or (r["recipe"] and "recipe " + json.loads(r["recipe"])["url"]) or ""
        print(f"{r['name'][:28]:28} {r['status']:11} {r['agent_calls']:2} calls  {found[:60]:60}  {(r['note'] or '')[:90]}")
    by_status = Counter(r["status"] for r in rows)
    print(f"\n{len(rows)} sites: {dict(by_status)}; {sum(r['agent_calls'] for r in rows)} agent calls in total")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shortlist", description="Score new job postings against your resume.")
    commands = parser.add_subparsers(dest="command", required=True)
    init_cmd = commands.add_parser("init", help="first-time setup: config.yaml, resume.md, and .env, by questions")
    init_cmd.add_argument("--yes", action="store_true", help="accept every default without asking")
    check_cmd = commands.add_parser("check", help="check the config, resume, scoring backend, and notifications")
    check_cmd.add_argument("--no-call", action="store_true", help="skip the one small test call to the scoring model")
    check_cmd.add_argument("--send-test", action="store_true", help="also send a test message on each channel")
    run_cmd = commands.add_parser("run", help="discover, fetch, filter, score, and notify")
    run_cmd.add_argument("--dry-run", action="store_true", help="print the digest instead of sending it")
    run_cmd.add_argument("--limit", type=int, help="score at most N new jobs this run")
    run_cmd.add_argument("--if-due", action="store_true",
                         help="skip unless the config's schedule limits allow a run now (used by the scheduled task)")
    discover_cmd = commands.add_parser("discover", help="turn due career pages into boards now, without fetching jobs")
    discover_cmd.add_argument("--site", help="rediscover this one career page (name or URL) from scratch")
    discover_cmd.add_argument("--max-calls", type=int, help="agent calls allowed per site (overrides the config)")
    rescore_cmd = commands.add_parser("rescore", help="score open jobs again, each on its own, after a resume or "
                                                      "prompt change")
    rescore_cmd.add_argument("--from", dest="from_score", type=int,
                             help="jobs scoring at least this (default: min_score - 15)")
    rescore_cmd.add_argument("--all", action="store_true",
                             help="also jobs already scored with the current resume, preferences, and prompt")
    rescore_cmd.add_argument("--limit", type=int, help="re-score at most N jobs")
    sites_cmd = commands.add_parser("sites", help="list what each career page was discovered as")
    sites_cmd.add_argument("--forget", metavar="NAME_OR_URL", help="forget a site so the next run rediscovers it")
    web_cmd = commands.add_parser("web", help="open the web app: matches, tracker, sources, runs, settings")
    web_cmd.add_argument("--port", type=int, default=8765)
    web_cmd.add_argument("--host", default="127.0.0.1",
                         help="interface to listen on (default 127.0.0.1: only this computer can reach the app)")
    web_cmd.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    for cmd in (init_cmd, check_cmd, run_cmd, discover_cmd, rescore_cmd, sites_cmd, web_cmd):
        cmd.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
        cmd.add_argument("--log-file", help="append log lines to this file instead of the console")
    args = parser.parse_args(argv)
    if getattr(args, "limit", None) is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    for stream in (sys.stdout, sys.stderr):  # job titles are not always representable in the Windows console codepage
        if stream is not None:  # both are None under pythonw.exe, which the scheduled task uses
            stream.reconfigure(encoding="utf-8", errors="replace")
    log_target = {"filename": args.log_file, "encoding": "utf-8"} if args.log_file else {}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", **log_target)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs every request at INFO
    try:
        if args.command in ("init", "check"):
            from . import wizard  # imported here: the other commands don't need it

            if args.command == "init":
                return wizard.init(Path(args.config), yes=args.yes)
            return wizard.check(Path(args.config), call=not args.no_call, send_test=args.send_test)
        cfg = load_config(args.config)
        if args.command == "discover":
            return discover_now(cfg, site=args.site, max_calls=args.max_calls)
        if args.command == "rescore":
            return rescore(cfg, from_score=args.from_score, everything=args.all, limit=args.limit)
        if args.command == "sites":
            conn = db.connect(cfg.db_path)
            if args.forget:
                print(f"forgot {db.forget_site(conn, args.forget)} site(s); the next run rediscovers them")
            return show_sites(conn)
        if args.command == "web":
            from .web import serve  # imported here: only the web command needs FastAPI

            return serve(args.config, host=args.host, port=args.port, open_browser=not args.no_browser)
        return run(cfg, dry_run=args.dry_run, limit=args.limit, if_due=args.if_due)
    except Exception:  # without a console (pythonw), an uncaught error would otherwise vanish
        log.exception("%s failed", args.command)
        return 1


if __name__ == "__main__":
    sys.exit(main())
