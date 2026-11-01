"""Turn careers pages (`career_pages`) into boards Shortlist can read, during every run, and remember each result in
the database so a site costs agent calls only when it is new or its board stopped returning jobs.

Cheapest first:
1. A supported job board linked from the careers page (Greenhouse, Lever, Ashby, Workday, Workable).
2. A supported board guessed from the company name, accepted only if its jobs mention the company.
3. schema.org JobPosting data on the careers page.
4. The agent: structured LLM calls through the scoring backend (CLI or API). Each call names a board, opens a linked
   page, asks for the page rendered in a browser, writes a CSS recipe, or gives up. Boards and recipes are checked
   against the real site before they are kept; a failed check is explained to the next call.
"""
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import unquote, urljoin

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ValidationError

from . import db
from .config import CareerPage, Config, HtmlBoard
from .fetchers import FETCHERS, Job, describe_error
from .html_boards import parse_board_page
from .polite import USER_AGENT, Blocked, Polite
from .render import Renderer, RenderUnavailable
from .scoring import BACKENDS

log = logging.getLogger(__name__)

RETRY_FOUND = timedelta(days=7)         # a working site is rediscovered at most weekly, even if it keeps coming up empty
RETRY_UNSUPPORTED = timedelta(days=30)  # sites change; try unreadable ones again monthly

