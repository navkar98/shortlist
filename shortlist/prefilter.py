"""Cheap, no-LLM rules: title include/exclude keywords and allowed locations/remote.

Terms match case-insensitively as whole words or phrases, so "intern" does not match "International"
and "ml" matches "AI/ML" but not "HTML".
"""
import re
from collections.abc import Callable
from functools import lru_cache

from .config import Filters
from .fetchers import Job

# Location text that names no place at all, e.g. "Remote", "Remote - Multiple Locations", "Anywhere", and
# Workday's "2 Locations" (the places are only on the job's page, so the scorer judges those).
_GENERIC_LOCATION = re.compile(r"(?<!\w)(?:remote|anywhere|global|worldwide|distributed|multiple locations|"
                               r"various locations|flexible|\d+ locations)(?!\w)", re.I)

# Many locations name a state or province but no country: "Boise, ID", "Bar Harbor, Maine", "Toronto, ON". Those count
# as naming the country too. Some codes after a comma are also country codes ("Bangalore, IN" is India, "Berlin, DE"
# Germany): they count as both, since the scorer reads the location anyway and a doubtful job is better kept than lost.
_US, _CANADA, _AUSTRALIA, _INDIA, _UK = "united states usa us u.s.", "canada", "australia", "india", "united kingdom uk"
_STATE_CODES = {
    **{code: _US for code in "AL AK AZ AR CO CT FL HI ID IL IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
                             "OH OK OR PA RI SC SD TN TX UT VT VA WV WI WY DC".split()},
    "CA": f"{_US} {_CANADA}", "IN": f"{_US} {_INDIA}", "DE": f"{_US} germany", "GA": _US, "WA": f"{_US} {_AUSTRALIA}",
    **{code: _CANADA for code in "AB BC MB NB NL NS ON PE QC SK YT NU".split()},
    "NT": f"{_CANADA} {_AUSTRALIA}",
    **{code: _AUSTRALIA for code in "NSW VIC QLD TAS ACT SA".split()},
}
_STATE_NAMES = {
    **{name: _US for name in (
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
        "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
        "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma",
        "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
        "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia")},
    **{name: _CANADA for name in (
        "alberta", "british columbia", "manitoba", "new brunswick", "newfoundland", "nova scotia", "ontario",
        "prince edward island", "quebec", "québec", "saskatchewan", "yukon", "northwest territories", "nunavut")},
    **{name: _AUSTRALIA for name in (
        "new south wales", "victoria", "queensland", "western australia", "south australia", "tasmania",
        "australian capital territory", "northern territory")},
    **{name: _INDIA for name in (
        "karnataka", "maharashtra", "telangana", "tamil nadu", "haryana", "uttar pradesh", "delhi", "kerala", "gujarat",
        "west bengal", "andhra pradesh", "rajasthan", "punjab", "madhya pradesh", "odisha", "chandigarh")},
    **{name: _UK for name in ("england", "scotland", "wales", "northern ireland")},
}
_STATE_CODE = re.compile(r",\s*([A-Z]{2,3})\b")  # upper case after a comma: "Austin, TX", "Sydney NSW" is not one
# Longest names first, so "New South Wales" is read whole rather than as "Wales".
_STATE_NAME = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(_STATE_NAMES, key=len, reverse=True)))
                         + r")(?!\w)", re.I)


def implied_countries(location: str) -> str:
    """The countries a location's states and provinces imply, as words the location filter can match."""
    found = {_STATE_CODES[code] for code in _STATE_CODE.findall(location) if code in _STATE_CODES}
    found |= {_STATE_NAMES[m.group(0).lower()] for m in _STATE_NAME.finditer(location)}
    return " ".join(sorted(found))


@lru_cache(maxsize=1024)
def _compiled(terms: tuple[str, ...]) -> re.Pattern | None:
    if not terms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t.strip()) for t in terms) + r")(?!\w)", re.I)


def _terms(terms: list[str]) -> re.Pattern | None:
    return _compiled(tuple(terms))


def build_filter(filters: Filters) -> Callable[[Job], str | None]:
    """Return a function giving the reason a job is rejected ("title", "excluded title", "location"), or None."""
    include, exclude, places = _terms(filters.title_include), _terms(filters.title_exclude), _terms(filters.locations)

    def location_ok(job: Job) -> bool:
        if places is None or places.search(job.location) or places.search(implied_countries(job.location)):
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
