import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from shortlist import db, publish
from shortlist.config import Config, Filters, Publish
from shortlist.fetchers import Job

NOW = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)


def job(job_id, title, location="New York, NY", ats="greenhouse", slug="acme", company="Acme"):
    return Job(ats=ats, slug=slug, job_id=job_id, company=company, title=title, location=location, remote=False,
               url=f"https://boards.example/{slug}/{job_id}", description="")


@pytest.fixture
def setup(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.upsert_jobs(conn, [job("1", "Machine Learning Engineer New Grad", "Seattle, WA"),
                          job("2", "Machine Learning Engineer New Grad", "San Jose, CA"),  # the same role elsewhere
                          job("3", "Research Scientist | Ranking"),
                          job("4", "Software Engineer Intern"),
                          job("9", "Marketing Intern", slug="gone"),  # an internship, but in no field
                          job("5", "Senior Software Engineer"),  # in no published group
                          job("6", "Data Scientist New Grad", ats="simplify", slug="acme-corp"),  # only from SimplifyJobs
                          job("7", "Research Engineer, Old", slug="gone")], "2026-11-09T12:00:00+00:00")
    db.upsert_jobs(conn, [job("8", "PhD Research Intern", slug="gone")], "2026-11-01T12:00:00+00:00")  # not seen lately
    db.upsert_jobs(conn, [job("1", "Machine Learning Engineer New Grad", "Seattle, WA")], "2026-11-09T13:00:00+00:00")
    cfg = Config(publish=Publish(folder=str(tmp_path / "jobs")))
    return cfg, conn, tmp_path / "jobs"


def test_the_lists_hold_open_roles_by_group(setup):
    cfg, conn, folder = setup
    total, counts = publish.write(cfg, conn, NOW)
    # Acme's board was read again at 13:00 with only role 1, so its other roles closed (Greenhouse lists every open
    # job). Role 7's board was last read at 12:00 with it. Role 8 hasn't been listed for a week.
    assert total == 2 and counts == {"intern": 0, "new-grad": 1, "phd": 0, "research": 1}
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert "| [New grad](new-grad/README.md) | [1](new-grad/ml.md) | – | 1 |" in readme
    assert "Updated 2026-11-09 18:00 UTC · 2 open roles" in readme and "Made with [Shortlist](" in readme
    page = (folder / "new-grad" / "ml.md").read_text(encoding="utf-8")
    assert "| **Acme** | [Machine Learning Engineer New Grad](<https://boards.example/acme/1>) | Seattle, WA |" in page
    assert "| [ML & AI](ml.md) | 1 | 1 | [A](ml.md) |" in (folder / "new-grad" / "README.md").read_text(encoding="utf-8")
    assert "Research Engineer, Old" in (folder / "research" / "general.md").read_text(encoding="utf-8")
    data = json.loads((folder / "jobs.json").read_text(encoding="utf-8"))
    assert {r["title"] for r in data["roles"]} == {"Machine Learning Engineer New Grad", "Research Engineer, Old"}
    assert not any("simplify" in r["url"] or r["company"] == "acme-corp" for r in data["roles"])


def test_closed_roles_leave_and_unchanged_pages_stay_as_they_were(setup):
    cfg, conn, folder = setup
    publish.write(cfg, conn, NOW)
    research = folder / "research" / "general.md"
    before = research.read_bytes(), research.stat().st_mtime_ns
    # Hours later Acme lists a data role instead of its ML one; the other board is as it was.
    db.upsert_jobs(conn, [job("10", "Data Scientist New Grad")], "2026-11-09T17:00:00+00:00")
    publish.write(cfg, conn, NOW.replace(hour=23))
    assert not (folder / "new-grad" / "ml.md").exists()
    assert "Data Scientist New Grad" in (folder / "new-grad" / "data.md").read_text(encoding="utf-8")
    assert (research.read_bytes(), research.stat().st_mtime_ns) == before
    assert "Updated 2026-11-09 23:00 UTC" in (folder / "README.md").read_text(encoding="utf-8")


def test_older_roles_on_paged_boards_stay_listed_until_a_week_unseen(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    wd = {"ats": "workday", "slug": "acme.wd1/Careers"}
    db.upsert_jobs(conn, [job("1", "Research Engineer", **wd)], "2026-10-31T12:00:00+00:00")  # unseen for 9 days
    db.upsert_jobs(conn, [job("2", "Applied Scientist Intern", **wd)], "2026-11-04T12:00:00+00:00")  # last full read
    db.upsert_jobs(conn, [job("3", "Research Scientist, Ranking", **wd)], "2026-11-09T12:00:00+00:00")  # today's page 1
    db.upsert_jobs(conn, [job("4", "Research Scientist", slug="stale")], "2026-11-06T12:00:00+00:00")  # board unread 3 days
    assert {r["url"].rsplit("/", 1)[1] for r in publish.open_roles(conn, ["research", "intern"], NOW)} == {"2", "3"}


def test_the_configs_filters_apply_to_the_lists(setup):
    cfg, conn, folder = setup
    cfg.filters = Filters(title_exclude=["old"])
    assert publish.write(cfg, conn, NOW) == (1, {"intern": 0, "new-grad": 1, "phd": 0, "research": 0})


def test_company_names_come_from_the_packs_comments(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.upsert_jobs(conn, [job("1", "Research Scientist", ats="workday", slug="ag.wd3/Airbus", company="ag"),
                          job("2", "Research Scientist", ats="ashby", slug="dandy", company="dandy")],
                   "2026-11-09T12:00:00+00:00")
    (tmp_path / "mine.yaml").write_text("companies:\n  - workday:ag.wd3/Airbus   # Airbus\n  - ashby:dandy\n",
                                        encoding="utf-8")
    cfg = Config(packs=["mine.yaml"], db_path=str(tmp_path / "t.db"))
    names = publish.board_names(cfg)
    assert names == {"workday:ag.wd3/airbus": "Airbus"}
    assert {r["company"] for r in publish.open_roles(conn, ["research"], NOW, names)} == {"Airbus", "Dandy"}


def test_rows_are_safe_markdown_and_one_row_per_role(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    title = "Research Scientist | Ranking [Remote]"
    db.upsert_jobs(conn, [job("1", title, "NYC"), job("2", title, "SF"), job("3", title, "LA")],
                   "2026-11-09T12:00:00+00:00")
    roles = publish.open_roles(conn, ["research"], NOW)
    assert len(roles) == 1 and roles[0]["locations"] == ["LA", "NYC", "SF"]
    assert roles[0]["url"] == "https://boards.example/acme/3"  # the same link every time, whatever order rows come in
    row = publish._row(roles[0], "**Acme**")
    assert "| **Acme** | [Research Scientist \\| Ranking (Remote)](<" in row and "LA; NYC (+1 more)" in row


def test_long_lists_go_on_over_pages_split_at_letters_then_companies(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    sizes = {"Alpha": 3, "Apex": 3, "Beta": 3, "Bolt": 80, "Cora": 3, "Delta": 3}
    db.upsert_jobs(conn, [job(f"{c}{n}", f"Machine Learning Engineer New Grad {n}", slug=c.lower(), company=c)
                          for c, k in sizes.items() for n in range(k)], "2026-11-09T12:00:00+00:00")
    roles = publish.open_roles(conn, ["new-grad"], NOW)
    pages = publish.field_pages(roles, "new-grad", "ml", "ML & AI", budget=8_000)
    assert [file for file, _, _ in pages][:2] == ["ml.md", "ml-2.md"] and len(pages) > 2
    assert all(len(text.encode()) <= 8_000 for _, _, text in pages)
    rows = [line for _, _, text in pages for line in text.splitlines() if "](<https://boards.example/" in line]
    assert len(rows) == len(roles) == 95
    labels = [label for _, label, _ in pages]
    assert len(set(labels)) == len(labels) and labels[0] == "A"  # A fits on a page by itself, so B starts a new one
    starts = [text.split("|---|---|---|---|\n")[1].split(" |")[0] for _, _, text in pages]
    assert starts[:2] == ["| **Alpha**", "| **Beta**"]
    assert "| **Bolt** (continued)" in starts  # Bolt alone is longer than a page
    assert f"Pages: **{labels[0]}** · [{labels[1]}](ml-2.md)" in pages[0][2]


def git(folder, *args):
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, check=True).stdout


def test_changes_are_committed_and_pushed_and_nothing_else(setup):
    cfg, conn, folder = setup
    remote = folder.parent / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(folder)], check=True, capture_output=True)
    git(folder, "config", "user.email", "bot@example.com")
    git(folder, "config", "user.name", "Bot")
    publish.publish(cfg, conn, push_changes=True)
    subject = git(remote, "log", "--format=%s", "-1").strip()
    assert subject == "Update the lists: 2 open roles (Internships 0, New grad 1, PhD & postdoc 0, Research 1)"
    publish.publish(cfg, conn, push_changes=True)  # the same lists again: no new commit
    assert len(git(remote, "log", "--format=%h").split()) == 1


def test_push_errors_never_show_credentials():
    assert publish.redact("fatal: unable to access 'https://user:ghp_SECRET@github.com/x/y.git/'") == \
        "fatal: unable to access 'https://***@github.com/x/y.git/'"
