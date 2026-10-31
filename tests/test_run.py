import json
from pathlib import Path

import pytest

from shortlist import __main__ as cli
from shortlist import notify, scoring
from shortlist.notify import format_digest

cli.notify_format = format_digest  # the real formatter, for tests that shrink its message limit
from shortlist.config import Config, Filters, Scoring
from shortlist.fetchers import parse_greenhouse

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_nextdoor.json"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    jobs = parse_greenhouse(json.loads(FIXTURE.read_text(encoding="utf-8")), "nextdoor")
    # A second, identical posting of the ML role in another city must be scored only once.
    ml = next(j for j in jobs if j.title == "Machine Learning Engineer - Ads")
    jobs.append(ml.model_copy(update={"job_id": "999", "location": "London, UK"}))
    monkeypatch.setattr(cli, "fetch_all", lambda companies, **kwargs: jobs)

    scored_keys = []

    def backend(prompt, schema, _cfg):
        batch = json.loads(prompt.split("<jobs>\n", 1)[1].split("\n</jobs>", 1)[0])
        scored_keys.extend(j["job_id"] for j in batch)
        return {"results": [{"job_id": j["job_id"], "score": 88 if "Machine Learning" in j["title"] else 30,
                             "matched_skills": [], "missing_skills": [], "dealbreakers": [],
                             "one_line_reason": f"fits: {j['location']}"} for j in batch]}

    monkeypatch.setitem(scoring.BACKENDS, "claude_cli", backend)
    (tmp_path / "resume.md").write_text("RESUME", encoding="utf-8")
    cfg = Config(companies=["greenhouse:nextdoor"], db_path=str(tmp_path / "t.db"),
                 resume_path=str(tmp_path / "resume.md"), scoring=Scoring(workdir=str(tmp_path / "wd"), recheck_alone=False),
                 filters=Filters(title_include=["machine learning", "data scientist"]))
    return cfg, scored_keys


def test_dry_run_scores_new_roles_once_and_prints_digest(setup, capsys):
    cfg, scored_keys = setup
    assert cli.run(cfg, dry_run=True) == 0
    out = capsys.readouterr().out
    # 3 titles pass the filter; the duplicate ML posting is folded into its role instead of being scored again.
    assert len(scored_keys) == 3 and "greenhouse:nextdoor:999" not in scored_keys
    assert "Shortlist: 1 new match" in out
    assert "88 | Machine Learning Engineer - Ads | Nextdoor" in out and "; London, UK" in out

    # Second run: nothing new to score; the dry run did not mark anything notified, so the match is still pending.
    assert cli.run(cfg, dry_run=True) == 0
    assert len(scored_keys) == 3
    assert "Shortlist: 1 new match" in capsys.readouterr().out


def test_limit_caps_roles_scored(setup, capsys):
    cfg, scored_keys = setup
    cli.run(cfg, dry_run=True, limit=1)
    assert len(scored_keys) == 1


def send_by(monkeypatch, channel, sender):
    monkeypatch.setitem(notify.SENDERS, channel, sender)


@pytest.fixture
def telegram(setup, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    setup[0].notify = ["telegram"]
    sent = []
    send_by(monkeypatch, "telegram",
            lambda text, env: sent.append((text, env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])))
    return sent


def test_real_run_sends_once_then_second_run_scores_and_sends_nothing(setup, telegram):
    cfg, scored_keys = setup
    assert cli.run(cfg) == 0
    [(text, token, chat_id)] = telegram
    assert text.startswith("Shortlist: 1 new match") and (token, chat_id) == ("123:SECRET", "42")

    assert cli.run(cfg) == 0
    assert len(telegram) == 1 and len(scored_keys) == 3


def test_failed_send_keeps_matches_pending(setup, telegram, monkeypatch):
    cfg, _ = setup

    def broken(text, env):
        raise RuntimeError("Telegram sendMessage failed: HTTP 502")

    send_by(monkeypatch, "telegram", broken)
    assert cli.run(cfg) == 1
    send_by(monkeypatch, "telegram", lambda text, env: telegram.append(text))
    assert cli.run(cfg) == 0
    assert len(telegram) == 1 and telegram[0].startswith("Shortlist: 1 new match")


