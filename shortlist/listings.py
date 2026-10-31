"""Jobs from listings.json files kept on GitHub (SimplifyJobs format: a JSON list of job entries, no descriptions).

Fields used, as seen in SimplifyJobs/New-Grad-Positions: id, company_name, title, locations, url, active, is_visible,
category, degrees, sponsorship, date_posted (Unix seconds).
"""
import logging
import re
import time

import httpx

from .config import Listings
from .details import canonical_identity, greenhouse_job_id
from .fetchers import Job, describe_error
from .polite import USER_AGENT, Polite

log = logging.getLogger(__name__)


def parse_listings(entries: list, source: Listings, now: float) -> list[Job]:
    jobs = []
    for x in entries:
        if not x.get("active") or not x.get("is_visible", True):
            continue
        if (source.keep_categories or source.keep_degrees) and x.get("category") not in source.keep_categories \
                and not set(x.get("degrees") or []) & set(source.keep_degrees):
            continue
        if x.get("sponsorship") in source.drop_sponsorship:
            continue
        if source.max_age_days is not None and now - x.get("date_posted", 0) > source.max_age_days * 86400:
            continue
        location = "; ".join(x.get("locations") or [])
        note = (f"[Listed on {source.name}] category: {x.get('category')}; degrees: "
                f"{', '.join(x.get('degrees') or []) or 'not stated'}; sponsorship: {x.get('sponsorship')}; link: {x['url']}")
        # A Greenhouse/Lever/Ashby link gets that board's identity, so it matches the same job fetched from the board.
        ats, slug, job_id = canonical_identity(x["url"]) or (
            "simplify", re.sub(r"[^a-z0-9]+", "-", x["company_name"].lower()).strip("-"), x["id"])
        jobs.append(Job(ats=ats, slug=slug, job_id=job_id, company=x["company_name"], title=x["title"].strip(),
                        location=location, remote="remote" in location.lower(), url=x["url"], description=note,
                        detail_url=x["url"]))
    return jobs


def fetch_listings(sources: list[Listings], *, skip_greenhouse_ids: set[str] = frozenset(), now: float | None = None,
                   transport: httpx.BaseTransport | None = None) -> list[Job]:
    """Listing jobs from every source; postings whose gh_jid we already fetched from a company board are left out."""
    now = time.time() if now is None else now
    jobs = []
    with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}, transport=transport) as client:
        polite = Polite(client)
        for source in sources:
            try:
                entries = polite.get(source.url).json()
                found = parse_listings(entries, source, now)
            except Exception as e:  # one listings file failing must never crash the run
                log.warning("%s: skipped (%s)", source.name, describe_error(e))
                continue
            kept = [j for j in found if greenhouse_job_id(j.url) not in skip_greenhouse_ids]
            log.info("%s: %d listings, %d match its filters (%d already fetched from a company board)",
                     source.name, len(entries), len(found), len(found) - len(kept))
            jobs += kept
    return jobs
