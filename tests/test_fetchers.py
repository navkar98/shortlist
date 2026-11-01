import json
import logging
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
import pytest

from shortlist.fetchers import (AMAZON_MAX_RESULTS, MAX_DESCRIPTION_CHARS, fetch_all, host_group, html_to_text,
                                parse_amazon, parse_ashby, parse_greenhouse, parse_lever, parse_workable, parse_workday)
from shortlist.polite import pace_key

FIXTURES = Path(__file__).parent / "fixtures"
PARSERS = {"greenhouse": parse_greenhouse, "lever": parse_lever, "ashby": parse_ashby, "workday": parse_workday,
           "workable": parse_workable, "amazon": parse_amazon}
SLUGS = {"workday_nvidia": "nvidia.wd5/NVIDIAExternalCareerSite", "workday_zillow": "zillow.wd5/Zillow_Group_External"}


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def raw_jobs(ats, data):
    return data if ats == "lever" else data["jobPostings"] if ats == "workday" else data["jobs"]


@pytest.mark.parametrize("name", sorted(p.stem for p in FIXTURES.glob("*.json") if p.stem.split("_")[0] in PARSERS))
def test_parse_fixture(name):
    ats = name.split("_")[0]
    slug = SLUGS.get(name, name.split("_", 1)[1])
    data = load(name)
    jobs = PARSERS[ats](data, slug)
    assert len(jobs) == len(raw_jobs(ats, data)) > 0
    assert len({j.job_id for j in jobs}) == len(jobs)
    for j in jobs:
        expected_slug = "amazon" if ats == "amazon" else slug  # every Amazon query shares one identity
        assert (j.ats, j.slug, j.key) == (ats, expected_slug, f"{ats}:{expected_slug}:{j.job_id}")
        assert j.job_id and j.title == j.title.strip() and j.title and j.company and j.location
        assert j.url.startswith("https://")
        if ats == "workday":  # the list has no descriptions; each job's page has one
            assert j.description == "" and j.detail_url == j.url
        else:
            assert 0 < len(j.description) <= MAX_DESCRIPTION_CHARS and not j.detail_url
        for leftover in ("&lt;", "&amp;", "<p", "<div", "<li", "<br", "\xa0"):
            assert leftover not in j.description


def test_workday_fields():
    data = load("workday_nvidia")
    raw, job = data["jobPostings"][0], parse_workday(data, SLUGS["workday_nvidia"])[0]
    assert job.url == "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite" + raw["externalPath"]
    assert job.job_id == raw["externalPath"].rsplit("_", 1)[-1] == raw["bulletFields"][0]
    assert (job.company, job.location) == ("nvidia", raw["locationsText"])


def test_workday_posting_without_a_job_page_is_skipped():
    data = load("workday_zillow")
    data["jobPostings"].append({"title": "Talent Community", "bulletFields": []})  # no externalPath
    assert len(parse_workday(data, SLUGS["workday_zillow"])) == len(data["jobPostings"]) - 1


def test_workable_fields():
    data = load("workable_huggingface")
    raw, job = data["jobs"][0], parse_workable(data, "huggingface")[0]
    assert (job.job_id, job.company, job.url) == (raw["shortcode"], data["name"], raw["url"])
    assert job.remote is raw["telecommuting"] and raw["locations"][0]["city"] in job.location
    assert "Hugging Face" in job.description


def test_amazon_fields_and_qualifications():
    data = load("amazon_applied-scientist-phd")
    raw, job = data["jobs"][0], parse_amazon(data, "applied scientist phd")[0]
    assert (job.job_id, job.company) == (raw["id_icims"], "Amazon")
    assert job.url == "https://www.amazon.jobs" + raw["job_path"]
    assert "Basic qualifications:" in job.description and "Preferred qualifications:" in job.description


def test_greenhouse_fields_and_remote():
    data = load("greenhouse_nextdoor")
    raw, job = data["jobs"][0], parse_greenhouse(data, "nextdoor")[0]
    assert (job.job_id, job.title, job.url, job.company) == (str(raw["id"]), raw["title"], raw["absolute_url"], "Nextdoor")
    assert job.location == raw["location"]["name"]
    assert "&lt;" in raw["content"] and "Nextdoor" in job.description
    by_location = {j.location: j.remote for j in parse_greenhouse(data, "nextdoor")}
    assert by_location["US Remote"] is True and by_location["New York, NY"] is False