def test_split_digest_marks_each_sent_part_and_retries_the_rest(setup, telegram, monkeypatch):
    cfg, _ = setup
    monkeypatch.setattr(cli, "format_digest", lambda rows, limit=0: cli.notify_format(rows, limit=1))  # one per message
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    good_backend = scoring.BACKENDS["claude_cli"]

    def everything_matches(prompt, schema, cfg_):
        out = good_backend(prompt, schema, cfg_)
        return {"results": [dict(r, score=88) for r in out["results"]]}

    monkeypatch.setitem(scoring.BACKENDS, "claude_cli", everything_matches)
    calls = []

    def flaky(text, env):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("Telegram sendMessage failed: HTTP 502")

    send_by(monkeypatch, "telegram", flaky)
    assert cli.run(cfg) == 1  # part 1 sent, part 2 failed
    assert [c.split("\n")[0] for c in calls] == ["Shortlist: 3 new matches (1/3)", "Shortlist: 3 new matches (2/3)"]

    calls.clear()
    send_by(monkeypatch, "telegram", lambda text, env: calls.append(text))
    assert cli.run(cfg) == 0  # only the two unsent matches go out
    assert [c.split("\n")[0] for c in calls] == ["Shortlist: 2 new matches (1/2)", "Shortlist: 2 new matches (2/2)"]


def test_if_due_runs_once_then_waits_for_the_schedule(setup, telegram, caplog):
    cfg, scored_keys = setup
    caplog.set_level("INFO", logger="shortlist")
    assert cli.run(cfg, if_due=True) == 0 and len(telegram) == 1
    assert cli.run(cfg, if_due=True) == 0
    assert "not due: last run was 0.0h ago (schedule.min_hours_between_runs = 6)" in caplog.text
    assert len(scored_keys) == 3  # the skipped run did not fetch or score anything


def test_skip_reason_counts_only_clean_runs_that_fetched_jobs(tmp_path):
    from datetime import datetime, timedelta

    from shortlist import db
    from shortlist.config import Schedule

    conn = db.connect(str(tmp_path / "t.db"))
    noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    at = lambda hours_before: (noon - timedelta(hours=hours_before)).isoformat(timespec="seconds")
    schedule = Schedule(max_runs_per_day=2, min_hours_between_runs=6)
    assert cli.skip_reason(conn, schedule, noon) is None  # never ran

    db.record_run(conn, at(2), 0, 0)    # offline: fetched nothing
    db.record_run(conn, at(1), 500, 1)  # Telegram failed
    assert cli.skip_reason(conn, schedule, noon) is None

    db.record_run(conn, at(7), 500, 0)  # 05:00 today
    assert cli.skip_reason(conn, schedule, noon) is None  # 7h >= 6h
    assert "last run was 3.0h ago" in cli.skip_reason(conn, schedule, noon - timedelta(hours=4))

    db.record_run(conn, at(0.5), 500, 0)  # 11:30 today: second run of the day
    evening = noon + timedelta(hours=6)  # 18:00 the same day, 6.5h after the last run
    assert cli.skip_reason(conn, schedule, evening).startswith("already ran 2 time(s) today")
    assert cli.skip_reason(conn, Schedule(max_runs_per_day=3, min_hours_between_runs=6), evening) is None


