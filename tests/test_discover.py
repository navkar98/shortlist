from datetime import datetime, timedelta, timezone

import httpx
import pytest

from shortlist import db
from shortlist.config import CareerPage, Config, Discovery
from shortlist.discover import (Page, discover_site, discover_sites, linked_boards, load_page, mentions_company,
                                name_guesses, recipe_problem, site_job_counts, site_sources)
from shortlist.fetchers import Job
from shortlist.polite import Polite
from shortlist.render import Renderer

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
LIST_PAGE = ("<html><body><main><h1>Open roles at Acme Robotics</h1>" + "<p>We build robots for everyone. </p>" * 20
             + '<ul class="openings">'
             + "".join(f'<li class="opening"><a class="role" href="/careers/role-{i}">{t}</a><span class="where">{w}</span></li>'
                       for i, (t, w) in enumerate([("Machine Learning Engineer, Ranking", "Boston"),
                                                   ("Research Scientist, Recommendations", "Remote"),
                                                   ("Senior Data Scientist, Search", "New York")]))
             + "</ul></main></body></html>")


def gh_board(company):
    return {"jobs": [{"id": 1, "title": "ML Engineer", "absolute_url": "https://x/1", "location": {"name": "NYC"},
                      "company_name": company}]}


def site_handler(pages: dict, boards: dict, calls: list | None = None):
    """A fake web: `pages` maps URL -> (status, html); `boards` maps Greenhouse slug -> company name."""
    def handler(request):
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "boards-api.greenhouse.io":
            slug = request.url.path.split("/")[3]
            return httpx.Response(200, json=gh_board(boards[slug])) if slug in boards else httpx.Response(404)
        if request.url.host in ("api.lever.co", "api.ashbyhq.com", "apply.workable.com"):
            return httpx.Response(404)
        status, html = pages.get(url.split("?")[0], (404, "Not Found"))
        return httpx.Response(status, text=html)
    return handler


def run_site(handler, site, ask=None, max_calls=8):
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client, Renderer() as renderer:
        return discover_site(site, Polite(client), renderer, ask or (lambda prompt: pytest.fail("no agent call")), max_calls)


SITE = CareerPage(name="Acme Robotics", url="https://acme.example/careers")


def test_linked_boards_and_name_guesses():
    text = ('<a href="https://boards.greenhouse.io/embed/job_board?for=acmeai">x</a> '
            "https://jobs.ashbyhq.com/Acme%20AI/123 https://jobs.lever.co/acme/abc https://apply.workable.com/acme-2/ "
            "https://acme.wd5.myworkdayjobs.com/en-US/Acme_Careers/job/1 https://acme.wd5.myworkdayjobs.com/wday/cxs/x")
    assert linked_boards(text) == ["greenhouse:acmeai", "lever:acme", "ashby:Acme AI", "workday:acme.wd5/Acme_Careers",
                                   "workable:acme-2"]
    assert linked_boards("https://ouryahoo.wd5.myworkdayjobs.com/careers") == ["workday:ouryahoo.wd5/careers"]
    guesses = name_guesses("Together AI")
    assert guesses[:4] == ["greenhouse:togetherai", "ashby:togetherai", "lever:togetherai", "workable:togetherai"]
    assert "greenhouse:together" in guesses and "greenhouse:together-ai" in guesses


def test_company_checks_for_guessed_boards():
    named = lambda company: [Job(ats="greenhouse", slug="acme", job_id="1", company=company, title="ML", location="",
                                 remote=False, url="https://x/1", description="")]
    assert mentions_company(named("Acme Robotics"), "Acme Robotics Inc")
    assert mentions_company(named("DeepMind"), "Google DeepMind")
    assert mentions_company(named("acme"), "Acme")  # name and slug can be the same word
    assert not mentions_company(named("Acme Insurance Brokers"), "Acme Robotics")
    slug_only = lambda desc: [Job(ats="lever", slug="acme", job_id="1", company="acme", title="ML", location="",
                                  remote=False, url="https://x/1", description=desc)]
    assert mentions_company(slug_only("Join Acme Robotics, where robots..."), "Acme Robotics")
    assert not mentions_company(slug_only("Join Acme Insurance today"), "Acme Robotics")
    assert not mentions_company(slug_only(""), "Acme Robotics")


def test_recipe_checks():
    job = lambda title, url="https://x/1": Job(ats="html", slug="a", job_id=url, company="a", title=title,
                                               location="", remote=False, url=url, description="")
    assert recipe_problem([]) == "it matched no jobs"
    assert "repeat" in recipe_problem([job("Engineer", f"https://x/{i}") for i in range(4)])
    assert "don't look like job titles" in recipe_problem([job("Apply"), job("Learn more", "https://x/2")])
    assert recipe_problem([job("ML Engineer"), job("Data Scientist", "https://x/2")]) == ""


def test_board_linked_from_the_careers_page_needs_no_agent():
    page = '<a href="https://job-boards.greenhouse.io/acmerobo">See open roles</a>' + "<p>text</p>" * 200
    outcome = run_site(site_handler({SITE.url: (200, page)}, {"acmerobo": "Totally Different Name"}), SITE)
    assert (outcome.status, outcome.board, outcome.agent_calls) == ("board", "greenhouse:acmerobo", 0)


