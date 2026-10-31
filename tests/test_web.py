import json
import warnings
from pathlib import Path

import pytest
import yaml

with warnings.catch_warnings():  # starlette's TestClient warns about its httpx dependency; not ours to fix
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from shortlist import db, web
from shortlist.fetchers import Job

CONFIG = """# my settings
companies:
  - greenhouse:acme          # Acme Robotics
  - lever:globex             # Globex

career_pages:
  - {name: "Initech", url: "https://initech.example/careers"}
  # - {name: "Hooli", url: "https://hooli.example/jobs"}  # 403 when checked

filters:
  title_include: [engineer, scientist]   # keep these
  title_exclude: [intern]
  locations: [new york, remote]
  allow_remote: true

preferences: |
  Research roles on ranking.

schedule:
  max_runs_per_day: 2
  min_hours_between_runs: 6

min_score: 70
db_path: shortlist.db
resume_path: resume.md
"""
TOKEN = {"X-Shortlist-Token": "test-token"}


def job(job_id, title, company="Acme Robotics", location="New York", description=""):
    return Job(ats="greenhouse", slug="acme", job_id=job_id, company=company, title=title, location=location,
               remote=False, url=f"https://acme.example/jobs/{job_id}", description=description)


@pytest.fixture
def app(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "resume.md").write_text("# Resume\nRanking research.", encoding="utf-8")
    conn = db.connect(str(tmp_path / "shortlist.db"))
    strong = job("1", "ML Engineer, Ranking", description="Build ranking models.")
    weak = job("2", "Data Scientist, Payments", company="Globex", location="Remote")
    db.upsert_jobs(conn, [strong, weak, job("3", "Account Executive")], "2026-10-20T10:00:00+00:00")
    db.save_result(conn, strong, json.dumps({"job_id": strong.key, "score": 88, "matched_skills": ["ranking"],
                                             "missing_skills": ["Go"], "dealbreakers": [],
                                             "one_line_reason": "Ranking research fit."}), 88, "2026-10-20T10:05:00+00:00")
    db.save_result(conn, weak, json.dumps({"job_id": weak.key, "score": 55, "matched_skills": [], "missing_skills": [],
                                           "dealbreakers": ["no sponsorship"], "one_line_reason": "Payments analytics."}),
                   55, "2026-10-20T10:05:00+00:00")
    db.record_run(conn, "2026-10-20T10:00:00+00:00", 3, 0, stats={"first_seen": 3, "scored": 2, "matches": 1, "sent": 1},
                  finished_at="2026-10-20T10:06:00+00:00")
    started = []
    monkeypatch.setattr(web.Activity, "start", lambda self, label, args: started.append((label, args)) or True)
    client = TestClient(web.create_app(tmp_path / "config.yaml", allowed_hosts={"testserver"}, token="test-token"))
    ids = {r["title"]: r["rowid"] for r in conn.execute("SELECT rowid, title FROM jobs")}
    return client, tmp_path, ids, started