SIGNATURES = [  # job boards linked from a careers page, as they appear in its links and embed scripts
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("lever", r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("workday", r"([a-z0-9-]+\.wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
]
NOT_SLUGS = {"embed", "v1", "boards", "api", "jobs", "j", "careers", "job_board", "js", "static", "assets"}
NOT_WORKDAY_SITES = {"wday"}  # "careers" is a real Workday site name (Yahoo's), unlike on the other boards
GENERIC_WORDS = {"ai", "labs", "lab", "inc", "the", "technologies", "technology", "group", "company", "co", "and",
                 "for", "of", "global", "tech"}
JOBISH = re.compile(r"job|career|position|opening|role|vacanc|join|hiring|apply|greenhouse|lever|ashby|workday|workable",
                    re.I)
NOT_TITLES = {"apply", "apply now", "learn more", "view job", "view details", "see details", "read more", "details",
              "view all jobs", "see all jobs", "view", "more"}


class _FirstPageOnly(frozenset):
    """Passed as `known` while checking a board, so paged boards stop after their first page."""

    def __contains__(self, item):
        return True


FIRST_PAGE = _FirstPageOnly()


@dataclass
class Page:
    url: str
    html: str = ""
    rendered: bool = False
    problem: str = ""  # why it could not be read


@dataclass
class Outcome:
    status: str                     # 'board', 'recipe', 'unsupported', or 'later' (try again next run, nothing stored)
    board: str = ""
    recipe: HtmlBoard | None = None
    note: str = ""
    agent_calls: int = 0


class AgentStep(BaseModel):
    action: Literal["board", "recipe", "open", "render", "give_up"]
    board: str = ""      # board: "greenhouse:<slug>", "lever:<slug>", "ashby:<slug>", "workday:<tenant>.wd<N>/<site>", "workable:<account>"
    url: str = ""        # open: a link from the page
    jobs: str = ""       # recipe: CSS selector matching one element per job
    title: str = ""      # recipe: inside a job element; empty = the link's text
    link: str = ""       # recipe: inside a job element; empty = the job element itself, or its first link
    location: str = ""   # recipe: inside a job element, optional
    next_page: str = ""  # recipe: the link to the next page of results, optional
    reason: str = ""


def _words(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", re.sub(r"\(.*?\)", "", name.lower()))


def name_tokens(name: str) -> list[str]:
    words = _words(name)
    return [w for w in words if w not in GENERIC_WORDS and len(w) >= 3] or words[:1]


def mentions_company(jobs: list[Job], name: str) -> bool:
    """Whether a guessed board belongs to this company. Boards that report a company name must share at least half
    of its distinctive words ("DeepMind" fits "Google DeepMind", "Acme Insurance" doesn't fit "Acme Robotics");
    boards that only have a slug (Lever, Ashby) must have descriptions that mention the company's full name."""
    want = set(name_tokens(name))
    if jobs and jobs[0].ats not in ("lever", "ashby"):  # these boards report the company's own name
        have = set(name_tokens(jobs[0].company))
        return len(want & have) / len(want | have) >= 0.5
    full = " ".join(_words(name))
    texts = [" ".join(re.findall(r"[a-z0-9]+", j.description.lower())) for j in jobs[:15] if j.description]
    hits = sum(full in text or all(w in text.split() for w in want) for text in texts)
    return bool(texts) and hits >= max(1, 0.3 * len(texts))


def linked_boards(text: str) -> list[str]:
    found = []
    for ats, pattern in SIGNATURES:
        for m in re.finditer(pattern, text):
            if ats == "workday":
                if m.group(2).lower() not in NOT_WORKDAY_SITES:
                    found.append(f"workday:{m.group(1)}/{m.group(2)}")
            elif (slug := unquote(m.group(1)).strip(".")) and slug.lower() not in NOT_SLUGS:
                found.append(f"{ats}:{slug}")
    return list(dict.fromkeys(found))[:8]


def name_guesses(name: str) -> list[str]:
    words = _words(name)
    core = [w for w in words if w not in GENERIC_WORDS] or words
    slugs = dict.fromkeys(s for s in ("".join(words), "-".join(words), "".join(core), "-".join(core), *core[:1]) if s)
    return [f"{ats}:{slug}" for slug in slugs for ats in ("greenhouse", "ashby", "lever", "workable")]


def check_board(polite: Polite, board: str, name: str, *, linked: bool = False) -> tuple[int, str]:
    """(jobs on its first page, problem). A board linked from the company's own page needn't mention the company."""
    ats, _, slug = board.partition(":")
    if ats not in FETCHERS or not slug or (ats == "workday" and not re.fullmatch(r"[\w-]+\.wd\d+/[\w-]+", slug)):
        return 0, "not a supported board name"
    try:
        jobs = FETCHERS[ats](polite, slug, FIRST_PAGE)
    except Exception as e:  # a wrong guess is expected; never crash discovery
        return 0, describe_error(e)
    if not jobs:
        return 0, "it lists no jobs"
    if not linked and not mentions_company(jobs, name):
        return 0, "its jobs don't mention the company"
    return len(jobs), ""


def recipe_problem(jobs: list[Job]) -> str:
    if not jobs:
        return "it matched no jobs"
    titles = [j.title for j in jobs]
    if len(jobs) > 2 and len(set(titles)) < len(titles) / 2:
        return f"the titles repeat ({titles[:3]}), so it matched something other than jobs"
    odd = [t for t in titles if len(t) < 4 or len(t) > 150 or t.lower() in NOT_TITLES]
    if len(odd) > len(titles) / 3:
        return f"these don't look like job titles: {odd[:3]}"
    if len({j.url for j in jobs}) < 0.8 * len(jobs):
        return "several jobs share one link"
    return ""


def readable_chars(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return len(soup.get_text(" ", strip=True))


def load_page(url: str, polite: Polite, renderer: Renderer, *, render: bool = False) -> Page:
    """The page's HTML; rendered in a browser when asked to, or when its HTML has almost no text (JavaScript-only).
    Bot protection (403, empty 202) is reported, never worked around."""
    try:
        response = polite.get(url)
    except Blocked as e:
        return Page(url, problem="robots.txt disallows it" if e.reason == "blocked by robots.txt" else e.reason)
    except Exception as e:
        return Page(url, problem=describe_error(e))
    if response.status_code != 200 or not response.text.strip():
        return Page(str(response.url), problem=f"HTTP {response.status_code} with an empty page (likely bot protection)")
    page = Page(str(response.url), response.text)
    if render or readable_chars(page.html) < 500:
        try:
            page = Page(page.url, renderer.html(page.url), rendered=True)
        except Exception as e:  # no browser installed, or the page would not render: keep the plain HTML
            log.info("%s: not rendered (%s)", page.url, describe_error(e))
    return page


def from_page(page: Page, name: str, polite: Polite) -> Outcome | None:
    """A board linked from the page, or the page's own JobPosting data; no agent calls."""
    for board in linked_boards(f"{page.url} {page.html}"):
        if check_board(polite, board, name, linked=True)[0]:
            return Outcome("board", board=board, note=f"linked from {page.url}")
    recipe = HtmlBoard(name=name, url=page.url, render=page.rendered)
    jobs, _ = parse_board_page(page.html, page.url, recipe)
    if jobs and not recipe_problem(jobs):
        return Outcome("recipe", recipe=recipe, note=f"JobPosting data on {page.url}")
    return None


def _job_list_region(soup: BeautifulSoup):
    """The smallest element holding most of the title-like links (3+ words), i.e. the likely job list."""
    counts, elements = {}, {}
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if 12 <= len(text) <= 140 and len(text.split()) >= 3:
            for parent in a.parents:
                counts[id(parent)] = counts.get(id(parent), 0) + 1
                elements[id(parent)] = parent
    best = max(counts.values(), default=0)
    candidates = [elements[i] for i, n in counts.items() if n >= 0.8 * best and elements[i].name not in ("[document]", "html")]
    return min(candidates, key=lambda el: len(str(el))) if best >= 3 and candidates else (soup.body or soup)


def _compact_html(element) -> str:
    soup = BeautifulSoup(str(element), "html.parser")
    for tag in soup(["img", "picture", "video", "source", "iframe", "input", "button", "path", "meta", "link", "svg"]):
        tag.decompose()
    for tag in soup.find_all(True):
        tag.attrs = {k: (" ".join(v[:3]) if isinstance(v, list) else str(v)[:120]) for k, v in tag.attrs.items()
                     if k in ("class", "id", "href", "rel", "role", "aria-label") or k.startswith("data-")}
    return re.sub(r"\s+", " ", str(soup))


def describe_page(page: Page) -> str:
    soup = BeautifulSoup(page.html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    links = []
    for a in soup.find_all("a", href=True):
        text, href = " ".join(a.get_text(" ", strip=True).split())[:100], urljoin(page.url, a["href"])
        if text and href.startswith("http"):
            links.append(f"{text} -> {href}")
    links = list(dict.fromkeys(links))
    links = ([link for link in links if JOBISH.search(link)] + [link for link in links if not JOBISH.search(link)])[:120]
    for tag in soup(["nav", "header", "footer"]):
        tag.decompose()
    rendered = " (HTML after JavaScript ran in a browser)" if page.rendered else ""
    return (f"URL: {page.url}{rendered}\nTitle: {title}\n\nLinks (text -> URL), job-related first:\n" + "\n".join(links)
            + f"\n\nHTML around the most likely job list (attributes trimmed):\n{_compact_html(_job_list_region(soup))[:12000]}")


INSTRUCTIONS = """You help a program find where a company lists its open jobs, so the program can read them every day
without you. Answer with exactly one action:
- "board": the jobs live on a job board the program reads. Set `board` to greenhouse:<slug> (boards.greenhouse.io/<slug>
  or job-boards.greenhouse.io/<slug>), lever:<slug> (jobs.lever.co/<slug>), ashby:<slug> (jobs.ashbyhq.com/<slug>),
  workday:<tenant>.wd<N>/<site> (<tenant>.wd<N>.myworkdayjobs.com/<site>) or workable:<account>
  (apply.workable.com/<account>). Use a slug you saw on the page, or one you are confident about.
- "recipe": the CURRENT page lists jobs itself. Give CSS selectors (soupsieve syntax): `jobs` matches one element per
  job; `title`, `link`, `location` are looked up inside that element (title empty: use the link's text; link empty: the
  job element is the link, or its first link); `next_page` matches the link to the next page of results, if any.
- "open": the jobs are probably on another page; set `url` to one of the links shown (for example "See open roles").
- "render": the current page probably fills its job list with JavaScript; load it in a browser.
- "give_up": the company has no job list the program could read; say why in `reason`.
Prefer "board" whenever the jobs are hosted on one of those boards. Keep `reason` to one sentence."""


def agent_discover(site: CareerPage, page: Page, polite: Polite, renderer: Renderer, ask, max_calls: int) -> Outcome:
    history, visited, calls = [], {page.url}, 0
    while calls < max_calls:
        prompt = (f"{INSTRUCTIONS}\n\nCompany: {site.name}\nCareers page given by the user: {site.url}\n\n"
                  + ("What you tried so far:\n" + "\n".join(f"- {h}" for h in history) + "\n\n" if history else "")
                  + f"Current page:\n{describe_page(page)}")
        try:
            answer = ask(prompt)
        except Exception as e:  # the backend failed (timeout, quota): try this site again next run
            return Outcome("later", note=f"agent call failed ({describe_error(e)})", agent_calls=calls + 1)
        calls += 1
        try:
            step = AgentStep.model_validate(answer)
        except ValidationError:
            history.append("Your last answer did not match the expected fields.")
            continue
        if step.action == "give_up":
            return Outcome("unsupported", note=f"agent gave up: {step.reason}", agent_calls=calls)
        if step.action == "board":
            jobs, problem = check_board(polite, step.board.strip(), site.name)
            if jobs:
                return Outcome("board", board=step.board.strip(), note=f"agent: {step.reason}", agent_calls=calls)
            history.append(f'You proposed board "{step.board}": rejected, {problem}.')
        elif step.action == "recipe":
            recipe = HtmlBoard(name=site.name, url=page.url, jobs=step.jobs, title=step.title, link=step.link or "a[href]",
                               location=step.location, next_page=step.next_page, render=page.rendered)
            try:
                jobs, _ = parse_board_page(page.html, page.url, recipe)
                problem = recipe_problem(jobs)
            except Exception as e:  # an invalid selector
                jobs, problem = [], f"the selector failed: {describe_error(e)}"
            if not problem:
                return Outcome("recipe", recipe=recipe, note=f"agent recipe, {len(jobs)} jobs on its first page",
                               agent_calls=calls)
            sample = "; ".join(f"{j.title!r} -> {j.url}" for j in jobs[:5]) or "none"
            history.append(f"Your recipe {recipe.model_dump(include={'jobs', 'title', 'link', 'location'})} was rejected:"
                           f" {problem}. First matches: {sample}.")
        else:  # open or render
            target = page.url if step.action == "render" else urljoin(page.url, step.url)
            if step.action == "open" and target in visited:
                history.append(f"You asked to open {target} again; it is the page shown, or was shown before.")
                continue
            visited.add(target)
            new = load_page(target, polite, renderer, render=step.action == "render")
            if new.problem:
                history.append(f"Opening {target} failed: {new.problem}.")
                continue
            if found := from_page(new, site.name, polite):  # a board link or JobPosting data: no further calls needed
                found.agent_calls, found.note = calls, f"{found.note} (agent opened it)"
                return found
            history.append(f"Opened {target}{' in a browser' if new.rendered else ''}; it is the current page below.")
            page = new
    return Outcome("unsupported", note=f"no board or recipe within {max_calls} agent calls", agent_calls=calls)


def discover_site(site: CareerPage, polite: Polite, renderer: Renderer, ask, max_calls: int) -> Outcome:
    page = load_page(site.url, polite, renderer)
    if not page.problem and (found := from_page(page, site.name, polite)):
        return found
    for board in name_guesses(site.name):
        if check_board(polite, board, site.name)[0]:
            return Outcome("board", board=board, note="guessed from the company name; its jobs mention the company")
    if page.problem:
        return Outcome("unsupported", note=f"careers page: {page.problem}")
    if max_calls <= 0:
        return Outcome("later", note="no agent calls left in this run")
    return agent_discover(site, page, polite, renderer, ask, max_calls)


def _needs_discovery(row: sqlite3.Row | None, cfg: Config, now: str) -> bool:
    if row is None:
        return True
    due = not row["retry_after"] or row["retry_after"] <= now
    if row["status"] == "unsupported":
        return due
    return due and row["empty_runs"] >= cfg.discovery.rediscover_after_empty_runs


def make_asker(cfg: Config):
    """Agent calls go through the same backend interface as scoring, so CLI and API backends both work."""
    backend = BACKENDS[cfg.scoring.backend]
    settings = cfg.scoring.model_copy(update={"model": cfg.discovery.model or cfg.scoring.model})
    schema = AgentStep.model_json_schema()
    return lambda prompt: backend(prompt, schema, settings)


def discover_sites(cfg: Config, conn: sqlite3.Connection, now: datetime, *, ask=None,
                   transport: httpx.BaseTransport | None = None) -> list[sqlite3.Row]:
    """Discover new and due career pages, store what was found, and return the stored rows of every career page that
    resolved to a board or recipe."""
    stamp = now.isoformat(timespec="seconds")
    stored = db.sites(conn)
    due = [p for p in cfg.career_pages if _needs_discovery(stored.get(p.url), cfg, stamp)]
    if not due and cfg.career_pages:
        log.info("all %d career pages are discovered and working; nothing to discover", len(cfg.career_pages))
    if due:
        ask, budget, counts = ask or make_asker(cfg), cfg.discovery.max_agent_calls_per_run, {}
        with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}, transport=transport) \
                as client, Renderer() as renderer:
            polite = Polite(client)
            for site in due:
                outcome = discover_site(site, polite, renderer, ask, min(cfg.discovery.max_agent_calls_per_site, budget))
                budget -= outcome.agent_calls
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
                found = outcome.board or (outcome.recipe and f"recipe {outcome.recipe.url}") or ""
                log.info("site %s: %s %s(%d agent calls) %s", site.name, outcome.status, f"{found} " if found else "",
                         outcome.agent_calls, outcome.note)
                if outcome.status != "later":
                    retry = now + (RETRY_UNSUPPORTED if outcome.status == "unsupported" else RETRY_FOUND)
                    db.save_site(conn, site.url, site.name, outcome.status, board=outcome.board or None,
                                 recipe=outcome.recipe.model_dump_json() if outcome.recipe else None, note=outcome.note,
                                 agent_calls=outcome.agent_calls, now=stamp, retry_after=retry.isoformat(timespec="seconds"))
        log.info("discovered %d career pages: %s; %d agent calls left in this run's budget", len(due),
                 ", ".join(f"{n} {s}" for s, n in sorted(counts.items())), budget)
    stored = db.sites(conn)
    return [stored[p.url] for p in cfg.career_pages if p.url in stored and stored[p.url]["status"] in ("board", "recipe")]


def site_sources(rows: list[sqlite3.Row]) -> tuple[list[str], list[HtmlBoard]]:
    """The `companies` entries and HTML recipes the discovered sites add to a run."""
    return ([r["board"] for r in rows if r["status"] == "board"],
            [HtmlBoard.model_validate_json(r["recipe"]) for r in rows if r["status"] == "recipe"])


def site_job_counts(rows: list[sqlite3.Row], jobs: list[Job]) -> dict[str, int]:
    """Jobs each discovered site produced in this run, keyed by careers page URL."""
    per_board: dict[tuple[str, str], int] = {}
    for job in jobs:
        per_board[(job.ats, job.slug)] = per_board.get((job.ats, job.slug), 0) + 1
    counts = {}
    for r in rows:
        if r["status"] == "board":
            ats, _, slug = r["board"].partition(":")
        else:
            ats, slug = "html", re.sub(r"[^a-z0-9]+", "-", r["name"].lower()).strip("-")
        counts[r["url"]] = per_board.get((ats, slug), 0)
    return counts