def test_board_guessed_from_the_name_must_mention_the_company():
    page = "<p>Careers at Acme. We are hiring.</p>" * 50
    found = run_site(site_handler({SITE.url: (200, page)}, {"acme": "Acme Robotics"}), SITE)
    assert (found.status, found.board, found.agent_calls) == ("board", "greenhouse:acme", 0)
    asked = []
    wrong = run_site(site_handler({SITE.url: (200, page)}, {"acme": "Acme Insurance Brokers"}), SITE,
                     ask=lambda prompt: asked.append(prompt) or {"action": "give_up", "reason": "no list"})
    assert wrong.status == "unsupported" and len(asked) == 1  # the other company's board was not accepted


def test_agent_recipe_is_checked_and_feedback_is_given():
    prompts = []
    answers = iter([{"action": "recipe", "jobs": "li.nope"},
                    {"action": "recipe", "jobs": "li.opening", "location": ".where", "reason": "list of openings"}])
    outcome = run_site(site_handler({SITE.url: (200, LIST_PAGE)}, {}), SITE,
                       ask=lambda prompt: prompts.append(prompt) or next(answers))
    assert (outcome.status, outcome.agent_calls) == ("recipe", 2)
    assert (outcome.recipe.jobs, outcome.recipe.location, outcome.recipe.url) == ("li.opening", ".where", SITE.url)
    assert "Company: Acme Robotics" in prompts[0] and 'class="opening"' in prompts[0]
    assert "rejected: it matched no jobs" in prompts[1]


def test_agent_opens_a_link_and_the_board_there_is_found_without_another_call():
    pages = {SITE.url: (200, '<a href="/careers/jobs">See open roles</a>' + "<p>About us.</p>" * 100),
             "https://acme.example/careers/jobs": (200, '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acmejobs">')}
    answers = iter([{"action": "open", "url": "/careers/jobs"}])
    outcome = run_site(site_handler(pages, {"acmejobs": "Acme"}), SITE, ask=lambda prompt: next(answers))
    assert (outcome.status, outcome.board, outcome.agent_calls) == ("board", "greenhouse:acmejobs", 1)


def test_blocked_page_and_budget_and_backend_failure():
    blocked = run_site(site_handler({SITE.url: (403, "Access denied")}, {}), SITE)
    assert (blocked.status, blocked.agent_calls) == ("unsupported", 0) and "HTTP 403" in blocked.note
    no_budget = run_site(site_handler({SITE.url: (200, LIST_PAGE)}, {}), SITE, max_calls=0)
    assert no_budget.status == "later"

    def broken(prompt):
        raise TimeoutError("claude timed out")

    failed = run_site(site_handler({SITE.url: (200, LIST_PAGE)}, {}), SITE, ask=broken)
    assert failed.status == "later" and "agent call failed" in failed.note
    capped = run_site(site_handler({SITE.url: (200, LIST_PAGE)}, {}), SITE, max_calls=3,
                      ask=lambda prompt: {"action": "recipe", "jobs": "li.nope"})
    assert (capped.status, capped.agent_calls) == ("unsupported", 3)


def test_bot_challenge_is_reported_not_rendered(monkeypatch):
    from shortlist import render

    monkeypatch.setattr(render.Renderer, "html", lambda self, url, wait_for="": pytest.fail("must not render"))
    with httpx.Client(transport=httpx.MockTransport(site_handler({SITE.url: (202, "")}, {}))) as client, \
            Renderer() as renderer:
        page = load_page(SITE.url, Polite(client), renderer)
    assert page.problem.startswith("HTTP 202 with an empty page")


def test_sites_are_stored_reused_and_rediscovered_when_they_go_empty(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    cfg = Config(companies=[], career_pages=[SITE], discovery=Discovery(rediscover_after_empty_runs=2))
    asked = []
    ask = lambda prompt: asked.append(prompt) or {"action": "recipe", "jobs": "li.opening"}
    transport = httpx.MockTransport(site_handler({SITE.url: (200, LIST_PAGE)}, {}))

    [row] = discover_sites(cfg, conn, NOW, ask=ask, transport=transport)
    assert row["status"] == "recipe" and row["agent_calls"] == 1 and len(asked) == 1
    companies, [recipe] = site_sources([row])
    assert companies == [] and recipe.jobs == "li.opening" and recipe.name == "Acme Robotics"

    discover_sites(cfg, conn, NOW + timedelta(hours=12), ask=ask, transport=transport)
    assert len(asked) == 1  # stored: no agent calls

    for _ in range(2):
        db.record_site_results(conn, {SITE.url: 0}, NOW.isoformat())
    discover_sites(cfg, conn, NOW + timedelta(days=1), ask=ask, transport=transport)
    assert len(asked) == 1  # empty twice, but rediscovery waits a week after the last discovery
    discover_sites(cfg, conn, NOW + timedelta(days=8), ask=ask, transport=transport)
    assert len(asked) == 2


def test_site_job_counts():
    rows = [{"url": "a", "status": "board", "board": "greenhouse:acme", "name": "Acme"},
            {"url": "b", "status": "recipe", "board": None, "name": "Globex Corp"}]
    job = lambda ats, slug: Job(ats=ats, slug=slug, job_id=slug, company="c", title="t", location="", remote=False,
                                url="https://x", description="")
    counts = site_job_counts(rows, [job("greenhouse", "acme"), job("html", "globex-corp"), job("html", "globex-corp")])
    assert counts == {"a": 1, "b": 2}
