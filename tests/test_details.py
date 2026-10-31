import logging
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from shortlist import details
from shortlist.details import canonical_identity, fetch_details, greenhouse_job_id
from shortlist.fetchers import Job

FIXTURES = Path(__file__).parent / "fixtures"
read = lambda name: (FIXTURES / name).read_text(encoding="utf-8")


def test_canonical_identity_and_gh_jid():
    assert canonical_identity("https://job-boards.greenhouse.io/newsbreak/jobs/4694899006") == \
        ("greenhouse", "newsbreak", "4694899006")
    assert canonical_identity("https://job-boards.eu.greenhouse.io/imc/jobs/4907416101") == ("greenhouse", "imc", "4907416101")
    assert canonical_identity("https://jobs.lever.co/weloglobal/0157c8d2-ccef-4760-89c2-cd874506955f/apply") == \
        ("lever", "weloglobal", "0157c8d2-ccef-4760-89c2-cd874506955f")
    assert canonical_identity("https://jobs.ashbyhq.com/generalist/fc7c7b49-248a-4849-a473-a0bd246e5486/application") == \
        ("ashby", "generalist", "fc7c7b49-248a-4849-a473-a0bd246e5486")
    assert canonical_identity("https://boards.greenhouse.io/embed/job_app?token=8052313") is None  # no board slug
    assert canonical_identity("https://lifeattiktok.com/search/7665797203155896581") is None
    assert greenhouse_job_id("https://www.optiver.com/join-us/jobs/8451763002/?gh_jid=8451763002") == "8451763002"
    assert greenhouse_job_id("https://lifeattiktok.com/search/1") is None


def test_jobposting_data_and_page_text_extraction():
    workday = details._jsonld_description(BeautifulSoup(read("detail_page_workday_jsonld.html"), "html.parser"))
    assert len(workday) > 1000 and "<p" not in workday and "&lt;" not in workday
    tiktok = details._page_text(BeautifulSoup(read("detail_page_tiktok_text.html"), "html.parser"))
    assert "Machine Learning Engineer Graduate" in tiktok[:300] and "Responsibilities" in tiktok
    for menu_item in ("#LifeAtTikTok", "Hear From Our Leaders", "Our Philosophy", "Interview tips"):
        assert menu_item not in tiktok  # the site menu is dropped as a link-heavy block


def test_streamed_page_keeps_its_hidden_content_and_drops_the_site_around_it():
    # A real ByteDance page: React streams the posting inside <div hidden id="S:0"> and moves it into place later.
    text = details._page_text(BeautifulSoup(read("detail_page_bytedance_streamed.html"), "html.parser"))
    lines = text.splitlines()
    assert lines[0].endswith("2027 Start (PhD)") and lines[1] == "Responsibilities"  # the page's title, then content
    assert "Minimum Qualification(s):" in text and "Reasonable Accommodation" in text
    for chrome in ("Privacy Policy", "Interview Tips", "© 2012", "Join us as we work together to"):
        assert chrome not in text


PAGE = """<html><body>
<nav><a href="/">Home</a><a href="/jobs">Jobs</a><a href="/blog">Blog</a></nav>
<div class="cookie-consent">We use cookies to improve your experience on our site. Accept all cookies or manage them.</div>
<div class="mobile-menu" style="display: none"><a href="/a">Teams</a> <a href="/b">Locations</a></div>
<section class="hero"><p>Join us as we build the future of everything, together with millions of people.</p></section>
<article>
  <header><h1>Machine Learning Engineer, Ranking</h1>
    <span>Location</span><span>:</span> <span>San Jose</span></header>
  <h2>About the role</h2>
  <p>You will build ranking models for search and feeds, working with product and infrastructure teams.&#8203;</p>
  <div class="description">Requirements:
  - PhD in computer science or a related field, or equivalent research experience
  - Experience with PyTorch and large-scale training of recommendation or ranking models
  - Publications at venues such as RecSys, KDD, WWW, or SIGIR are a plus but not required
  - Strong software engineering in Python, and comfort reading production C++ code
  - Clear written communication with partners across research, product, and infrastructure</div>
  <p>We offer a competitive salary, equity, and benefits, and we sponsor visas for this role.</p>
  <div class="share"><span>Share this job</span> <a href="/x">LinkedIn</a></div>
</article>
<footer><p>© 2026 Acme Inc. All rights reserved. Privacy policy and terms of service apply to this website.</p></footer>
</body></html>"""