def test_every_page_renders(app):
    client, *_ = app
    for path in ("/", "/matches", "/tracker", "/sources", "/runs", "/settings", "/activity"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Shortlist" in response.text or path == "/activity"
    assert "2 jobs scored" in client.get("/matches").text
    overview = client.get("/").text
    assert "1 strong match waiting for you" in overview and "ML Engineer, Ranking" in overview  # the 88; 55 is below 70
    assert 'class="count"' in overview  # the sidebar badge for waiting matches


def test_matches_search_and_filters(app):
    client, *_ = app
    rows = client.get("/matches/rows").text
    assert rows.index("ML Engineer, Ranking") < rows.index("Data Scientist, Payments")
    assert "Account Executive" not in rows  # never scored
    assert "Data Scientist" not in client.get("/matches/rows", params={"min_score": 60}).text
    assert "ML Engineer" not in client.get("/matches/rows", params={"q": "payments"}).text
    assert "ML Engineer" not in client.get("/matches/rows", params={"remote": "1"}).text


def test_changes_need_the_token_and_a_local_host(app):
    client, _, ids, _ = app
    assert client.post(f"/jobs/{ids['ML Engineer, Ranking']}/track", data={"stage": "saved"}).status_code == 403
    assert client.get("/matches", headers={"Host": "evil.example"}).status_code == 403


def test_tracking_stage_notes_feedback_and_export(app):
    client, tmp_path, ids, _ = app
    job_id = ids["ML Engineer, Ranking"]
    response = client.post(f"/jobs/{job_id}/track", data={"stage": "applied"}, headers=TOKEN)
    assert response.status_code == 204
    assert json.loads(response.headers["HX-Trigger"]) == {"jobChanged": None, "toast": "Moved to Applied"}
    note = client.post(f"/jobs/{job_id}/track", data={"notes": "Referral from Sam"}, headers=TOKEN)
    assert json.loads(note.headers["HX-Trigger"]) == {"notesSaved": None}  # no list redraw while typing
    client.post(f"/jobs/{job_id}/track", data={"feedback": "1"}, headers=TOKEN)
    row = db.job_row(db.connect(str(tmp_path / "shortlist.db")), job_id)
    assert (row["stage"], row["notes"], row["feedback"]) == ("applied", "Referral from Sam", 1) and row["applied_at"]
    assert client.post(f"/jobs/{job_id}/track", data={"stage": "hired!"}, headers=TOKEN).status_code == 400
    tracker = client.get("/tracker").text
    assert "ML Engineer, Ranking" in tracker and "Referral from Sam" in tracker
    csv_text = client.get("/tracker.csv").text
    assert "ML Engineer, Ranking" in csv_text and "applied" in csv_text
    assert "ML Engineer" not in client.get("/matches/rows", params={"stage": "saved"}).text


def test_job_detail_panel(app):
    client, _, ids, _ = app
    detail = client.get(f"/jobs/{ids['ML Engineer, Ranking']}").text
    assert "Ranking research fit." in detail and "Build ranking models." in detail and "chip ok" in detail
    assert "no sponsorship" in client.get(f"/jobs/{ids['Data Scientist, Payments']}").text
    assert client.get("/jobs/9999").status_code == 404


def test_settings_rewrite_only_their_sections(app):
    client, tmp_path, *_ = app
    form = {"min_score": "75", "title_include": "engineer, researcher\nscientist", "title_exclude": "intern",
            "locations": "new york, london", "allow_remote": "", "preferences": "Applied research.\nNo managers.",
            "max_runs_per_day": "3", "min_hours_between_runs": "4"}
    response = client.post("/settings", data=form, headers=TOKEN)
    assert response.status_code == 204 and response.headers["HX-Redirect"] == "/settings?saved=settings"
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    saved = yaml.safe_load(text)
    assert saved["min_score"] == 75 and saved["filters"]["title_include"] == ["engineer", "researcher", "scientist"]
    assert saved["filters"]["allow_remote"] is False and saved["schedule"]["max_runs_per_day"] == 3
    assert saved["preferences"] == "Applied research.\nNo managers.\n"
    assert "# Acme Robotics" in text and "# my settings" in text  # untouched sections keep their comments
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == CONFIG
    bad = client.post("/settings", data={**form, "min_score": "500"}, headers=TOKEN)
    assert bad.status_code == 400 and (tmp_path / "config.yaml").read_text(encoding="utf-8") == text


def test_resume_is_saved(app):
    client, tmp_path, *_ = app
    client.post("/settings/resume", data={"resume": "# New resume\r\nRecsys."}, headers=TOKEN)
    assert (tmp_path / "resume.md").read_text(encoding="utf-8") == "# New resume\nRecsys."


def test_adding_a_careers_page_writes_the_config_and_starts_discovery(app):
    client, tmp_path, _, started = app
    response = client.post("/sources/career-pages", data={"name": "Umbrella AI", "url": "https://umbrella.example/jobs"},
                           headers=TOKEN)
    assert response.status_code == 204 and response.headers["HX-Redirect"] == "/runs"
    pages = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))["career_pages"]
    assert pages[-1] == {"name": "Umbrella AI", "url": "https://umbrella.example/jobs"} and len(pages) == 2
    assert started == [("discover Umbrella AI", ["discover", "--site", "Umbrella AI"])]
    again = client.post("/sources/career-pages", data={"name": "Umbrella", "url": "https://umbrella.example/jobs"},
                        headers=TOKEN)
    assert again.status_code == 400
    assert "Initech" in client.get("/sources").text and "greenhouse:acme" in client.get("/sources").text


def test_run_buttons_and_history(app):
    client, _, _, started = app
    assert "6.0" in client.get("/runs").text  # minutes of the recorded run
    assert client.post("/runs", data={"kind": "dry"}, headers=TOKEN).status_code == 204
    assert client.post("/runs", data={"kind": "explode"}, headers=TOKEN).status_code == 400
    assert started == [("dry run", ["run", "--dry-run"])]


