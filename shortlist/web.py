"""Local web app: matches, application tracker, sources, runs, and settings (FastAPI + Jinja2 templates + htmx).

It is a single-user app for this computer, without logins. It listens on 127.0.0.1 by default, answers only requests
addressed to localhost (against DNS rebinding), and every request that changes something must carry the token that
is written into each page (so another website open in the browser cannot act on the app).
"""
import csv
import io
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .config import Config, Filters, add_career_page, available_packs, load_config, replace_sections, write_config
from .fetchers import Job
from .notify import LIMITS, REQUIRED_ENV
from .prefilter import build_filter, first_match
from .scoring import BACKENDS, INSTRUCTIONS, KEY_ENV, build_prompt, fingerprint, needs_key

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
VISIBLE_STAGES = [s for s in db.STAGES if s != "new"]  # the tracker board's columns
STAGE_LABELS = {"new": "New", "saved": "Saved", "applied": "Applied", "interviewing": "Interviewing", "offer": "Offer",
                "rejected": "Rejected", "not_interested": "Not interested"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Activity:
    """At most one background task (a run, a dry run, or a discovery) as a `python -m shortlist ...` process. Runs
    also take the run lock, so they never overlap a scheduled run either."""

    def __init__(self, config_path: Path):
        self.config_path, self.proc, self.label, self.log_path = config_path, None, "", None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, label: str, args: list[str]) -> bool:
        if self.running:
            return False
        logs = self.config_path.parent / "logs"
        logs.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.label, self.log_path = label, logs / f"{stamp}-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}.log"
        with open(self.log_path, "ab") as output:  # printed output (digest, site table) lands next to the log lines
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "shortlist", *args, "--config", str(self.config_path), "--log-file",
                 str(self.log_path)], cwd=HERE.parent, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True

    def tail(self, lines: int = 150) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _terms(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\n]", text) if t.strip()]


# --- template helpers ----------------------------------------------------------------------------------------------

def ago(iso: str | None) -> str:
    """'just now', '5m ago', '3h ago', '2d ago', or a date for anything older than two weeks."""
    if not iso:
        return ""
    then = datetime.fromisoformat(iso)
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    for limit, unit, size in ((3600, "m", 60), (86400, "h", 3600), (86400 * 14, "d", 86400)):
        if seconds < limit:
            return "just now" if seconds < 90 else f"{seconds // size:.0f}{unit} ago"
    return f"{then.astimezone():%b} {then.astimezone().day}"


def hue(name: str) -> int:
    """A stable color (0-359) per company, for its monogram."""
    return zlib.crc32((name or "").lower().encode()) % 360


def initials(name: str) -> str:
    words = re.findall(r"[^\W_]+", name or "")
    return "".join(w[0] for w in words[:2]).upper() or "?"


def band(score: int | None) -> str:
    """The score's color band; the same thresholds as the scoring prompt (85+ apply now, 70-84 strong fit)."""
    return "none" if score is None else "top" if score >= 85 else "good" if score >= 70 else "fair" if score >= 50 else "low"


# --- queries -------------------------------------------------------------------------------------------------------

PAGE_SIZE = 50
JOINED = f" FROM jobs j JOIN {db.BOARD_SEEN} b USING (ats, slug) LEFT JOIN tracking t USING (ats, slug, job_id) WHERE "


