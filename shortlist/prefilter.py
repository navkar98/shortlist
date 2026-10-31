"""Cheap, no-LLM rules: title include/exclude keywords and allowed locations/remote.

Terms match case-insensitively as whole words or phrases, so "intern" does not match "International"
and "ml" matches "AI/ML" but not "HTML".
"""
import re
from collections.abc import Callable

from .config import Filters
from .fetchers import Job

# Location text that names no place at all, e.g. "Remote", "Remote - Multiple Locations", "Anywhere", and
# Workday's "2 Locations" (the places are only on the job's page, so the scorer judges those).
_GENERIC_LOCATION = re.compile(r"(?<!\w)(?:remote|anywhere|global|worldwide|distributed|multiple locations|"
                               r"various locations|flexible|\d+ locations)(?!\w)", re.I)


def _terms(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t.strip()) for t in terms) + r")(?!\w)", re.I)


def build_filter(filters: Filters) -> Callable[[Job], str | None]:
    """Return a function giving the reason a job is rejected ("title", "excluded title", "location"), or None."""
    include, exclude, places = _terms(filters.title_include), _terms(filters.title_exclude), _terms(filters.locations)

    def location_ok(job: Job) -> bool:
        if places is None or places.search(job.location):
            return True
        names_no_place = not re.search(r"[^\W\d_]", _GENERIC_LOCATION.sub("", job.location))
        return names_no_place and (filters.allow_remote or not job.remote)

    def rejection(job: Job) -> str | None:
        if include and not include.search(job.title):
            return "title"
        if exclude and exclude.search(job.title):
            return "excluded title"
        if not location_ok(job):
            return "location"
        return None

    return rejection


def first_match(terms: list[str], text: str) -> str:
    """The first of `terms` found in `text` (matched as the filters match), or ""."""
    pattern = _terms(terms)
    found = pattern.search(text) if pattern else None
    return found.group(0) if found else ""