def test_config_text_helpers_keep_comments():
    text = web.replace_sections(CONFIG, {"min_score": 80})
    assert "min_score: 80" in text and "# Acme Robotics" in text and "# keep these" in text
    added = web.add_career_page(CONFIG, "Umbrella", "https://u.example")
    lines = added.splitlines()
    assert lines.index('  - {name: "Umbrella", url: "https://u.example"}') == lines.index(
        '  - {name: "Initech", url: "https://initech.example/careers"}') + 1  # before the commented-out entry
    assert yaml.safe_load(added)["career_pages"][-1]["name"] == "Umbrella"


def test_settings_switch_backend_channels_and_packs_and_leave_the_rest_alone(app, monkeypatch):
    client, tmp_path, *_ = app
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    page = client.get("/settings").text
    assert "consumer-tech" in page and 'value="telegram"' in page and 'value="claude_cli" selected' in page
    unchanged = {"min_score": "70", "title_include": "engineer, scientist", "title_exclude": "intern",
                 "locations": "new york, remote", "allow_remote": "1", "preferences": "Research roles on ranking.",
                 "max_runs_per_day": "2", "min_hours_between_runs": "6"}
    response = client.post("/settings", headers=TOKEN, data={
        **unchanged, "backend": "anthropic_api", "model": "sonnet",  # "sonnet" was claude_cli's
        "notify": ["telegram"], "packs": ["new-grad"]})
    assert response.status_code == 204
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    saved = yaml.safe_load(text)
    assert saved["scoring"] == {"backend": "anthropic_api"}  # the new backend's default model, not claude_cli's
    assert saved["notify"] == ["telegram"] and saved["packs"] == ["new-grad"]
    assert "# keep these" in text  # unchanged filters keep their comments
    page = client.get("/settings").text
    assert "claude-opus-5-5" in page and "ANTHROPIC_API_KEY: missing" in page and "TELEGRAM_CHAT_ID: missing" in page


def test_matches_page_in_pages_of_fifty(app):
    client, tmp_path, *_ = app
    conn = db.connect(str(tmp_path / "shortlist.db"))
    extra = [job(f"x{n}", f"Research Engineer {n}") for n in range(60)]
    db.upsert_jobs(conn, extra, "2026-10-20T10:00:00+00:00")  # the same read of the board as the others
    for j in extra:
        db.save_result(conn, j, json.dumps({"job_id": j.key, "score": 75, "matched_skills": [], "missing_skills": [],
                                            "dealbreakers": [], "one_line_reason": "Fine."}), 75, "2026-10-21T10:05:00+00:00")
    first = client.get("/matches/rows").text
    assert "62</b> jobs" in first and first.count('class="match"') == 50 and "Show 12 more" in first
    rest = client.get("/matches/rows", params={"offset": 50}).text
    assert rest.count('class="match"') == 12 and "Show" not in rest and "jobs</span>" not in rest


def test_template_helpers():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    assert web.ago((now - timedelta(seconds=30)).isoformat()) == "just now"
    assert web.ago((now - timedelta(hours=3, minutes=5)).isoformat()) == "3h ago"
    assert web.ago((now - timedelta(days=2)).isoformat()) == "2d ago"
    assert web.ago("2025-03-04T10:00:00+00:00") == "Mar 4" and web.ago(None) == ""
    assert (web.initials("Hugging Face"), web.initials("x.ai"), web.initials("")) == ("HF", "XA", "?")
    assert web.hue("Meta") == web.hue("meta") and 0 <= web.hue("Meta") < 360
    assert [web.band(s) for s in (None, 30, 55, 72, 90)] == ["none", "low", "fair", "good", "top"]


