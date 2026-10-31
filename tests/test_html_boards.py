import json
import logging
import re
from pathlib import Path

import httpx

from shortlist import render
from shortlist.config import HtmlBoard
from shortlist.html_boards import parse_board_page, scrape_boards

FIXTURES = Path(__file__).parent / "fixtures"
read = lambda name: (FIXTURES / name).read_text(encoding="utf-8")

# Recipes written the way `discover` will write them, checked against the saved real pages.
SNAP = HtmlBoard(name="Snap", url="https://careers.snap.com/jobs", jobs="tbody tr", link='a[href*="/job?id="]',
                 title='a[href*="/job?id="]', location="td:last-child")
WAYMO = HtmlBoard(name="Waymo", url="https://careers.withwaymo.com/jobs/search?page=1", jobs=".job-search-results-card",
                  link=".job-search-results-card-title a", location=".job-component-location", next_page='a[rel="next"]')
META = HtmlBoard(name="Meta", url="https://www.metacareers.com/jobs", jobs='a[href*="/profile/job_details/"]', title="h3",
                 render=True, max_pages=1)


def test_table_recipe():
    html = read("html_snap_jobs.html")
    jobs, next_url = parse_board_page(html, SNAP.url, SNAP)
    assert len(jobs) == len(set(re.findall(r'href="(/job\?id=[^"]+)"', html))) == 11 and next_url is None
    first = jobs[0]
    assert (first.title, first.location) == ("Director, Corporate & Brand Communications", "Los Angeles")
    assert first.url == first.detail_url == "https://careers.snap.com/job?id=R0046250" and first.description == ""
    assert first.ats == "html" and first.slug == "snap" and len({j.job_id for j in jobs}) == len(jobs)


def test_card_recipe_and_next_page():
    jobs, next_url = parse_board_page(read("html_waymo_search_p1.html"), WAYMO.url, WAYMO)
    assert len(jobs) == 5 and next_url == "https://careers.withwaymo.com/jobs/search?page=2"
    assert jobs[0].title.startswith("2027 Summer Intern, MS/PhD, Machine Learning")
    assert jobs[0].location == "Mountain View, California" and jobs[0].url.startswith("https://careers.withwaymo.com/jobs/")


def test_scrape_follows_next_pages_until_there_is_none(caplog):
    last_page = ('<div class="job-search-results-card"><h3 class="job-search-results-card-title">'
                 '<a href="/jobs/research-scientist-perception">Research Scientist, Perception</a></h3>'
                 '<li class="job-component-location">San Francisco, California</li></div>')

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        page = request.url.params["page"]
        return httpx.Response(200, text=read("html_waymo_search_p1.html") if page == "1" else last_page)

    caplog.set_level(logging.INFO, logger="shortlist.html_boards")
    jobs = scrape_boards([WAYMO], transport=httpx.MockTransport(handler))
    assert len(jobs) == 6 and jobs[-1].title == "Research Scientist, Perception"
    assert "html:Waymo: 6 jobs" in caplog.text


def test_jobposting_list_without_selectors():
    postings = [{"@type": "JobPosting", "title": "ML Engineer, Ranking", "url": "/jobs/1",
                 "description": "<p>Build ranking models.</p>",
                 "jobLocation": {"@type": "Place", "address": {"addressLocality": "Berlin", "addressCountry": "DE"}}},
                {"@type": "JobPosting", "title": "Research Scientist", "description": "Fairness research.",
                 "jobLocationType": "TELECOMMUTE"}]
    page = ('<script type="application/ld+json">' + json.dumps({"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": i, "item": p} for i, p in enumerate(postings)]}) + "</script>")
    board = HtmlBoard(name="Acme AI", url="https://acme.ai/careers")
    jobs, _ = parse_board_page(page, board.url, board)
    assert [(j.title, j.location, j.description, j.detail_url) for j in jobs] == [
        ("ML Engineer, Ranking", "Berlin, DE", "Build ranking models.", ""),
        ("Research Scientist", "Remote", "Fairness research.", "")]
    assert jobs[0].url == "https://acme.ai/jobs/1" and jobs[1].url == board.url and jobs[0].job_id != jobs[1].job_id


def test_robots_blocks_a_board_unless_you_ignore_robots(caplog):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, text=read("html_snap_jobs.html"))

    caplog.set_level(logging.INFO, logger="shortlist.html_boards")
    assert scrape_boards([SNAP], transport=httpx.MockTransport(handler)) == []
    assert "html:Snap: skipped (blocked by robots.txt)" in caplog.text
    allowed = SNAP.model_copy(update={"ignore_robots": True})
    assert len(scrape_boards([allowed], transport=httpx.MockTransport(handler))) == 11


def test_bot_challenge_is_reported_not_counted_as_zero_jobs(caplog):
    def handler(request):  # what Waymo began serving after several requests: 202 with an empty body
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(202, text="")

    caplog.set_level(logging.INFO, logger="shortlist.html_boards")
    assert scrape_boards([WAYMO], transport=httpx.MockTransport(handler)) == []
    assert "html:Waymo: skipped (RuntimeError: HTTP 202 with an empty page, likely bot protection)" in caplog.text


def test_rendered_board(monkeypatch, caplog):
    rendered = []
    monkeypatch.setattr(render.Renderer, "html",
                        lambda self, url, wait_for="": rendered.append((url, wait_for)) or read("html_meta_rendered.html"))
    robots = lambda request: httpx.Response(404)
    jobs = scrape_boards([META], transport=httpx.MockTransport(robots))
    assert rendered == [(META.url, META.jobs)] and len(jobs) == 11  # waits for the job elements to appear
    assert jobs[0].title == "Critical Facility Engineer"
    assert jobs[0].url.startswith("https://www.metacareers.com/profile/job_details/")


def test_rendered_board_without_a_browser_is_skipped(caplog):
    caplog.set_level(logging.INFO, logger="shortlist.html_boards")
    assert scrape_boards([META], transport=httpx.MockTransport(lambda request: httpx.Response(404))) == []
    assert "html:Meta: skipped (RenderUnavailable: browser disabled in tests)" in caplog.text