def test_lever_remote_and_lists():
    data = load("lever_rover")
    jobs = {j.job_id: j for j in parse_lever(data, "rover")}
    assert all(jobs[r["id"]].remote for r in data if r["workplaceType"] == "remote")
    in_office = [r for r in data if r["workplaceType"] == "hybrid"
                 and "remote" not in " ".join(r["categories"]["allLocations"]).lower()]
    assert in_office and not any(jobs[r["id"]].remote for r in in_office)
    first = data[0]
    assert jobs[first["id"]].url == first["hostedUrl"] and jobs[first["id"]].company == "rover"
    assert first["lists"][0]["text"] in jobs[first["id"]].description


def test_ashby_remote_ignores_isremote_on_hybrid():
    data = load("ashby_pinecone")
    jobs = {j.job_id: j for j in parse_ashby(data, "pinecone")}
    for r in data["jobs"]:
        assert jobs[r["id"]].remote == (r["workplaceType"] == "Remote" or "remote" in r["location"].lower())
    assert any(r["workplaceType"] == "Hybrid" and r["isRemote"] and not jobs[r["id"]].remote for r in data["jobs"])


def test_ashby_secondary_locations_joined():
    data = load("ashby_quora")
    raw, job = data["jobs"][0], parse_ashby(data, "quora")[0]
    assert job.location.startswith(raw["location"])
    assert all(s["location"] in job.location for s in raw["secondaryLocations"])


def test_html_to_text_and_truncation():
    assert html_to_text("<p>Hello&nbsp;<b>world</b></p><ul><li>A &amp; B</li></ul>") == "Hello world\nA & B"
    job = {"id": "x", "title": " T ", "location": "Remote", "workplaceType": "Remote", "jobUrl": "https://x",
           "descriptionPlain": "a" * 20000, "isListed": True}
    [parsed] = parse_ashby({"jobs": [job, dict(job, id="y", isListed=False)]}, "acme")
    assert len(parsed.description) == MAX_DESCRIPTION_CHARS and parsed.title == "T"


def test_fetch_all_skips_failing_companies(caplog):
    def handler(request):
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)  # no robots.txt: everything allowed (RFC 9309)
        if path == "/v1/boards/nextdoor/jobs":
            assert "content" not in request.url.params  # descriptions come later, for new jobs only
            return httpx.Response(200, json=load("greenhouse_nextdoor"))
        if path == "/v0/postings/rover":
            assert request.url.params["mode"] == "json"
            return httpx.Response(200, json=load("lever_rover"))
        if path == "/v0/postings/missing":
            return httpx.Response(404, json={"ok": False, "error": "Document not found"})
        if path == "/posting-api/job-board/slow":
            raise httpx.ReadTimeout("timed out", request=request)
        if path == "/posting-api/job-board/garbage":
            return httpx.Response(200, text="<html>not json</html>")
        if path == "/posting-api/job-board/wrongshape":
            return httpx.Response(200, json={"error": "nope"})
        raise AssertionError(f"unexpected request {request.url}")

    caplog.set_level(logging.INFO, logger="shortlist.fetchers")
    companies = ["greenhouse:nextdoor", "lever:rover", "lever:missing", "ashby:slow", "ashby:garbage",
                 "ashby:wrongshape", "smartrecruiters:acme"]
    jobs = fetch_all(companies, transport=httpx.MockTransport(handler))

    assert {j.slug for j in jobs} == {"nextdoor", "rover"}
    assert len(jobs) == len(load("greenhouse_nextdoor")["jobs"]) + len(load("lever_rover"))
    messages = [r.getMessage() for r in caplog.records]
    assert f"greenhouse:nextdoor: {len(load('greenhouse_nextdoor')['jobs'])} jobs" in messages
    assert "lever:missing: skipped (HTTP 404)" in messages
    assert "ashby:slow: skipped (timeout)" in messages
    assert any(m.startswith("ashby:garbage: skipped (JSONDecodeError") for m in messages)
    assert "ashby:wrongshape: skipped (KeyError: 'jobs')" in messages
    assert "smartrecruiters:acme: skipped (ValueError: unknown ATS 'smartrecruiters')" in messages


def test_fetch_all_keeps_the_order_and_reads_each_host_one_board_at_a_time():
    active, most = Counter(), Counter()
    lock = threading.Lock()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        host = request.url.host
        with lock:
            active[host] += 1
            most[host] = max(most[host], active[host])
        time.sleep(0.02)
        with lock:
            active[host] -= 1
        slug = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"jobs": [{"id": slug, "title": f"ML Engineer {slug}", "location": {"name": ""},
                                                   "absolute_url": f"https://x.example/{slug}"}]})

    companies = ["greenhouse:a", "greenhouse:b", "greenhouse:c"]
    jobs = fetch_all(companies, transport=httpx.MockTransport(handler), max_workers=8)
    assert [j.slug for j in jobs] == ["a", "b", "c"]
    assert most["boards-api.greenhouse.io"] == 1  # the shared host never sees two requests at once
    assert host_group("workday:acme.wd5/External") == host_group("workday:other.wd5/Careers") == "workday:wd5"
    assert host_group("workday:acme.wd1/External") != host_group("workday:acme.wd5/External")


