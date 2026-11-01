"""Fetch open jobs from public Greenhouse, Lever, and Ashby job boards and normalize them into Job.

Fields parsed here are only those seen in the saved responses under tests/fixtures/.
"""
import hashlib
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
from pydantic import BaseModel

from .polite import USER_AGENT, Blocked, Polite

log = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 8000
_BLOCK_TAG = re.compile(r"<\s*(?:br|li|/p|/div|/li|/h[1-6]|/tr)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


class Job(BaseModel):
    ats: str
    slug: str
    job_id: str
    company: str
    title: str
    location: str
    remote: bool
    url: str
    description: str
    detail_url: str = ""  # set when the description is only a listing note and the full text can be fetched here

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.slug}:{self.job_id}"

    @property
    def role_hash(self) -> str:
        """Same company, title and description text: the same role posted in several locations."""
        return hashlib.sha1("\n".join([self.ats, self.slug, self.title, self.description]).encode()).hexdigest()


def clean_text(s: str) -> str:
    s = re.sub(r"[^\S\n]+", " ", s)  # collapse spaces, tabs and nbsp, keep newlines
    return re.sub(r"\s*\n\s*", "\n", s).strip()


def html_to_text(s: str) -> str:
    return clean_text(html.unescape(_TAG.sub("", _BLOCK_TAG.sub("\n", s))))


def _job(ats, slug, job_id, company, title, locations, workplace_type, url, description, detail_url="") -> Job:
    location = "; ".join(loc.strip() for loc in locations if loc and loc.strip())
    remote = workplace_type.lower() == "remote" or "remote" in location.lower()
    return Job(ats=ats, slug=slug, job_id=str(job_id), company=company.strip() or slug, title=title.strip(),
               location=location, remote=remote, url=url, description=description[:MAX_DESCRIPTION_CHARS],
               detail_url=detail_url)


def parse_greenhouse(data: dict, slug: str) -> list[Job]:
    """With `content` (entity-escaped HTML, so unescaped once) the description is filled in; without it, the
    description is fetched later from the single-job API, only for new jobs that pass the filters."""
    return [_job("greenhouse", slug, j["id"], j.get("company_name") or slug, j["title"],
                 [(j.get("location") or {}).get("name", "")], "", j["absolute_url"],
                 html_to_text(html.unescape(j.get("content") or "")),
                 detail_url="" if j.get("content") else f"https://job-boards.greenhouse.io/{slug}/jobs/{j['id']}")
            for j in data["jobs"]]


def parse_lever(data: list, slug: str) -> list[Job]:
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        lists = "\n".join(f"{item.get('text', '')}\n{html_to_text(item.get('content', ''))}" for item in j.get("lists") or [])
        description = clean_text("\n".join([j.get("descriptionPlain") or "", lists, j.get("additionalPlain") or ""]))
        jobs.append(_job("lever", slug, j["id"], slug, j["text"], cats.get("allLocations") or [cats.get("location") or ""],
                         j.get("workplaceType") or "", j["hostedUrl"], description))
    return jobs


def parse_ashby(data: dict, slug: str) -> list[Job]:
    # `isRemote` is true for Hybrid jobs too, so remote comes from workplaceType / location text instead.
    return [_job("ashby", slug, j["id"], slug, j["title"],
                 [j.get("location") or ""] + [s.get("location") or "" for s in j.get("secondaryLocations") or []],
                 j.get("workplaceType") or "", j["jobUrl"], clean_text(j.get("descriptionPlain") or ""))
            for j in data["jobs"] if j.get("isListed", True)]


def parse_workday(data: dict, slug: str) -> list[Job]:
    """One page of a Workday job list; `slug` is "<tenant>.<wdN>/<site>". The full description is on each job's page."""
    host, site = slug.split("/", 1)
    jobs = []
    for j in data["jobPostings"]:
        if not j.get("externalPath"):  # rare entries without a job page (seen deep in NVIDIA's list); nothing to link to
            continue
        path = j["externalPath"]  # /job/<location>/<title>_<requisition id>
        url = f"https://{host}.myworkdayjobs.com/{site}{path}"
        jobs.append(_job("workday", slug, path.rsplit("_", 1)[-1], host.split(".")[0], j["title"],
                         [j.get("locationsText") or ""], "", url, "", detail_url=url))
    return jobs


def parse_workable(data: dict, slug: str) -> list[Job]:
    jobs = []
    for j in data["jobs"]:
        places = [", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
                  for loc in j.get("locations") or [] if not loc.get("hidden")]
        places = places or [", ".join(p for p in (j.get("city"), j.get("state"), j.get("country")) if p)]
        jobs.append(_job("workable", slug, j["shortcode"], data.get("name") or slug, j["title"], places,
                         "remote" if j.get("telecommuting") else "", j["url"], html_to_text(j.get("description") or "")))
    return jobs