def test_page_text_is_the_posting_laid_out_as_a_browser_shows_it():
    text = details._page_text(BeautifulSoup(PAGE, "html.parser"))
    assert text.splitlines() == [
        "Machine Learning Engineer, Ranking",
        "Location: San Jose",  # inline pieces stay on one line; the article's own header is kept
        "About the role",
        "You will build ranking models for search and feeds, working with product and infrastructure teams.",
        "Requirements:",  # line breaks inside text are kept: plain-text postings are shown as written
        "- PhD in computer science or a related field, or equivalent research experience",
        "- Experience with PyTorch and large-scale training of recommendation or ranking models",
        "- Publications at venues such as RecSys, KDD, WWW, or SIGIR are a plus but not required",
        "- Strong software engineering in Python, and comfort reading production C++ code",
        "- Clear written communication with partners across research, product, and infrastructure",
        "We offer a competitive salary, equity, and benefits, and we sponsor visas for this role.",
    ]  # no navigation, cookie notice, hidden menu, banner, share links, or footer; no zero-width characters


def test_page_level_landmarks_go_unless_they_hold_the_posting():
    wrapped = "<body><header><main><p>" + "Build and evaluate ranking models for our feed. " * 5 + "</p></main></header></body>"
    assert "Build and evaluate ranking models" in details._page_text(BeautifulSoup(wrapped, "html.parser"))
    menu_only = "<body><div role='navigation'>" + "<p>A long list of every team we have and more.</p>" * 3 + "</div></body>"
    assert details._page_text(BeautifulSoup(menu_only, "html.parser")) == ""


def test_greenhouse_board_named_by_a_careers_page():
    assert details.greenhouse_board('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeai">') == "acmeai"
    assert details.greenhouse_board('<iframe src="https://boards.greenhouse.io/embed/job_app?for=acme&token=5">') == "acme"
    assert details.greenhouse_board('fetch("https://boards-api.greenhouse.io/v1/boards/acme-labs/jobs/")') == "acme-labs"
    assert details.greenhouse_board("https://job-boards.eu.greenhouse.io/acme/jobs/5") == "acme"
    assert details.greenhouse_board("<p>no board here</p>") is None


def test_embedded_greenhouse_posting_is_read_from_the_board_api():
    page = '<html><body><h1>Careers</h1><p>We are hiring across the company. ' * 3 + \
           '</p><script src="https://boards.greenhouse.io/embed/job_board/js?for=headlandstechnologies"></script></body></html>'
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, text=read("detail_greenhouse_headlands.json"), headers={"content-type": "application/json"})
        return httpx.Response(200, text=page)

    job = listed("x", "https://careers.example.com/open-roles?gh_jid=4444104004")
    [filled] = fetch_details([job], transport=httpx.MockTransport(handler))
    assert "/v1/boards/headlandstechnologies/jobs/4444104004" in seen
    assert len(filled.description) > 500 and "We are hiring across the company" not in filled.description


def listed(job_id, url):
    return Job(ats="simplify", slug="acme", job_id=job_id, company="Acme", title="ML Engineer", location="NYC",
               remote=False, url=url, description=f"[Listed on test] link: {url}", detail_url=url)