def test_workday_tenants_share_one_pace_per_data_center():
    assert pace_key("acme.wd1.myworkdayjobs.com") == pace_key("other.wd1.myworkdayjobs.com") == "wd1.myworkdayjobs.com"
    assert pace_key("acme.wd5.myworkdayjobs.com") != pace_key("acme.wd1.myworkdayjobs.com")
    assert pace_key("boards-api.greenhouse.io") == "boards-api.greenhouse.io"


def posting(n):
    return {"title": f"ML Engineer {n}", "externalPath": f"/job/NYC/ML-Engineer_R{n}", "locationsText": "New York",
            "postedOn": "Posted Today", "bulletFields": [f"R{n}"]}


def test_without_a_network_boards_are_skipped_as_unreachable_not_disallowed(caplog):
    def handler(request):
        raise httpx.ConnectError("no network", request=request)

    caplog.set_level("INFO", logger="shortlist")
    assert fetch_all(["greenhouse:acme"], transport=httpx.MockTransport(handler)) == []
    assert "greenhouse:acme: skipped (robots.txt unreachable: ConnectError)" in caplog.text
    assert "blocked by robots.txt" not in caplog.text


def test_workday_pages_until_the_first_pages_total():
    offsets = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        body = json.loads(request.content)
        offsets.append(body["offset"])
        assert request.method == "POST" and request.url.path == "/wday/cxs/acme/External/jobs" and body["limit"] == 20
        count = {0: 20, 20: 20, 40: 5}[body["offset"]]
        total = 45 if body["offset"] == 0 else 0  # Workday reports the total on the first page only
        return httpx.Response(200, json={"total": total, "jobPostings": [posting(body["offset"] + i) for i in range(count)]})

    jobs = fetch_all(["workday:acme.wd1/External"], transport=httpx.MockTransport(handler))
    assert offsets == [0, 20, 40] and len(jobs) == 45 and jobs[0].url.startswith("https://acme.wd1.myworkdayjobs.com/External/job/")


def test_paged_boards_stop_at_a_page_with_nothing_new_unless_a_full_scan_is_due():
    offsets = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        offset = json.loads(request.content)["offset"]
        offsets.append(offset)
        return httpx.Response(200, json={"total": 100 if offset == 0 else 0,
                                         "jobPostings": [posting(offset + i) for i in range(20)]})

    known = {f"workday:acme.wd1/External:R{n}" for n in range(15, 100)}  # only R0-R14 are new
    jobs = fetch_all(["workday:acme.wd1/External"], known=known, transport=httpx.MockTransport(handler))
    assert offsets == [0, 20] and len(jobs) == 40  # page 2 had nothing new
    offsets.clear()
    fetch_all(["workday:acme.wd1/External"], known=known, full_scan={"workday:acme.wd1/External"},
              transport=httpx.MockTransport(handler))
    assert offsets == [0, 20, 40, 60, 80]


def test_greenhouse_list_without_content_points_details_at_the_api():
    data = {"jobs": [{"id": 42, "title": "ML Engineer", "absolute_url": "https://acme.com/jobs?gh_jid=42",
                      "location": {"name": "NYC"}, "company_name": "Acme"}]}
    [job] = parse_greenhouse(data, "acme")
    assert job.description == "" and job.detail_url == "https://job-boards.greenhouse.io/acme/jobs/42"
    assert job.url == "https://acme.com/jobs?gh_jid=42"


def test_amazon_pages_newest_first_and_stops_at_the_cap():
    offsets = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        params = request.url.params
        assert (params["base_query"], params["sort"], params["result_limit"]) == ("machine learning", "recent", "100")
        offsets.append(int(params["offset"]))
        jobs = [{"id_icims": f"{params['offset']}-{i}", "title": "Applied Scientist", "job_path": f"/en/jobs/{i}",
                 "normalized_location": "Seattle, Washington, USA", "description": "d"} for i in range(100)]
        return httpx.Response(200, json={"hits": 2177, "jobs": jobs})

    jobs = fetch_all(["amazon:machine learning"], transport=httpx.MockTransport(handler))
    assert len(jobs) == AMAZON_MAX_RESULTS and offsets == [0, 100, 200, 300, 400]


def test_robots_txt_disallow_skips_the_company(caplog):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        raise AssertionError("must not fetch a disallowed URL")

    caplog.set_level(logging.INFO, logger="shortlist.fetchers")
    assert fetch_all(["workable:acme"], transport=httpx.MockTransport(handler)) == []
    assert "workable:acme: skipped (blocked by robots.txt)" in caplog.text