def test_listing_jobs_join_the_pool_and_board_copies_win(setup, monkeypatch, capsys):
    from shortlist.fetchers import Job

    cfg, _ = setup
    board_ml = next(j for j in cli.fetch_all([]) if j.title == "Machine Learning Engineer - Ads")
    note = lambda url: f"[Listed on test] link: {url}"
    same_posting = board_ml.model_copy(update={"description": note(board_ml.url), "detail_url": board_ml.url})
    tiktok_url = "https://lifeattiktok.com/search/1"
    listing_only = Job(ats="simplify", slug="tiktok", job_id="t1", company="TikTok", title="Machine Learning Engineer Graduate",
                       location="San Jose", remote=False, url=tiktok_url, description=note(tiktok_url), detail_url=tiktok_url)
    monkeypatch.setattr(cli, "fetch_listings", lambda sources, skip_greenhouse_ids: [same_posting, listing_only])
    monkeypatch.setattr(cli, "fetch_details", lambda jobs: [
        j.model_copy(update={"description": "Full TikTok text\n\n" + j.description, "detail_url": ""}) if j.detail_url else j
        for j in jobs])
    seen = {}

    def backend(prompt, schema, _cfg):
        batch = json.loads(prompt.split("<jobs>\n", 1)[1].split("\n</jobs>", 1)[0])
        seen.update({j["job_id"]: j["description"] for j in batch})
        return {"results": [{"job_id": j["job_id"], "score": 50, "matched_skills": [], "missing_skills": [],
                             "dealbreakers": [], "one_line_reason": "ok"} for j in batch]}

    monkeypatch.setitem(scoring.BACKENDS, "claude_cli", backend)
    assert cli.run(cfg, dry_run=True) == 0
    assert seen["simplify:tiktok:t1"].startswith("Full TikTok text") and "[Listed on test]" in seen["simplify:tiktok:t1"]
    assert seen[board_ml.key] == board_ml.description  # the board's own copy, not the listing note


def test_discovered_sites_join_the_run_and_their_results_are_recorded(setup, monkeypatch):
    from shortlist import db
    from shortlist.config import CareerPage

    cfg, _ = setup
    site = CareerPage(name="Acme", url="https://acme.example/careers")
    cfg = cfg.model_copy(update={"career_pages": [site]})
    conn = db.connect(cfg.db_path)
    db.save_site(conn, site.url, site.name, "board", board="lever:acme", note="linked", now="2026-10-01T00:00:00+00:00",
                 retry_after="2026-10-08T00:00:00+00:00")
    asked = []
    monkeypatch.setattr(cli, "discover_sites", lambda cfg_, conn_, now: cli.__dict__["_real_discover"](
        cfg_, conn_, now, ask=lambda prompt: asked.append(prompt)))
    fetched = {}

    def fake_fetch_all(companies, **kwargs):
        fetched.update(companies=companies, **kwargs)
        return setup_jobs()

    monkeypatch.setattr(cli, "fetch_all", fake_fetch_all)
    assert cli.run(cfg, dry_run=True) == 0
    assert "lever:acme" in fetched["companies"] and "greenhouse:nextdoor" in fetched["companies"]
    assert not asked  # the stored site was reused, no discovery calls
    assert "lever:acme" in fetched["full_scan"]  # never fully scanned before
    assert db.sites(conn)[site.url]["empty_runs"] == 1  # the fake fetch returned no lever:acme jobs


cli._real_discover = cli.discover_sites


def test_done_keys_leave_out_candidates_still_waiting_to_be_scored(tmp_path):
    from shortlist import db
    from shortlist.fetchers import Job
    from shortlist.prefilter import build_filter

    conn = db.connect(str(tmp_path / "t.db"))
    job = lambda job_id, title: Job(ats="workday", slug="acme.wd1/Ext", job_id=job_id, company="acme", title=title,
                                    location="New York", remote=False, url=f"https://x/{job_id}", description="")
    waiting, rejected, scored = job("1", "ML Engineer"), job("2", "Account Executive"), job("3", "ML Scientist")
    db.upsert_jobs(conn, [waiting, rejected, scored], "t1")
    db.save_result(conn, scored, '{"one_line_reason": "r"}', 50, "t1")
    done = cli.done_keys(conn, build_filter(Filters(title_include=["ml"])))
    assert done == {rejected.key, scored.key}  # the waiting candidate keeps its board's pages coming


def test_full_scans_are_recorded_only_for_boards_that_returned_jobs():
    from shortlist.config import HtmlBoard
    from shortlist.fetchers import Job

    job = lambda ats, slug: Job(ats=ats, slug=slug, job_id="1", company="c", title="t", location="", remote=False,
                                url="https://x", description="")
    boards = [HtmlBoard(name="Acme Labs", url="https://acme/jobs"), HtmlBoard(name="Globex", url="https://globex/jobs")]
    found = cli.boards_with_jobs(["workday:a.wd1/X", "workday:b.wd1/Y", "amazon:machine learning"], boards,
                                 [job("workday", "a.wd1/X"), job("amazon", "amazon"), job("html", "acme-labs")])
    assert found == {"workday:a.wd1/X", "amazon:machine learning", "html:https://acme/jobs"}