def parse_amazon(data: dict, slug: str) -> list[Job]:
    """Amazon search results. Every query maps to the slug "amazon", so a job found by two queries is stored once."""
    jobs = []
    for j in data["jobs"]:
        text = "\n\n".join([j.get("description") or "", "Basic qualifications:<br/>" + (j.get("basic_qualifications") or ""),
                            "Preferred qualifications:<br/>" + (j.get("preferred_qualifications") or "")])
        jobs.append(_job("amazon", "amazon", j["id_icims"], "Amazon", j["title"],
                         [j.get("normalized_location") or j.get("location") or ""], "",
                         "https://www.amazon.jobs" + j["job_path"], html_to_text(text)))
    return jobs


# Every fetch function takes `known`: the keys of jobs already stored. Paged sources (Workday, Amazon) list newest
# first, so they stop after a page with nothing new; `known=None` asks for a full scan.

def fetch_greenhouse(polite: Polite, slug: str, known: set[str] | None = None) -> list[Job]:
    # Without content the list is ~12x smaller; descriptions come later, only for new jobs that pass the filters.
    return parse_greenhouse(polite.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json(), slug)


def fetch_lever(polite: Polite, slug: str, known: set[str] | None = None) -> list[Job]:
    return parse_lever(polite.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}).json(), slug)


def fetch_ashby(polite: Polite, slug: str, known: set[str] | None = None) -> list[Job]:
    return parse_ashby(polite.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json(), slug)


def nothing_new(page: list[Job], known: set[str] | None) -> bool:
    return known is not None and all(job.key in known for job in page)


WORKDAY_PAGE = 20  # the list API's page size (seen in its responses)


def fetch_workday(polite: Polite, slug: str, known: set[str] | None = None) -> list[Job]:
    host, site = slug.split("/", 1)
    url = f"https://{host}.myworkdayjobs.com/wday/cxs/{host.split('.')[0]}/{site}/jobs"
    jobs, total, offset = [], None, 0
    while total is None or offset < total:  # pages past the end wrap around to the first page, so stop at `total`
        data = polite.request("POST", url, json={"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset,
                                                 "searchText": ""}).json()
        total = data["total"] if total is None else total  # only the first page reports the total
        page = parse_workday(data, slug)
        jobs += page
        if not page or nothing_new(page, known):
            break
        offset += WORKDAY_PAGE
    return jobs


def fetch_workable(polite: Polite, slug: str, known: set[str] | None = None) -> list[Job]:
    data = polite.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"}).json()
    return parse_workable(data, slug)


AMAZON_MAX_RESULTS = 500  # newest first per query; new postings between two runs are far fewer


def fetch_amazon(polite: Polite, query: str, known: set[str] | None = None) -> list[Job]:
    jobs, offset = [], 0
    while offset < AMAZON_MAX_RESULTS:
        data = polite.get("https://www.amazon.jobs/en/search.json",
                          params={"base_query": query, "result_limit": 100, "offset": offset, "sort": "recent"}).json()
        page = parse_amazon(data, query)
        jobs += page
        offset += 100
        if len(page) < 100 or offset >= data.get("hits", 0) or nothing_new(page, known):
            break
    return jobs


# "ats:slug" in config.yaml -> fetch function. Workday slugs are "<tenant>.<wdN>/<site>"; Amazon slugs are a search query.
FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby, "workday": fetch_workday,
            "workable": fetch_workable, "amazon": fetch_amazon}


def describe_error(e: Exception) -> str:
    """One short line for a log message."""
    if isinstance(e, Blocked):
        return e.reason
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    return f"{type(e).__name__}: {e}".splitlines()[0][:200]


def fetch_company(polite: Polite, company: str, known: set[str] | None = None) -> list[Job]:
    ats, _, slug = company.partition(":")
    try:
        if ats not in FETCHERS:
            raise ValueError(f"unknown ATS {ats!r}")
        jobs = FETCHERS[ats](polite, slug, known)
    except Exception as e:  # one company failing must never crash the run
        log.warning("%s: skipped (%s)", company, describe_error(e))
        return []
    log.info("%s: %d jobs", company, len(jobs))
    return jobs


def fetch_all(companies: list[str], *, known: set[str] | None = None, full_scan: set[str] = frozenset(),
              max_workers: int = 5, timeout: float = 20.0, transport: httpx.BaseTransport | None = None) -> list[Job]:
    """Jobs from every board. With `known`, paged boards stop at a page with nothing new, except boards in
    `full_scan`, which are read to the end."""
    with httpx.Client(timeout=timeout, transport=transport, headers={"User-Agent": USER_AGENT}) as client, \
            ThreadPoolExecutor(max_workers) as pool:
        polite = Polite(client)
        return [job for jobs in pool.map(lambda c: fetch_company(polite, c, None if c in full_scan else known), companies)
                for job in jobs]
