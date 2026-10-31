from shortlist.config import Filters
from shortlist.fetchers import Job
from shortlist.prefilter import build_filter


def job(title="Machine Learning Engineer", location="New York, NY", remote=False):
    return Job(ats="ashby", slug="acme", job_id="1", company="acme", title=title, location=location, remote=remote,
               url="https://example.com", description="d")


def test_title_terms_match_whole_words():
    reject = build_filter(Filters(title_include=["ml", "research", "new grad"], title_exclude=["intern", "staff"]))
    assert reject(job("Senior AI/ML Engineer")) is None
    assert reject(job("Software Engineer, New Grad")) is None
    assert reject(job("HTML Developer")) == "title"
    assert reject(job("Researcher")) == "title"  # "research" does not match "researcher"
    assert reject(job("Research Scientist, International")) is None  # "intern" does not match "International"
    assert reject(job("ML Research Intern")) == "excluded title"
    assert reject(job("Staff ML Engineer")) == "excluded title"


def test_locations_and_remote():
    filters = Filters(locations=["united states", "us", "u.s.", "london"], allow_remote=True)
    reject = build_filter(filters)
    assert reject(job(location="Remote - US", remote=True)) is None
    assert reject(job(location="Bangalore; London, UK")) is None
    assert reject(job(location="Washington, U.S.")) is None
    assert reject(job(location="Houston")) == "location"  # "us" is a whole word, not a substring
    assert reject(job(location="Remote", remote=True)) is None
    assert reject(job(location="Remote - Multiple Locations", remote=True)) is None
    assert reject(job(location="Remote - India", remote=True)) == "location"
    assert reject(job(location="", remote=False)) is None  # no location given: let the scorer judge
    assert reject(job(location="3 Locations")) is None  # Workday's summary; the places are on the job's page

    no_remote = build_filter(filters.model_copy(update={"allow_remote": False}))
    assert no_remote(job(location="Remote", remote=True)) == "location"
    assert no_remote(job(location="Remote - US", remote=True)) is None


def test_empty_filters_pass_everything():
    assert build_filter(Filters())(job(title="Anything", location="Mars")) is None
