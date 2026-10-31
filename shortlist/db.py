"""SQLite storage. Each job is stored once per (ats, slug, job_id), scored at most once, and notified at most once."""
import sqlite3

from .fetchers import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    ats         TEXT NOT NULL,
    slug        TEXT NOT NULL,
    job_id      TEXT NOT NULL,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT NOT NULL,
    url         TEXT NOT NULL,
    role_hash   TEXT NOT NULL,  -- equal for identical postings of one role in several locations
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    status      TEXT,           -- NULL until handled, then 'scored', 'failed', or 'duplicate'
    dup_of      TEXT,           -- for duplicates: key of the posting that was scored
    score       INTEGER,
    result      TEXT,           -- validated scoring result as JSON
    scored_at   TEXT,
    notified_at TEXT,
    PRIMARY KEY (ats, slug, job_id)
)"""
RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    started_at   TEXT NOT NULL,     -- UTC ISO timestamp
    jobs_fetched INTEGER NOT NULL,
    exit_code    INTEGER NOT NULL
)"""
SITES_SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    url           TEXT PRIMARY KEY, -- a careers page from config.career_pages
    name          TEXT NOT NULL,
    status        TEXT NOT NULL,    -- 'board' (a supported job board), 'recipe' (CSS recipe), 'unsupported'
    board         TEXT,             -- "ats:slug" when status = 'board'
    recipe        TEXT,             -- HtmlBoard JSON when status = 'recipe'
    note          TEXT,             -- how it was found, or why it is unsupported
    agent_calls   INTEGER NOT NULL, -- LLM calls this discovery used
    discovered_at TEXT NOT NULL,
    retry_after   TEXT,             -- don't discover this site again before then
    empty_runs    INTEGER NOT NULL DEFAULT 0,  -- consecutive runs in which it returned no jobs
    last_ok       TEXT              -- last run in which it returned jobs
)"""
SCANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS board_scans (
    board          TEXT PRIMARY KEY, -- "ats:slug" or "html:<url>"
    last_full_scan TEXT NOT NULL
)"""
TRACKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracking (   -- what you did with a job (web app)
    ats        TEXT NOT NULL,
    slug       TEXT NOT NULL,
    job_id     TEXT NOT NULL,
    stage      TEXT NOT NULL DEFAULT 'new',  -- one of STAGES
    notes      TEXT NOT NULL DEFAULT '',
    feedback   INTEGER NOT NULL DEFAULT 0,   -- 1 thumbs up, -1 thumbs down
    applied_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (ats, slug, job_id)
)"""
STAGES = ["new", "saved", "applied", "interviewing", "offer", "rejected", "not_interested"]
# Columns added after the first release of a table; `connect` adds any that an older database lacks.
ADDED_COLUMNS = {
    "jobs": {"description": "TEXT",   # kept for scored jobs, for the job detail view and rescoring
             "scored_with": "TEXT"},  # fingerprint of the resume, preferences, and prompt a job was scored with
    "runs": {"finished_at": "TEXT", "dry_run": "INTEGER NOT NULL DEFAULT 0", "first_seen": "INTEGER",
             "scored": "INTEGER", "matches": "INTEGER", "sent": "INTEGER"},
}
WHERE_JOB = "WHERE ats = ? AND slug = ? AND job_id = ?"


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)  # the web app and a run may write at the same time
    conn.row_factory = sqlite3.Row
    for schema in (SCHEMA, RUNS_SCHEMA, SITES_SCHEMA, SCANS_SCHEMA, TRACKING_SCHEMA):
        conn.execute(schema)
    with conn:
        for table, columns in ADDED_COLUMNS.items():
            have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, declaration in columns.items():
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    return conn


def known_keys(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT ats || ':' || slug || ':' || job_id FROM jobs")}


def unscored_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT ats, slug, job_id, company, title, location, url FROM jobs WHERE status IS NULL").fetchall()


def full_scans_due(conn: sqlite3.Connection, boards: list[str], before: str) -> set[str]:
    """Boards never fully scanned, or last fully scanned before `before`."""
    last = dict(conn.execute("SELECT board, last_full_scan FROM board_scans").fetchall())
    return {b for b in boards if last.get(b, "") < before}


def record_full_scans(conn: sqlite3.Connection, boards: set[str], now: str) -> None:
    with conn:
        conn.executemany("INSERT OR REPLACE INTO board_scans (board, last_full_scan) VALUES (?, ?)",
                         [(b, now) for b in boards])


def sites(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["url"]: row for row in conn.execute("SELECT * FROM sites ORDER BY name")}


def save_site(conn: sqlite3.Connection, url: str, name: str, status: str, *, board: str | None = None,
              recipe: str | None = None, note: str = "", agent_calls: int = 0, now: str,
              retry_after: str | None = None) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO sites (url, name, status, board, recipe, note, agent_calls, discovered_at,"
                     " retry_after, empty_runs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                     (url, name, status, board, recipe, note, agent_calls, now, retry_after))


def record_site_results(conn: sqlite3.Connection, jobs_by_url: dict[str, int], now: str) -> None:
    """After a run: count consecutive empty runs per site, so a site whose board or recipe broke gets rediscovered."""
    with conn:
        for url, n in jobs_by_url.items():
            if n:
                conn.execute("UPDATE sites SET empty_runs = 0, last_ok = ? WHERE url = ?", (now, url))
            else:
                conn.execute("UPDATE sites SET empty_runs = empty_runs + 1 WHERE url = ?", (url,))


def forget_site(conn: sqlite3.Connection, name_or_url: str) -> int:
    with conn:
        return conn.execute("DELETE FROM sites WHERE url = ? OR lower(name) = lower(?)", (name_or_url, name_or_url)).rowcount


def record_run(conn: sqlite3.Connection, started_at: str, jobs_fetched: int, exit_code: int, *, dry_run: bool = False,
               finished_at: str | None = None, stats: dict | None = None) -> None:
    stats = stats or {}
    with conn:
        conn.execute("INSERT INTO runs (started_at, jobs_fetched, exit_code, dry_run, finished_at, first_seen, scored,"
                     " matches, sent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (started_at, jobs_fetched, exit_code, int(dry_run), finished_at, stats.get("first_seen"),
                      stats.get("scored"), stats.get("matches"), stats.get("sent")))


def completed_runs(conn: sqlite3.Connection, since: str) -> list[str]:
    """Start times of real (not dry) runs since `since` that fetched jobs and finished cleanly, newest first."""
    return [row[0] for row in conn.execute(
        "SELECT started_at FROM runs WHERE exit_code = 0 AND jobs_fetched > 0 AND dry_run = 0 AND started_at >= ?"
        " ORDER BY started_at DESC", (since,))]


def recent_runs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()


def job_row(conn: sqlite3.Connection, rowid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT j.rowid AS id, j.*, coalesce(t.stage, 'new') AS stage, coalesce(t.notes, '') AS notes,"
                        " coalesce(t.feedback, 0) AS feedback, t.applied_at FROM jobs j LEFT JOIN tracking t"
                        " USING (ats, slug, job_id) WHERE j.rowid = ?", (rowid,)).fetchone()


def set_tracking(conn: sqlite3.Connection, rowid: int, now: str, *, stage: str | None = None, notes: str | None = None,
                 feedback: int | None = None) -> None:
    """Update what the user did with a job; setting the stage to 'applied' the first time records the date."""
    row = job_row(conn, rowid)
    if row is None:
        raise KeyError(rowid)
    stage = stage if stage is not None else row["stage"]
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    applied_at = row["applied_at"] or (now if stage == "applied" else None)
    with conn:
        conn.execute("INSERT OR REPLACE INTO tracking (ats, slug, job_id, stage, notes, feedback, applied_at, updated_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (row["ats"], row["slug"], row["job_id"], stage, notes if notes is not None else row["notes"],
                      feedback if feedback is not None else row["feedback"], applied_at, now))


def upsert_jobs(conn: sqlite3.Connection, jobs: list[Job], now: str) -> int:
    """Insert unseen jobs and refresh last_seen on known ones. Returns how many were new."""
    with conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO jobs (ats, slug, job_id, company, title, location, url, role_hash, first_seen,"
            " last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(j.ats, j.slug, j.job_id, j.company, j.title, j.location, j.url, j.role_hash, now, now) for j in jobs])
        inserted = conn.total_changes - before
        conn.executemany(f"UPDATE jobs SET last_seen = ? {WHERE_JOB}", [(now, j.ats, j.slug, j.job_id) for j in jobs])
    return inserted


def unscored_keys(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT ats || ':' || slug || ':' || job_id FROM jobs WHERE status IS NULL")}


def handled_roles(conn: sqlite3.Connection) -> dict[str, str]:
    """role_hash -> key of the posting that was scored (or failed) for that role."""
    return {row[0]: row[1] for row in conn.execute(
        "SELECT role_hash, ats || ':' || slug || ':' || job_id FROM jobs WHERE status IN ('scored', 'failed')")}


def save_result(conn: sqlite3.Connection, job: Job, result_json: str | None, score: int | None, now: str,
                scored_with: str = "") -> None:
    """Record a score (or a permanent failure when result_json is None). `job.location` may list several locations,
    and `job.role_hash` reflects the full description when it was fetched after the job was first stored."""
    with conn:
        conn.execute(f"UPDATE jobs SET status = ?, score = ?, result = ?, scored_at = ?, location = ?, role_hash = ?,"
                     f" description = ?, scored_with = ? {WHERE_JOB}",
                     ("scored" if result_json else "failed", score, result_json, now, job.location, job.role_hash,
                      job.description, scored_with or None, job.ats, job.slug, job.job_id))


def mark_duplicates(conn: sqlite3.Connection, jobs: list[Job], of_key: str) -> None:
    with conn:
        conn.executemany(f"UPDATE jobs SET status = 'duplicate', dup_of = ? {WHERE_JOB}",
                         [(of_key, j.ats, j.slug, j.job_id) for j in jobs])


def mark_notified(conn: sqlite3.Connection, rows: list[sqlite3.Row], now: str) -> None:
    with conn:
        conn.executemany(f"UPDATE jobs SET notified_at = ? {WHERE_JOB}",
                         [(now, r["ats"], r["slug"], r["job_id"]) for r in rows])


# A posting is closed when its board was read without it. Sources that list every open job do so on each read; Workday
# boards are read to the end at least weekly (FULL_SCAN_EVERY), so a job unseen for longer than that is gone. Amazon
# searches (newest 500) and HTML boards (limited pages) can drop an open job, so they never count as closed.
COMPLETE_LISTS = ("greenhouse", "lever", "ashby", "workable", "simplify")
BOARD_SEEN = "(SELECT ats, slug, max(last_seen) AS board_seen FROM jobs GROUP BY ats, slug)"
CLOSED = (f"(j.last_seen < b.board_seen AND (j.ats IN {COMPLETE_LISTS} OR j.ats = 'workday'"
          " AND julianday(b.board_seen) - julianday(j.last_seen) > 8))")


def feedback_examples(conn: sqlite3.Connection, min_score: int, limit: int) -> list[dict]:
    """Jobs the user judged, as examples for the scorer: thumbs up or down, or else what they did (applied is a good
    fit, "not interested" a poor one). Ratings that disagree with the score come first, they teach the most; then the
    most recent. At most half the examples are of one kind, when the other kind has enough."""
    rows = conn.execute(
        "SELECT j.title, j.company, j.location, j.score, t.feedback, t.stage, t.updated_at FROM tracking t"
        " JOIN jobs j USING (ats, slug, job_id) WHERE j.status = 'scored' AND (t.feedback != 0"
        " OR t.stage IN ('applied', 'interviewing', 'offer', 'not_interested')) ORDER BY t.updated_at DESC").fetchall()
    examples = []
    for r in rows:
        good = r["feedback"] > 0 if r["feedback"] else r["stage"] != "not_interested"
        examples.append({"title": r["title"], "company": r["company"], "location": r["location"], "score": r["score"],
                         "good": good, "disagrees": good != (r["score"] >= min_score)})
    examples.sort(key=lambda e: not e["disagrees"])  # stable: disagreements first, each group newest first
    good, poor = [e for e in examples if e["good"]], [e for e in examples if not e["good"]]
    n_good = min(len(good), max(limit // 2, limit - len(poor)))
    return sorted(good[:n_good] + poor[:limit - n_good], key=lambda e: not e["disagrees"])


def rescore_candidates(conn: sqlite3.Connection, from_score: int, outdated_from: str | None) -> list[sqlite3.Row]:
    """Open scored jobs at `from_score` or above, best first; with `outdated_from` (the current fingerprint), only
    those scored with another resume, preferences, or prompt."""
    return conn.execute(
        f"SELECT j.* FROM jobs j JOIN {BOARD_SEEN} b USING (ats, slug) WHERE j.status = 'scored' AND j.score >= ?"
        f" AND NOT {CLOSED}"
        + (" AND coalesce(j.scored_with, '') != ?" if outdated_from else "") + " ORDER BY j.score DESC",
        (from_score, outdated_from) if outdated_from else (from_score,)).fetchall()


def pending_matches(conn: sqlite3.Connection, min_score: int, seen_since: str) -> list[sqlite3.Row]:
    """Scored jobs at or above min_score, not yet notified, and seen on their board since `seen_since` (paged boards
    are not re-read to the end every run, so "seen recently" stands for "still listed"); best first."""
    return conn.execute(
        "SELECT ats, slug, job_id, company, title, location, url, score, result FROM jobs WHERE status = 'scored'"
        " AND score >= ? AND notified_at IS NULL AND last_seen >= ? ORDER BY score DESC, company, title",
        (min_score, seen_since)).fetchall()