def test_filtered_out_page_explains_drops_and_lets_words_through(app):
    client, tmp_path, *_ = app
    conn = db.connect(str(tmp_path / "shortlist.db"))
    db.upsert_jobs(conn, [job("4", "ML Engineer Intern"), job("5", "Data Engineer", location="Tokyo")],
                   "2026-10-20T10:00:00+00:00")  # first seen in the recorded run, like "Account Executive"
    page = client.get("/filtered").text
    assert "Account Executive" in page and "no title word" in page and "excluded: intern" in page
    assert "Add to titles" in page and "account executive" in page and "tokyo" in page
    response = client.post("/filters/add", data={"field": "title_include", "term": "account executive", "scope": "run"},
                           headers=TOKEN)
    assert response.status_code == 204 and "passing=1" in response.headers["HX-Redirect"]
    filters = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))["filters"]
    assert filters["title_include"] == ["engineer", "scientist", "account executive"]
    client.post("/filters/remove", data={"field": "title_exclude", "term": "intern"}, headers=TOKEN)
    assert yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))["filters"]["title_exclude"] == []
    assert client.post("/filters/add", data={"field": "db_path", "term": "x"}, headers=TOKEN).status_code == 400


def test_closed_postings_are_marked_and_hidden_unless_asked_for(app):
    client, tmp_path, ids, _ = app
    conn = db.connect(str(tmp_path / "shortlist.db"))
    # A later read of the board lists the ranking job but no longer the payments one: that posting closed.
    db.upsert_jobs(conn, [job("1", "ML Engineer, Ranking", description="Build ranking models.")], "2026-10-22T10:00:00+00:00")
    rows = client.get("/matches/rows").text
    assert "ML Engineer, Ranking" in rows and "Data Scientist, Payments" not in rows
    shown = client.get("/matches/rows", params={"closed": "1"}).text
    assert "Data Scientist, Payments" in shown and shown.count('class="pill closed"') == 1
    assert "No longer listed" in client.get(f"/jobs/{ids['Data Scientist, Payments']}").text
    assert "No longer listed" not in client.get(f"/jobs/{ids['ML Engineer, Ranking']}").text
    client.post(f"/jobs/{ids['Data Scientist, Payments']}/track", data={"stage": "applied"}, headers=TOKEN)
    assert "pill closed" in client.get("/tracker").text  # still tracked, with the badge


def test_settings_shows_how_often_scores_agree_with_your_ratings(app):
    client, _, ids, _ = app
    assert "Rate matches with" in client.get("/settings").text
    client.post(f"/jobs/{ids['ML Engineer, Ranking']}/track", data={"feedback": "1"}, headers=TOKEN)  # 88: agrees
    client.post(f"/jobs/{ids['Data Scientist, Payments']}/track", data={"feedback": "1"}, headers=TOKEN)  # 55: disagrees
    assert "<b>1 of 2</b> jobs you rated" in client.get("/settings").text


def test_scoring_prompt_can_be_changed_restored_and_jobs_rescored(app):
    client, tmp_path, _, started = app
    from shortlist.scoring import INSTRUCTIONS
    page = client.get("/settings").text
    assert "This is the built-in version" in page and "Score each job from 0 to 100" in page
    assert "The whole prompt for one job" in page and "Ranking research." in page  # the resume, in the full prompt
    assert "2 open jobs scoring\n  55+ were scored with an earlier resume" in page  # scored before fingerprints

    assert client.post("/settings/prompt", data={"instructions": "Judge strictly.\r\nUse only the text below.  "},
                       headers=TOKEN).status_code == 204
    config = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(config)["scoring"]["instructions"] == "Judge strictly.\nUse only the text below.\n"
    assert config.startswith("# my settings") and "# Acme Robotics" in config  # the rest is left as it was
    assert "Your own version" in client.get("/settings").text
    client.post("/settings/prompt", data={"restore": "1"}, headers=TOKEN)
    assert "instructions" not in (yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8")).get("scoring") or {})
    client.post("/settings/prompt", data={"instructions": INSTRUCTIONS}, headers=TOKEN)  # the built-in text: nothing stored
    assert "instructions" not in (yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8")).get("scoring") or {})

    assert "<b>1</b> open job scoring 80+" in client.get("/rescore/count?from_score=80&scope=all").text
    assert "No open job scoring 95+" in client.get("/rescore/count?from_score=95&scope=all").text
    response = client.post("/rescore", data={"from_score": "55", "scope": "outdated"}, headers=TOKEN)
    assert response.status_code == 204 and response.headers["HX-Redirect"] == "/runs"
    client.post("/rescore", data={"from_score": "80", "scope": "all"}, headers=TOKEN)
    assert started == [("re-score", ["rescore", "--from", "55"]), ("re-score", ["rescore", "--from", "80", "--all"])]
    assert client.post("/rescore", data={"from_score": "95", "scope": "all"}, headers=TOKEN).status_code == 400