def search_jobs(conn, *, q: str = "", min_score: int = 0, stage: str = "active", company: str = "", source: str = "",
                remote: bool = False, days: int = 0, sort: str = "score", closed: bool = False, limit: int = PAGE_SIZE,
                offset: int = 0) -> tuple[list[dict], int]:
    """One page of scored jobs matching the filters, and how many match in all. Closed postings only on request."""
    where, args = ["j.status = 'scored'"] + ([] if closed else [f"NOT {db.CLOSED}"]), []
    if q:
        where.append("(j.title LIKE ? OR j.company LIKE ? OR j.location LIKE ? OR j.result LIKE ?)")
        args += [f"%{q}%"] * 4
    if min_score:
        where.append("j.score >= ?")
        args.append(min_score)
    if stage == "active":
        where.append("coalesce(t.stage, 'new') NOT IN ('rejected', 'not_interested')")
    elif stage in db.STAGES:
        where.append("coalesce(t.stage, 'new') = ?")
        args.append(stage)
    if company:
        where.append("j.company LIKE ?")
        args.append(f"%{company}%")
    if source:
        where.append("j.ats = ?")
        args.append(source)
    if remote:
        where.append("lower(j.location) LIKE '%remote%'")
    if days:
        where.append("j.first_seen >= ?")
        args.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds"))
    sql = JOINED + " AND ".join(where)
    order = "j.first_seen DESC, j.score DESC" if sort == "newest" else "j.score DESC, j.first_seen DESC"
    total = conn.execute("SELECT count(*)" + sql, args).fetchone()[0]
    rows = conn.execute("SELECT j.rowid AS id, j.company, j.title, j.location, j.url, j.ats, j.score, j.result,"
                        " j.first_seen, j.last_seen, coalesce(t.stage, 'new') AS stage, coalesce(t.feedback, 0) AS feedback,"
                        f" {db.CLOSED} AS closed{sql} ORDER BY {order} LIMIT ? OFFSET ?", [*args, limit, offset])
    return [_with_result(dict(r)) for r in rows], total


def _with_result(row: dict) -> dict:
    row["result"] = json.loads(row["result"]) if row.get("result") else {}
    return row


def board_health(conn) -> list[dict]:
    last_run = conn.execute("SELECT max(started_at) FROM runs").fetchone()[0] or ""
    return [dict(r) for r in conn.execute(
        "SELECT ats, slug, max(company) AS company, count(*) AS stored, sum(last_seen >= ?) AS seen_last_run,"
        " max(last_seen) AS last_seen, sum(status = 'scored') AS scored FROM jobs GROUP BY ats, slug"
        " ORDER BY seen_last_run = 0 DESC, company", (last_run,))]


def last_run_start(conn) -> str:
    return conn.execute("SELECT max(started_at) FROM runs").fetchone()[0] or ""


def nav_counts(conn, min_score: int) -> dict:
    """Badges in the sidebar: strong matches nobody has acted on yet, and applications in play."""
    waiting = conn.execute("SELECT count(*)" + JOINED + "j.status = 'scored' AND j.score >= ?"
                           f" AND coalesce(t.stage, 'new') = 'new' AND NOT {db.CLOSED}", (min_score,)).fetchone()[0]
    active = conn.execute("SELECT count(*) FROM tracking WHERE stage IN ('saved', 'applied', 'interviewing', 'offer')"
                          ).fetchone()[0]
    return {"matches": waiting, "tracker": active}


def overview(conn, min_score: int) -> dict:
    now = datetime.now(timezone.utc)
    week = (now - timedelta(days=7)).isoformat(timespec="seconds")
    kpi = dict(conn.execute(
        f"SELECT count(*) AS scored, sum(j.score >= :m AND coalesce(t.stage, 'new') = 'new' AND NOT {db.CLOSED}) AS waiting,"
        " sum(j.score >= :m AND j.first_seen >= :week) AS strong_week, sum(j.first_seen >= :week) AS scored_week,"
        " sum(coalesce(t.stage, 'new') IN ('applied', 'interviewing', 'offer')) AS applied,"
        " sum(coalesce(t.stage, 'new') IN ('interviewing', 'offer')) AS interviewing"
        + JOINED + "j.status = 'scored'", {"m": min_score, "week": week}).fetchone())
    top, _ = search_jobs(conn, min_score=min_score, stage="new", days=7, limit=6)
    if not top:  # a quiet week: the best ones still waiting
        top, _ = search_jobs(conn, min_score=min_score, stage="new", limit=6)
    buckets = [0] * 10
    for bucket, n in conn.execute("SELECT min(score / 10, 9), count(*) FROM jobs WHERE status = 'scored' GROUP BY 1"):
        buckets[bucket] = n
    first_day = (now - timedelta(days=13)).date()
    per_day = dict(conn.execute("SELECT substr(first_seen, 1, 10), count(*) FROM jobs WHERE status = 'scored'"
                                " AND score >= ? AND first_seen >= ? GROUP BY 1", (min_score, first_day.isoformat())))
    days = [(first_day + timedelta(days=i)) for i in range(14)]
    funnel = dict(conn.execute("SELECT stage, count(*) FROM tracking GROUP BY stage"))
    runs = [dict(r) for r in db.recent_runs(conn, limit=1)]
    return {"kpi": kpi, "top": top, "buckets": buckets, "daily": [(d, per_day.get(d.isoformat(), 0)) for d in days],
            "funnel": funnel, "last_run": runs[0] if runs else None}