def setup_jobs():
    return parse_greenhouse(json.loads(FIXTURE.read_text(encoding="utf-8")), "nextdoor")


def test_real_run_without_telegram_secrets_stops_before_fetching(setup, monkeypatch, caplog):
    cfg, scored_keys = setup
    cfg.notify = ["telegram"]
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(cli, "fetch_all", lambda companies, **kwargs: pytest.fail("must not fetch"))
    assert cli.run(cfg) == 2
    assert "TELEGRAM_BOT_TOKEN" in caplog.text and not scored_keys


def test_without_channels_a_real_run_needs_no_secrets_and_keeps_matches_for_the_web_app(setup, monkeypatch, caplog):
    cfg, scored_keys = setup
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    caplog.set_level("INFO", logger="shortlist")
    assert cfg.notify == [] and cli.run(cfg) == 0 and len(scored_keys) == 3
    assert "1 new matches; no notification channel is set" in caplog.text


def test_max_jobs_per_run_caps_scoring_like_limit(setup):
    cfg, scored_keys = setup
    cfg.scoring.max_jobs_per_run = 2
    cli.run(cfg, dry_run=True)
    assert len(scored_keys) == 2
    cli.run(cfg, dry_run=True)
    assert len(scored_keys) == 3  # the next run scores the rest


def test_rescore_scores_outdated_jobs_alone_with_the_current_prompt(setup, monkeypatch):
    cfg, scored_keys = setup
    cli.run(cfg, dry_run=True)
    conn = cli.db.connect(cfg.db_path)
    ml = "SELECT score, scored_with, description FROM jobs WHERE title = 'Machine Learning Engineer - Ads' AND job_id != '999'"
    first = conn.execute(ml).fetchone()
    assert first["score"] == 88 and first["scored_with"]
    scored_keys.clear()
    assert cli.rescore(cfg, from_score=None, everything=False, limit=None) == 0
    assert scored_keys == []  # nothing changed since: nothing to do

    prompts = []

    def stricter(prompt, schema, _cfg):
        prompts.append(prompt)
        batch = json.loads(prompt.split("<jobs>\n", 1)[1].split("\n</jobs>", 1)[0])
        return {"results": [{"job_id": j["job_id"], "score": 60, "matched_skills": [], "missing_skills": [],
                             "dealbreakers": [], "one_line_reason": "partial"} for j in batch]}

    monkeypatch.setitem(scoring.BACKENDS, "claude_cli", stricter)
    # A job scored before descriptions were kept: its description is read again, from the board's API.
    conn.execute("UPDATE jobs SET description = NULL WHERE title = 'Machine Learning Engineer - Ads'")
    conn.commit()
    asked = []
    monkeypatch.setattr(cli, "fetch_details", lambda jobs: [
        asked.append(j.detail_url) or j.model_copy(update={"description": "Read again.", "detail_url": ""})
        if j.detail_url else j for j in jobs])
    strict = cfg.model_copy(update={"scoring": cfg.scoring.model_copy(update={"instructions": "Be strict."})})
    assert cli.rescore(strict, from_score=None, everything=False, limit=None) == 0
    assert len(prompts) == 1 and prompts[0].startswith("Be strict.") and '"description": "Read again."' in prompts[0]
    assert asked == [f"https://job-boards.greenhouse.io/nextdoor/jobs/{asked[0].rsplit('/', 1)[1]}"]
    again = conn.execute(ml).fetchone()
    assert again["score"] == 60 and again["description"] == "Read again." and again["scored_with"] != first["scored_with"]
    assert cli.rescore(strict, from_score=None, everything=False, limit=None) == 0 and len(prompts) == 1
    assert cli.rescore(strict, from_score=50, everything=True, limit=None) == 0 and len(prompts) == 2  # --all
