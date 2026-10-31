"""Careers pages without a known job board, read with CSS-selector recipes (`html_boards` in config.yaml, or found
by discovery and kept in the database). A recipe without a `jobs` selector reads the page's schema.org JobPosting
data. Job descriptions are fetched later, only for new jobs that pass the filters (details.py)."""
import hashlib
import logging
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .config import HtmlBoard
from .details import job_postings, posting_location, posting_text
from .fetchers import MAX_DESCRIPTION_CHARS, Job, describe_error, nothing_new
from .polite import USER_AGENT, Blocked, Polite
from .render import Renderer

log = logging.getLogger(__name__)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _job(board: HtmlBoard, url: str, title: str, location: str, description: str = "", id_extra: str = "") -> Job:
    return Job(ats="html", slug=re.sub(r"[^a-z0-9]+", "-", board.name.lower()).strip("-"),
               job_id=hashlib.sha1(f"{url}{id_extra}".encode()).hexdigest()[:16], company=board.name, title=title,
               location=location, remote="remote" in location.lower(), url=url,
               description=description[:MAX_DESCRIPTION_CHARS], detail_url="" if description else url)


def parse_board_page(html: str, page_url: str, board: HtmlBoard) -> tuple[list[Job], str | None]:
    """The jobs on one page, and the next page's URL when the recipe names a next-page link that is present."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    if board.jobs:
        for el in soup.select(board.jobs):
            link = el if el.name == "a" and el.get("href") else el.select_one(board.link)
            title_el = el.select_one(board.title) if board.title else link
            if link is None or not link.get("href") or title_el is None:
                continue
            location_el = el.select_one(board.location) if board.location else None
            title = _one_line(title_el.get_text(" ", strip=True))
            if title:
                jobs.append(_job(board, urljoin(page_url, link["href"]).split("#")[0], title,
                                 _one_line(location_el.get_text(" ", strip=True)) if location_el else ""))
    else:
        for posting in job_postings(soup):
            url = urljoin(page_url, str(posting.get("url") or page_url))
            title = _one_line(str(posting.get("title") or ""))
            if title:  # postings without their own URL share the page's, so the title keeps their ids apart
                jobs.append(_job(board, url, title, posting_location(posting), posting_text(posting),
                                 id_extra="" if posting.get("url") else title))
    next_link = soup.select_one(board.next_page) if board.next_page else None
    return jobs, urljoin(page_url, next_link["href"]) if next_link is not None and next_link.get("href") else None


def board_id(board: HtmlBoard) -> str:
    """Identifies an HTML board in the full-scan schedule (several boards may share a company name)."""
    return f"html:{board.url}"


def _scrape(board: HtmlBoard, polite: Polite, renderer: Renderer, known: set[str] | None) -> list[Job]:
    url, seen, jobs = board.url, set(), {}
    for _ in range(board.max_pages):
        seen.add(url)
        if board.render:
            if not board.ignore_robots and not polite.allowed(url):
                raise Blocked(url)
            html = renderer.html(url, wait_for=board.jobs)
        else:
            response = polite.get(url, respect_robots=not board.ignore_robots)
            if response.status_code != 200 or not response.text.strip():  # e.g. 202 + empty body: a bot challenge
                raise RuntimeError(f"HTTP {response.status_code} with {'an empty' if not response.text.strip() else 'a'}"
                                   " page, likely bot protection")
            html = response.text
        page_jobs, url = parse_board_page(html, url, board)
        for job in page_jobs:
            jobs.setdefault(job.key, job)
        if not url or url in seen or nothing_new(page_jobs, known):
            break
    return list(jobs.values())


def scrape_boards(boards: list[HtmlBoard], *, known: set[str] | None = None, full_scan: set[str] = frozenset(),
                  transport: httpx.BaseTransport | None = None) -> list[Job]:
    """Every board's jobs; a board that fails is logged and skipped. The browser starts only if a board needs it.
    With `known`, a board stops at a page with nothing new, unless its board_id is in `full_scan`."""
    if not boards:
        return []
    jobs = []
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}, transport=transport) as client, \
            Renderer() as renderer:
        polite = Polite(client)
        for board in boards:
            try:
                found = _scrape(board, polite, renderer, None if board_id(board) in full_scan else known)
            except Exception as e:  # one board failing must never crash the run
                log.warning("html:%s: skipped (%s)", board.name, describe_error(e))
                continue
            log.info("html:%s: %d jobs", board.name, len(found))
            jobs += found
    return jobs