# Words that say nothing about a role on their own; never suggested as title words.
STOPWORDS = {"a", "an", "and", "at", "for", "in", "of", "on", "or", "the", "to", "with", "i", "ii", "iii", "iv", "v",
             "sr", "jr", "new", "team", "remote", "hybrid", "onsite", "contract", "full", "time", "part", "us", "usa"}


def filtered_jobs(conn, filters: Filters, scope: str) -> list[dict]:
    """Unscored jobs from the latest run that the filters drop, with the reason and, for an excluded title, the word.
    scope "run": jobs first seen in that run; "open": every job that run listed."""
    column = "last_seen" if scope == "open" else "first_seen"
    reject, dropped = build_filter(filters), []
    for r in conn.execute(f"SELECT rowid AS id, ats, slug, job_id, company, title, location, url, first_seen FROM jobs"
                          f" WHERE status IS NULL AND {column} = ?", (last_run_start(conn),)):
        job = Job(ats=r["ats"], slug=r["slug"], job_id=r["job_id"], company=r["company"], title=r["title"],
                  location=r["location"], remote="remote" in r["location"].lower(), url=r["url"], description="")
        if reason := reject(job):
            word = first_match(filters.title_exclude, r["title"]).lower() if reason == "excluded title" else ""
            dropped.append({**dict(r), "job": job, "reason": reason, "word": word})
    return dropped


def _grams(text: str, sizes=(1, 2)) -> set[str]:
    words = re.findall(r"[a-z0-9+#]+(?:[./-][a-z0-9+#]+)*", text.lower())
    return {" ".join(words[i:i + n]) for n in sizes for i in range(len(words) - n + 1)
            if words[i] not in STOPWORDS and words[i + n - 1] not in STOPWORDS and not words[i].isdigit()}


def title_suggestions(dropped: list[dict], filters: Filters, limit: int = 14) -> list[dict]:
    """Words and two-word phrases common in titles the include list missed, with how many jobs each would let through
    (still subject to the excluded words and places)."""
    titles = [d for d in dropped if d["reason"] == "title"]
    counts = Counter(g for d in titles for g in _grams(d["title"]))
    known = {t.lower() for t in filters.title_include + filters.title_exclude}
    out = []
    for phrase, n in counts.most_common(200):
        if phrase in known or any(first_match([k], phrase) for k in filters.title_exclude):
            continue
        reject = build_filter(filters.model_copy(update={"title_include": filters.title_include + [phrase]}))
        passing = sum(1 for d in titles if first_match([phrase], d["title"]) and reject(d["job"]) is None)
        if passing:
            out.append({"phrase": phrase, "titles": n, "passing": passing})
        if len(out) == limit:
            break
    return out


def place_suggestions(dropped: list[dict], limit: int = 10) -> list[dict]:
    """Places named in the locations of jobs dropped for their location (not two-letter codes, which are ambiguous)."""
    counts = Counter(p for d in dropped if d["reason"] == "location"
                     for p in {s.strip().lower() for s in re.split(r"[;,|/()]", d["location"]) if len(s.strip()) > 2})
    return [{"place": p, "jobs": n} for p, n in counts.most_common(limit) if not p.isdigit()]


def greeting() -> str:
    hour = datetime.now().hour
    return "Good morning" if 5 <= hour < 12 else "Good afternoon" if 12 <= hour < 18 else "Good evening"


# --- app -----------------------------------------------------------------------------------------------------------