def test_fetch_details_by_api_jobposting_robots_and_fallbacks(caplog):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "boards-api.greenhouse.io" and path == "/v1/boards/headlandstechnologiesllc/jobs/4227566009":
            return httpx.Response(200, text=read("detail_greenhouse_headlands.json"), headers={"content-type": "application/json"})
        if host == "api.lever.co" and path == "/v0/postings/weloglobal/0157c8d2-ccef-4760-89c2-cd874506955f":
            return httpx.Response(200, text=read("detail_lever_weloglobal.json"), headers={"content-type": "application/json"})
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /blocked\n") if host == "jobs.example.com" \
                else httpx.Response(404)
        if host == "acme.wd1.myworkdayjobs.com":
            return httpx.Response(200, text=read("detail_page_workday_jsonld.html"))
        if path == "/expired/1":
            return httpx.Response(200, text="<html><body><p>This job has expired</p></body></html>")
        raise AssertionError(f"unexpected request {request.url}")

    jobs = [listed("gh", "https://job-boards.greenhouse.io/headlandstechnologiesllc/jobs/4227566009"),
            listed("lv", "https://jobs.lever.co/weloglobal/0157c8d2-ccef-4760-89c2-cd874506955f/apply"),
            listed("wd", "https://acme.wd1.myworkdayjobs.com/External/job/NYC/Researcher_R1"),
            listed("blocked", "https://jobs.example.com/blocked/1"),
            listed("expired", "https://jobs.example.com/expired/1"),
            Job(ats="lever", slug="rover", job_id="direct", company="rover", title="MLE", location="Seattle",
                remote=False, url="https://jobs.lever.co/rover/direct", description="Full board description")]
    caplog.set_level(logging.INFO, logger="shortlist.details")
    out = {j.job_id: j for j in fetch_details(jobs, transport=httpx.MockTransport(handler))}

    for key in ("gh", "lv", "wd"):  # full text first, listing note kept at the end, no further fetching needed
        assert len(out[key].description) > 500 and out[key].description.endswith(f"link: {out[key].url}")
        assert out[key].detail_url == ""
    assert "Quantitative Researcher" in out["gh"].description
    for key in ("blocked", "expired"):  # left as the listing note; still marked as having a fetchable page
        assert out[key].description == jobs[[j.job_id for j in jobs].index(key)].description and out[key].detail_url
    assert out["direct"] == jobs[-1]
    assert "https://jobs.example.com/blocked/1" not in requests
    assert requests.count("https://jobs.example.com/robots.txt") == 1
    assert "descriptions for 5 jobs listed without one:" in caplog.text and "2 board API" in caplog.text
    assert "1 JobPosting data" in caplog.text and "1 blocked by robots.txt" in caplog.text
    assert "1 no readable text" in caplog.text


def test_unreachable_robots_means_no_page_fetch():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(503)
        raise AssertionError("the page must not be fetched when robots.txt is unreachable")

    [job] = fetch_details([listed("x", "https://down.example.com/job/1")], transport=httpx.MockTransport(handler))
    assert job.detail_url and job.description.startswith("[Listed on test]")


@pytest.mark.parametrize("status", [401, 403, 404])
def test_robots_4xx_means_no_rules(status):
    """RFC 9309 2.3.1.3: a 4xx for robots.txt means the crawler may access any resource."""
    page = "<html><body><main>" + "<p>Build ranking models for search.</p>" * 20 + "</main></body></html>"

    def handler(request):
        return httpx.Response(status) if request.url.path == "/robots.txt" else httpx.Response(200, text=page)

    [job] = fetch_details([listed("x", "https://open.example.com/job/1")], transport=httpx.MockTransport(handler))
    assert job.description.startswith("Build ranking models") and not job.detail_url


def test_javascript_only_page_is_rendered_but_a_403_is_not(monkeypatch):
    from shortlist import render

    rendered = []
    jsonld = read("detail_page_workday_jsonld.html")
    monkeypatch.setattr(render.Renderer, "html", lambda self, url, wait_for="": rendered.append(url) or jsonld)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/blocked/1":
            return httpx.Response(403, text="Access denied")
        if request.url.path == "/challenged/1":
            return httpx.Response(202, text="")  # a bot challenge with an empty body
        return httpx.Response(200, text="<html><body><div id='app'></div></body></html>")  # filled in by JavaScript

    js_only = Job(ats="workday", slug="acme.wd1/External", job_id="R1", company="acme", title="Researcher",
                  location="2 Locations", remote=False, url="https://spa.example.com/job/1", description="",
                  detail_url="https://spa.example.com/job/1")
    refused, challenged = listed("x", "https://spa.example.com/blocked/1"), listed("y", "https://spa.example.com/challenged/1")
    out = {j.job_id: j for j in fetch_details([js_only, refused, challenged], transport=httpx.MockTransport(handler))}
    assert rendered == ["https://spa.example.com/job/1"]  # bot protection (403, 202 challenge) is never worked around
    assert len(out["R1"].description) > 1000 and out["R1"].location not in ("", "2 Locations")
    assert out["x"].description == refused.description and out["y"].description == challenged.description


def test_workday_job_without_description_gets_its_page_data():
    url = "https://acme.wd1.myworkdayjobs.com/External/job/NYC/Researcher_R1"
    job = Job(ats="workday", slug="acme.wd1/External", job_id="R1", company="acme", title="Researcher",
              location="2 Locations", remote=False, url=url, description="", detail_url=url)
    handler = lambda request: httpx.Response(404) if request.url.path == "/robots.txt" else \
        httpx.Response(200, text=read("detail_page_workday_jsonld.html"))
    [filled] = fetch_details([job], transport=httpx.MockTransport(handler))
    assert len(filled.description) > 1000 and not filled.description.endswith("\n") and not filled.detail_url
