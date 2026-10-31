import json

from shortlist import db
from shortlist.fetchers import Job


def job(job_id, slug="acme", description="d"):
    return Job(ats="greenhouse", slug=slug, job_id=job_id, company="Acme", title="ML Engineer", location="NYC",
               remote=False, url="https://example.com", description=description)


def test_upsert_dedupes_and_tracks_unscored(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    jobs = [job("1"), job("2"), job("1", slug="other")]
    assert db.upsert_jobs(conn, jobs, "t1") == 3
    assert db.upsert_jobs(conn, jobs, "t2") == 0
    rows = conn.execute("SELECT slug, job_id, first_seen, last_seen FROM jobs ORDER BY slug, job_id").fetchall()
    assert [tuple(r) for r in rows] == [("acme", "1", "t1", "t2"), ("acme", "2", "t1", "t2"), ("other", "1", "t1", "t2")]
    assert db.unscored_keys(conn) == {j.key for j in jobs}


def test_schema_survives_reconnect(tmp_path):
    path = str(tmp_path / "t.db")
    db.upsert_jobs(db.connect(path), [job("1")], "t1")
    assert db.upsert_jobs(db.connect(path), [job("1"), job("2")], "t2") == 1


def test_full_scan_schedule_and_known_keys(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    assert db.full_scans_due(conn, ["workday:a", "html:b"], "2026-10-15") == {"workday:a", "html:b"}
    db.record_full_scans(conn, {"workday:a"}, "2026-10-20")
    assert db.full_scans_due(conn, ["workday:a", "html:b"], "2026-10-15") == {"html:b"}
    assert db.full_scans_due(conn, ["workday:a"], "2026-10-21") == {"workday:a"}
    db.upsert_jobs(conn, [job("1")], "t1")
    assert db.known_keys(conn) == {"greenhouse:acme:1"}


def test_sites_store_results_and_forget(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.save_site(conn, "https://a/careers", "Acme", "board", board="lever:acme", note="linked", agent_calls=0, now="t1")
    db.record_site_results(conn, {"https://a/careers": 0}, "t2")
    db.record_site_results(conn, {"https://a/careers": 0}, "t3")
    row = db.sites(conn)["https://a/careers"]
    assert (row["empty_runs"], row["last_ok"]) == (2, None)
    db.record_site_results(conn, {"https://a/careers": 12}, "t4")
    row = db.sites(conn)["https://a/careers"]
    assert (row["empty_runs"], row["last_ok"]) == (0, "t4")
    assert db.forget_site(conn, "acme") == 1 and db.sites(conn) == {}


def test_results_duplicates_and_pending_matches(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    good, copy, weak, broken, gone = job("1"), job("2"), job("3", description="x"), job("4", description="y"), job("5", description="z")
    db.upsert_jobs(conn, [gone], "t0")
    db.upsert_jobs(conn, [good, copy, weak, broken], "t1")
    db.save_result(conn, good.model_copy(update={"location": "NYC; London"}), '{"one_line_reason": "r"}', 90, "t1")
    db.mark_duplicates(conn, [copy], good.key)
    db.save_result(conn, weak, '{"one_line_reason": "r"}', 40, "t1")
    db.save_result(conn, broken, None, None, "t1")
    db.save_result(conn, gone, '{"one_line_reason": "r"}', 95, "t0")

    assert db.unscored_keys(conn) == set()
    assert db.handled_roles(conn)[good.role_hash] == good.key
    pending = db.pending_matches(conn, 70, "t1")  # `gone` scores higher but was not seen in this run
    assert [(r["job_id"], r["location"], r["score"]) for r in pending] == [("1", "NYC; London", 90)]
    status = dict(conn.execute("SELECT job_id, status FROM jobs").fetchall())
    assert status == {"1": "scored", "2": "duplicate", "3": "scored", "4": "failed", "5": "scored"}


def test_feedback_examples_put_disagreements_first_and_balance_the_kinds(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    jobs = [Job(ats="lever", slug="a", job_id=str(n), company="Acme", title=f"Role {n}", location="NYC", remote=False,
                url=f"https://x/{n}", description="d") for n in range(8)]
    db.upsert_jobs(conn, jobs, "2026-10-20T10:00:00+00:00")
    for j, score in zip(jobs, (90, 40, 85, 30, 75, 20, 88, 60)):
        db.save_result(conn, j, json.dumps({"job_id": j.key, "score": score}), score, "2026-10-20T10:01:00+00:00")
    rowid = dict(conn.execute("SELECT job_id, rowid FROM jobs"))
    ratings = [("0", None, -1), ("1", None, 1), ("2", "applied", 0), ("3", "not_interested", 0), ("4", None, 1),
               ("5", None, -1), ("6", "saved", 0), ("7", "interviewing", 0)]  # job 6: saved only, not a judgment
    for i, (job_id, stage, feedback) in enumerate(ratings):
        db.set_tracking(conn, rowid[job_id], f"2026-10-21T10:0{i}:00+00:00", stage=stage, feedback=feedback or None)
    examples = db.feedback_examples(conn, min_score=70, limit=20)
    assert [(e["title"], e["good"], e["disagrees"]) for e in examples] == [
        ("Role 7", True, True), ("Role 1", True, True), ("Role 0", False, True),  # disagreements, newest first
        ("Role 4", True, False), ("Role 2", True, False), ("Role 5", False, False), ("Role 3", False, False)]
    few = db.feedback_examples(conn, min_score=70, limit=4)
    assert sum(e["good"] for e in few) == 2 and few[0]["disagrees"]