def create_app(config_path: str | Path, *, allowed_hosts: set[str] | None = LOCAL_HOSTS, token: str | None = None) -> FastAPI:
    """`allowed_hosts=None` accepts any Host header (only when the user chose to listen beyond this computer)."""
    config_path = Path(config_path).resolve()
    app = FastAPI(title="Shortlist", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(fromiso=lambda s: (s or "")[:16].replace("T", " "), ago=ago, hue=hue,
                                 initials=initials, band=band)
    token = token or secrets.token_urlsafe(24)
    activity = Activity(config_path)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if allowed_hosts is not None and (request.url.hostname or "") not in allowed_hosts:
            return PlainTextResponse("This app only answers requests addressed to localhost.", status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-shortlist-token") != token:
            return PlainTextResponse("Missing or wrong token; reload the page.", status_code=403)
        return await call_next(request)

    def cfg() -> Config:
        return load_config(config_path)

    def conn():
        return db.connect(cfg().db_path)

    def page(request: Request, name: str, **context) -> HTMLResponse:
        if "nav" in context:  # full pages carry the sidebar's badges
            context["counts"] = nav_counts(conn(), cfg().min_score)
        return templates.TemplateResponse(request, name, {"token": token, "activity": activity, "stages": db.STAGES,
                                                          "stage_labels": STAGE_LABELS, **context})

    @app.get("/")
    def home(request: Request):
        config = cfg()
        c = conn()
        return page(request, "overview.html", nav="overview", min_score=config.min_score, greeting=greeting(),
                    schedule=config.schedule, **overview(c, config.min_score))

    @app.get("/matches")
    def matches(request: Request):
        c = conn()
        sources = [r[0] for r in c.execute("SELECT DISTINCT ats FROM jobs WHERE status = 'scored' ORDER BY ats")]
        stats = dict(c.execute(
            f"SELECT count(*) AS scored, sum(j.score >= ? AND NOT {db.CLOSED}) AS strong, sum(j.first_seen >= ?) AS since_run,"
            f" sum({db.CLOSED}) AS closed" + JOINED + "j.status = 'scored'", (cfg().min_score, last_run_start(c))).fetchone())
        dropped = c.execute("SELECT count(*) FROM jobs WHERE status IS NULL AND first_seen = ?",
                            (last_run_start(c),)).fetchone()[0]
        return page(request, "matches.html", nav="matches", sources=sources, stats=stats, min_score=cfg().min_score,
                    dropped=dropped)

    @app.get("/matches/rows")
    def matches_rows(request: Request, q: str = "", min_score: int = 0, stage: str = "active", company: str = "",
                     source: str = "", remote: str = "", days: int = 0, sort: str = "score", closed: str = "",
                     offset: int = 0):
        c = conn()
        rows, total = search_jobs(c, q=q, min_score=min_score, stage=stage, company=company, source=source,
                                  remote=bool(remote), days=days, sort=sort, closed=bool(closed), offset=max(0, offset))
        return page(request, "_match_rows.html", rows=rows, total=total, offset=offset, page_size=PAGE_SIZE,
                    since=last_run_start(c))

    @app.get("/jobs/{job_id}")
    def job_detail(request: Request, job_id: int):
        c = conn()
        row = db.job_row(c, job_id)
        if row is None:
            raise HTTPException(404)
        closed = c.execute(f"SELECT {db.CLOSED} FROM jobs j JOIN {db.BOARD_SEEN} b USING (ats, slug) WHERE j.rowid = ?",
                           (job_id,)).fetchone()[0]
        return page(request, "_job.html", job={**_with_result(dict(row)), "closed": closed})

    @app.post("/jobs/{job_id}/track")
    def track(job_id: int, stage: str | None = Form(None), notes: str | None = Form(None),
              feedback: int | None = Form(None)):
        try:
            db.set_tracking(conn(), job_id, _now(), stage=stage, notes=notes, feedback=feedback)
        except KeyError:
            raise HTTPException(404) from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if stage is None and feedback is None:  # a note: saved quietly, without redrawing the list being typed next to
            events = {"notesSaved": None}
        else:
            message = f"Moved to {STAGE_LABELS[stage]}" if stage else \
                {1: "Marked as a good fit", -1: "Marked as a poor fit"}.get(feedback, "Fit rating cleared")
            events = {"jobChanged": None, "toast": message}
        return Response(status_code=204, headers={"HX-Trigger": json.dumps(events)})

    @app.get("/tracker")
    def tracker(request: Request):
        rows = [_with_result(dict(r)) for r in conn().execute(
            "SELECT j.rowid AS id, j.company, j.title, j.location, j.url, j.score, j.result, j.last_seen, t.stage,"
            f" t.applied_at, t.notes, t.updated_at, {db.CLOSED} AS closed FROM tracking t JOIN jobs j USING (ats, slug, job_id)"
            f" JOIN {db.BOARD_SEEN} b USING (ats, slug) WHERE t.stage != 'new' ORDER BY t.updated_at DESC")]
        columns = {s: [r for r in rows if r["stage"] == s] for s in VISIBLE_STAGES}
        return page(request, "tracker.html", nav="tracker", columns=columns)

    @app.get("/tracker.csv")
    def tracker_csv():
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["company", "title", "location", "score", "stage", "applied_at", "notes", "url"])
        for r in conn().execute("SELECT j.company, j.title, j.location, j.score, t.stage, t.applied_at, t.notes, j.url"
                                " FROM tracking t JOIN jobs j USING (ats, slug, job_id) WHERE t.stage != 'new'"
                                " ORDER BY t.stage, j.company"):
            writer.writerow(list(r))
        return Response(out.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="shortlist-applications.csv"'})

    @app.get("/sources")
    def sources(request: Request):
        c, config = conn(), cfg()
        stored = db.sites(c)
        sites = [{"name": p.name, "url": p.url, "row": stored.get(p.url)} for p in config.career_pages]
        boards = board_health(c)
        site_status = {s: sum(1 for x in sites if (x["row"]["status"] if x["row"] else "pending") == s)
                       for s in ("board", "recipe", "unsupported", "pending")}
        return page(request, "sources.html", nav="sources", sites=sites, boards=boards, site_status=site_status,
                    empty_boards=sum(1 for b in boards if not b["seen_last_run"]),
                    listings=config.github_listings, html_boards=config.html_boards)

    @app.post("/sources/career-pages")
    def add_page(name: str = Form(...), url: str = Form(...)):
        name, url = name.strip(), url.strip()
        if not name or not re.match(r"https?://", url):
            raise HTTPException(400, "Give a company name and a careers page URL starting with http(s)://")
        if any(p.url == url for p in cfg().career_pages):
            raise HTTPException(400, "That careers page is already in the config")
        write_config(config_path, add_career_page(config_path.read_text(encoding="utf-8"), name, url))
        activity.start(f"discover {name}", ["discover", "--site", name])
        return Response(status_code=204, headers={"HX-Redirect": "/runs"})

    @app.post("/sources/rediscover")
    def rediscover(name: str = Form(...)):
        if not activity.start(f"discover {name}", ["discover", "--site", name]):
            raise HTTPException(409, "Something is already running; wait for it to finish")
        return Response(status_code=204, headers={"HX-Redirect": "/runs"})

    @app.post("/sources/forget")
    def forget(name: str = Form(...)):
        db.forget_site(conn(), name)
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @app.get("/runs")
    def runs(request: Request):
        history = [dict(r) for r in db.recent_runs(conn())]
        for r in history:
            took = r.get("finished_at") and datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])
            r["minutes"] = round(took.total_seconds() / 60, 1) if took else None
        return page(request, "runs.html", nav="runs", runs=history, chart=history[:20][::-1])

    @app.post("/runs")
    def start_run(kind: str = Form(...)):
        args = {"dry": ["run", "--dry-run"], "real": ["run"], "discover": ["discover"]}.get(kind)
        if args is None:
            raise HTTPException(400, "unknown kind")
        if not activity.start({"dry": "dry run", "real": "run", "discover": "discovery"}[kind], args):
            raise HTTPException(409, "Something is already running; wait for it to finish")
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @app.get("/activity")
    def activity_panel(request: Request):
        return page(request, "_activity.html")

    def resume_text(config: Config) -> str:
        path = Path(config.resume_path)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def rescore_count(config: Config, from_score: int, scope: str) -> int:
        current = fingerprint(resume_text(config), config.preferences, config.scoring.instructions or INSTRUCTIONS)
        return len(db.rescore_candidates(conn(), from_score, current if scope == "outdated" else None))

    @app.get("/settings")
    def settings(request: Request, saved: str = ""):
        config, c = cfg(), conn()
        resume = resume_text(config)
        secrets_needed = [name for channel in config.notify for name in REQUIRED_ENV[channel]]
        if needs_key(config.scoring):
            secrets_needed.insert(0, KEY_ENV[config.scoring.backend])
        rated = db.feedback_examples(c, config.min_score, limit=10_000)
        instructions = config.scoring.instructions or INSTRUCTIONS
        # The whole prompt for one job, with the job itself left as a placeholder.
        example = Job(ats="board", slug="company", job_id="123", company="Company", title="Job title", location="City",
                      remote=False, url="", description="The job's description, up to 8,000 characters.")
        feedback = db.feedback_examples(c, config.min_score, config.scoring.feedback_examples) \
            if config.scoring.feedback_examples else []
        from_score = max(0, config.min_score - 15)
        return page(request, "settings.html", nav="settings", config=config, saved=saved, resume=resume,
                    packs=available_packs(), channels=list(LIMITS), backends=list(BACKENDS),
                    secrets={name: bool(os.environ.get(name)) for name in secrets_needed},
                    rated=len(rated), agree=sum(not e["disagrees"] for e in rated),
                    instructions=instructions, custom_prompt=bool(config.scoring.instructions),
                    full_prompt=build_prompt([example], resume, config.preferences, feedback, instructions),
                    from_score=from_score, scope="outdated", count=rescore_count(config, from_score, "outdated"))

    @app.post("/settings/prompt")
    def save_prompt(instructions: str = Form(""), restore: str = Form("")):
        config, text = cfg(), config_path.read_text(encoding="utf-8")
        scoring = dict((yaml.safe_load(text) or {}).get("scoring") or {})
        instructions = "\n".join(line.rstrip() for line in instructions.replace("\r\n", "\n").strip().splitlines())
        if restore or instructions == INSTRUCTIONS:
            instructions = ""  # the built-in prompt, so later versions of it apply too
        if instructions != config.scoring.instructions.strip():
            scoring.pop("instructions", None)
            scoring.update({"instructions": instructions + "\n"} if instructions else {})
            write_config(config_path, replace_sections(text, {"scoring": scoring}))
        return Response(status_code=204, headers={"HX-Redirect": "/settings?saved=" + quote("scoring prompt") + "#prompt"})

    @app.get("/rescore/count")
    def rescore_estimate(request: Request, from_score: int = 0, scope: str = "outdated"):
        return page(request, "_rescore_count.html", count=rescore_count(cfg(), from_score, scope),
                    from_score=from_score, scope=scope)

    @app.post("/rescore")
    def start_rescore(from_score: int = Form(...), scope: str = Form("outdated")):
        if not rescore_count(cfg(), from_score, scope):
            raise HTTPException(400, "No open job to re-score with these choices")
        args = ["rescore", "--from", str(from_score)] + (["--all"] if scope == "all" else [])
        if not activity.start("re-score", args):
            raise HTTPException(409, "Something is already running; wait for it to finish")
        return Response(status_code=204, headers={"HX-Redirect": "/runs"})

    @app.get("/filtered")
    def filtered(request: Request, scope: str = "run", added: str = "", removed: str = "", passing: int = 0):
        config, c = cfg(), conn()
        dropped = filtered_jobs(c, config.filters, scope)
        return page(request, "filtered.html", nav="filtered", scope=scope, dropped=dropped,
                    reasons=Counter(d["reason"] for d in dropped), words=Counter(d["word"] for d in dropped if d["word"]),
                    titles=title_suggestions(dropped, config.filters), places=place_suggestions(dropped),
                    added=added, removed=removed, passing=passing, last_run=last_run_start(c),
                    batch_size=config.scoring.batch_size)

    def change_filter(field: str, term: str, add: bool, scope: str) -> Response:
        if field not in ("title_include", "title_exclude", "locations"):
            raise HTTPException(400, "unknown filter")
        config, text = cfg(), config_path.read_text(encoding="utf-8")
        terms = list(getattr(config.filters, field))
        before = {d["id"] for d in filtered_jobs(conn(), config.filters, "open")}
        terms = [*terms, term] if add and term.lower() not in map(str.lower, terms) else \
            [t for t in terms if t.lower() != term.lower()]
        filters = config.filters.model_copy(update={field: terms})
        write_config(config_path, replace_sections(text, {"filters": filters.model_dump()}))
        passing = len(before - {d["id"] for d in filtered_jobs(conn(), filters, "open")})
        return Response(status_code=204, headers={"HX-Redirect": f"/filtered?scope={scope}&{'added' if add else 'removed'}="
                                                                 f"{quote(term)}&passing={passing}"})

    @app.post("/filters/add")
    def filters_add(field: str = Form(...), term: str = Form(...), scope: str = Form("run")):
        return change_filter(field, term.strip(), True, scope)

    @app.post("/filters/remove")
    def filters_remove(field: str = Form(...), term: str = Form(...), scope: str = Form("run")):
        return change_filter(field, term.strip(), False, scope)

    @app.post("/settings")
    def save_settings(min_score: int = Form(...), title_include: str = Form(""), title_exclude: str = Form(""),
                      locations: str = Form(""), allow_remote: str = Form(""), preferences: str = Form(""),
                      max_runs_per_day: int = Form(...), min_hours_between_runs: float = Form(...),
                      backend: str = Form(""), model: str = Form(""),
                      notify: list[str] = Form([]), packs: list[str] = Form([])):
        config, text = cfg(), config_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        values = {
            "min_score": min_score,
            "filters": {"title_include": _terms(title_include), "title_exclude": _terms(title_exclude),
                        "locations": _terms(locations), "allow_remote": bool(allow_remote)},
            "preferences": preferences.strip().replace("\r\n", "\n") + "\n",
            "schedule": {**config.schedule.model_dump(), "max_runs_per_day": max_runs_per_day,
                         "min_hours_between_runs": min_hours_between_runs},
        }
        scoring, model = dict(raw.get("scoring") or {}), model.strip()
        if backend and backend != config.scoring.backend:
            scoring["backend"] = backend
            scoring.pop("model", None)  # another backend's model name means nothing to the new one...
            model = "" if model == config.scoring.model else model  # ...unless one was typed for it
        if model and model != config.scoring.model:
            scoring["model"] = model
        values.update(scoring=scoring, notify=notify, packs=packs)
        # Only changed sections are rewritten; the rest stay as written, comments included.
        defaults = {"scoring": {}, "notify": [], "packs": []}
        values = {key: value for key, value in values.items() if value != raw.get(key, defaults.get(key))}
        try:
            write_config(config_path, replace_sections(text, values))
        except Exception as e:  # invalid values: say what, change nothing
            raise HTTPException(400, f"Not saved: {e}") from None
        return Response(status_code=204, headers={"HX-Redirect": "/settings?saved=settings"})

    @app.post("/settings/resume")
    def save_resume(resume: str = Form(...)):
        Path(cfg().resume_path).write_text(resume.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        return Response(status_code=204, headers={"HX-Redirect": "/settings?saved=resume"})

    return app


def serve(config_path: str, *, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> int:
    import uvicorn

    local = host in LOCAL_HOSTS
    if not local:
        log.warning("listening on %s: other devices on your network can open this app, and it has no login", host)
    app = create_app(config_path, allowed_hosts=LOCAL_HOSTS if local else None)
    url = f"http://{'127.0.0.1' if not local else host}:{port}/"
    print(f"Shortlist is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
