"""The apply agent loop, with a scripted agent and a recording browser (tests never start a real one)."""
import pytest

from shortlist import apply, db
from shortlist.config import load_config
from shortlist.fetchers import Job

SNAPSHOT = """\
- heading "Apply for this job" [ref=e1]
- textbox "First Name" [ref=e2]
- textbox "Password" [ref=e3]
- button "Attach" [ref=e4] [cursor=pointer]
- button "Submit application" [ref=e5] [cursor=pointer]"""
JOB = {"url": "https://jobs.example/1", "company": "Example", "title": "Engineer"}
ANSWERS = {"files": {"resume": "/files/resume.pdf"}, "answers": [{"question": "First name", "answer": "Alex"},
                                                                 {"question": "Salary?", "answer": ""}]}


class FakeBrowser:
    closed = False

    def __init__(self, fail=(), page=SNAPSHOT):
        self.done, self.fail, self.page, self.tabs, self.opened, self.waited = [], set(fail), page, 1, [], 0

    def snapshot(self):
        return self.page

    def host(self):
        return "jobs.example"

    def open(self, url):
        self.opened.append(url)

    def start_tab(self):  # self.tabs: the window's first tab and those kept for you
        self.tabs += 1
        return ["tab"]

    def drop_tabs(self, tabs):
        self.tabs -= len(tabs)

    def wait_closed(self):
        self.waited += 1

    def close(self):
        pass

    def _do(self, *what):
        if what[1] in self.fail:
            raise RuntimeError("element is not visible")
        self.done.append(what)

    def fill(self, ref, value): self._do("fill", ref, value)
    def fill_password(self, ref, password): self._do("password", ref, password)
    def choose(self, ref, value): self._do("choose", ref, value)
    def check(self, ref): self._do("check", ref)
    def click(self, ref): self._do("click", ref)
    def upload(self, ref, path): self._do("upload", ref, path)


def scripted(*steps):
    prompts, queue = [], list(steps)

    def ask(prompt):
        prompts.append(prompt)
        return queue.pop(0)
    return ask, prompts


def step(status="continue", actions=(), questions=(), reason=""):
    return {"status": status, "reason": reason, "questions": list(questions),
            "actions": [{"do": do, "ref": ref, "value": value} for do, ref, value in actions]}


FILL = step(actions=[("fill", "e2", "Alex"), ("upload", "e4", "resume")])
SUBMIT = step(actions=[("submit", "e5", "")])


def run(browser, ask, **kwargs):
    kwargs = {"submit": False, "max_calls": 6, **kwargs}
    return apply.apply_to_job(browser, ask, JOB, ANSWERS, "resume text", **kwargs)


def test_review_mode_fills_and_leaves_the_last_button_to_you():
    browser, (ask, prompts) = FakeBrowser(), scripted(FILL, SUBMIT)
    outcome = run(browser, ask)
    assert (outcome.status, outcome.calls) == ("ready", 2)
    assert browser.done == [("fill", "e2", "Alex"), ("upload", "e4", "/files/resume.pdf")]  # e5 was never pressed
    assert 'fill textbox "First Name": done' in prompts[1]
    assert "- First name: Alex" in prompts[0] and "Salary" not in prompts[0]  # unanswered entries are not answers


def test_auto_mode_presses_it_and_reports_submitted_once_the_page_confirms():
    browser, (ask, _) = FakeBrowser(), scripted(FILL, SUBMIT, step("done", reason="Thank you for applying."))
    outcome = run(browser, ask, submit=True)
    assert outcome.status == "submitted" and browser.done[-1] == ("click", "e5")


def test_done_without_a_submit_is_not_reported_as_submitted():
    outcome = run(FakeBrowser(), scripted(step("done", reason="You already applied."))[0], submit=True)
    assert outcome.status == "stopped" and "nothing was sent" in outcome.note


def test_a_code_sent_by_email_after_submit_is_handed_over_not_reported_as_sent():
    browser, (ask, prompts) = FakeBrowser(), scripted(SUBMIT, step("hand_over", reason="Type the code the site emailed."))
    outcome = run(browser, ask, submit=True)
    assert (outcome.status, outcome.note) == ("your_turn", "Type the code the site emailed.")
    assert 'status "hand_over"' in prompts[0] and "Never try to get past a bot check" in prompts[0]


def test_a_pressed_submit_without_a_confirmation_is_left_open_never_retried_as_failed():
    for last in (step("give_up", reason="Stuck."), *[step(actions=[("fill", "e2", "Alex")])] * 4):
        ask, _ = scripted(SUBMIT, last, *[last] * 4)
        outcome = run(FakeBrowser(), ask, submit=True, max_calls=3)
        assert outcome.status == "your_turn" and outcome.note.startswith('button "Submit application" was pressed')
        assert "check the tab and your email" in outcome.note


def test_the_password_is_typed_only_on_the_jobs_site_and_known_job_boards():
    assert apply._site("acme.wd5.myworkdayjobs.com") == "myworkdayjobs.com" and apply._site("jobs.acme.co.uk") == "acme.co.uk"
    assert apply._password_site("job-boards.eu.greenhouse.io", set()) and apply._password_site("careers.acme.com", {"acme.com"})
    assert not apply._password_site("login.elsewhere.example", {"acme.com"}) and not apply._password_site("", {""})

    class Elsewhere(FakeBrowser):
        def host(self):  # the job link led here; then the page sent the agent to another site
            return "jobs.example" if not self.done else "login.elsewhere.example"
    browser = Elsewhere()
    ask, prompts = scripted(step(actions=[("click", "e1", "")]), step(actions=[("fill", "e3", "{password}")]),
                            step("hand_over", reason="x"))
    run(browser, ask, password="s3cret")
    assert browser.done == [("click", "e1")] and "not on login.elsewhere.example: hand this one over" in prompts[2]


def test_submit_among_other_actions_is_refused():
    browser, (ask, prompts) = FakeBrowser(), scripted(step(actions=[("submit", "e5", ""), ("fill", "e2", "x")]),
                                                      step("give_up", reason="stuck"))
    assert run(browser, ask, submit=True).status == "stopped" and browser.done == []
    assert "must be the only action" in prompts[1]


def test_unknown_questions_end_the_application_with_suggestions():
    ask, _ = scripted(step("ask", questions=[{"question": "Salary expectations?", "suggested": "Open"}]))
    browser = FakeBrowser()
    outcome = run(browser, ask)
    assert outcome.status == "questions" and browser.done == []
    assert outcome.questions == [{"question": "Salary expectations?", "suggested": "Open", "reusable": False}]


def test_the_password_reaches_only_the_password_method_and_never_the_prompt():
    browser, (ask, prompts) = FakeBrowser(), scripted(step(actions=[("fill", "e3", "{password}")]),
                                                      step(actions=[("fill", "e2", "mine is {password}")]),
                                                      step("give_up", reason="x"))
    run(browser, ask, password="s3cret")
    assert browser.done == [("password", "e3", "s3cret")]  # the second, mixed into other text, was refused
    assert all("s3cret" not in p for p in prompts) and "only for a password field" in prompts[2]
    assert "An account password is available" in prompts[0]


def test_a_page_that_shows_the_password_does_not_show_it_to_the_agent():
    browser = FakeBrowser(page=SNAPSHOT + '\n- textbox "Password": s3cret [ref=e9]')  # a "show password" toggle
    ask, prompts = scripted(step("give_up", reason="x"))
    run(browser, ask, password="s3cret")
    assert "s3cret" not in prompts[0] and '- textbox "Password": {password} [ref=e9]' in prompts[0]


def test_the_agent_is_told_whether_the_site_has_an_account():
    create = step(actions=[("fill", "e3", "{password}")])
    browser, (ask, prompts) = FakeBrowser(), scripted(create, step("give_up", reason="check your email"))
    outcome = run(browser, ask, password="s3cret")
    assert "never typed on this site: if the site wants an account, create one" in prompts[0]
    assert "typed on this site before (earlier in this application)" in prompts[1]
    assert outcome.account_hosts == ["jobs.example"]  # remembered, so the next application there signs in
    ask, prompts = scripted(step("give_up", reason="x"))
    assert run(FakeBrowser(), ask, password="s3cret", accounts={"jobs.example": "2026-11-12"}).account_hosts == []
    assert "typed on this site before (first on 2026-11-12)" in prompts[0] and "sign in" in prompts[0]


def test_without_a_password_the_placeholder_is_refused():
    browser, (ask, prompts) = FakeBrowser(), scripted(step(actions=[("fill", "e3", "{password}")]), step("give_up"))
    run(browser, ask)
    assert browser.done == [] and "No account password is available" in prompts[0]


def test_a_failed_action_stops_its_turn_and_is_told_to_the_agent():
    browser, (ask, prompts) = FakeBrowser(fail={"e2"}), scripted(FILL, step("give_up", reason="x"))
    run(browser, ask)
    assert browser.done == [] and 'fill textbox "First Name" failed: RuntimeError: element is not visible' in prompts[1]


def test_three_failed_turns_in_a_row_end_the_application():
    browser, (ask, _) = FakeBrowser(fail={"e5"}), scripted(*[step(actions=[("click", "e5", "")])] * 5)
    outcome = run(browser, ask, max_calls=10)
    assert (outcome.status, outcome.calls) == ("failed", 3) and outcome.note.startswith("stuck: click button")


def test_nothing_after_a_click_is_done():
    browser, (ask, prompts) = FakeBrowser(), scripted(step(actions=[("click", "e1", ""), ("fill", "e2", "Alex")]),
                                                      step("give_up"))
    run(browser, ask)
    assert browser.done == [("click", "e1")] and "after that click were not done" in prompts[1]


def test_typing_into_a_suggesting_box_ends_the_turn_so_its_options_can_be_picked():
    browser = FakeBrowser(page=SNAPSHOT + '\n- combobox "Location (City)" [ref=e6]')
    (ask, prompts) = scripted(step(actions=[("fill", "e6", "Boston"), ("fill", "e2", "Alex")]),
                                                      step("give_up"))
    run(browser, ask)
    assert browser.done == [("fill", "e6", "Boston")] and "after that fill were not done" in prompts[1]


def test_preferences_reach_the_agent_for_questions_about_working_at_the_location():
    ask, prompts = scripted(step("give_up"))
    run(FakeBrowser(), ask, preferences="Locations: United States. Places: san mateo, boston")
    assert "<preferences>\nLocations: United States. Places: san mateo, boston\n</preferences>" in prompts[0]
    assert "never from the preferences" in prompts[0]  # visa and authorization stay with the stored answers


def test_unknown_files_bad_refs_and_the_call_cap():
    browser, (ask, prompts) = FakeBrowser(), scripted(step(actions=[("upload", "e4", "passport")]),
                                                      step(actions=[("click", "e5 >> nth=0", "")]), FILL, FILL)
    outcome = run(browser, ask, max_calls=4)
    assert (outcome.status, outcome.calls) == ("failed", 4)
    assert "no file named 'passport'" in prompts[1] and "did not match the expected fields" in prompts[2]
    assert ("click", "e5 >> nth=0") not in browser.done


def test_a_failing_agent_call_is_an_outcome_not_a_crash():
    def ask(prompt):
        raise TimeoutError("timed out")
    assert run(FakeBrowser(), ask).status == "failed"


def test_answers_file_round_trip(tmp_path):
    path = tmp_path / "answers.yaml"
    path.write_text("files:\n  resume: cv.pdf\nanswers:\n- question: First name\n  answer: Alex\n- question: Sponsorship?\n"
                    "  answer: yes\n", encoding="utf-8")
    answers = apply.load_answers(path)
    assert answers["files"] == {"resume": str((tmp_path / "cv.pdf").resolve())}
    assert [(a["question"], a["answer"]) for a in answers["answers"]] == [
        ("First name", "Alex"), ("Sponsorship?", "Yes")]  # YAML read the bare `yes` as true


def test_questions_message_lists_them_with_suggestions_and_stays_short():
    text = apply.questions_message("Acme: Engineer", [{"question": "Why Acme?", "suggested": "Robots."},
                                                      {"question": "Notice period?", "suggested": ""}])
    assert text.startswith("Shortlist: Acme: Engineer needs 2 answers") and "1. Why Acme?\nSuggested: Robots." in text
    assert "Suggested: (none)" in text and "Questions page" in text
    long = apply.questions_message("Acme: Engineer", [{"question": "Q" * 300, "suggested": "S" * 400}] * 12)
    assert len(long) <= 3500 and long.endswith("Questions page of the web app.")


def test_a_new_question_is_stored_per_job_and_sent_then_used_once_confirmed(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("companies: []\nnotify: [telegram]\napply: {mode: auto}\n", encoding="utf-8")
    answers_file = "answers:\n- question: First name\n  answer: Alex\n"
    (tmp_path / "answers.yaml").write_text(answers_file, encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    conn = db.connect(cfg.db_path)
    db.upsert_jobs(conn, [Job(ats="greenhouse", slug="acme", job_id="1", company="Acme", title="Engineer", location="",
                              remote=False, url="https://jobs.example/1", description="")], "2026-11-12T10:00:00+00:00")
    job = {**JOB, "key": "greenhouse:acme:1", "rowid": 1}
    browser, sent = FakeBrowser(), []
    monkeypatch.setattr(apply, "Browser", lambda *a, **k: browser)
    monkeypatch.setattr(apply.Polite, "allowed", lambda self, url: True)
    monkeypatch.setattr(apply.notify, "send", lambda channel, text: sent.append((channel, text)))

    ask, _ = scripted(step("ask", questions=[{"question": "Why Example?", "suggested": "Robots.", "reusable": False}]))
    assert apply.apply_now(cfg, job, headless=True, ask=ask) == 0
    assert [(r["question"], r["suggested"], r["reuse"]) for r in db.open_questions(conn)] == [("Why Example?", "Robots.", 0)]
    assert len(sent) == 1 and sent[0][0] == "telegram" and "Why Example?" in sent[0][1]
    assert (tmp_path / "answers.yaml").read_text(encoding="utf-8") == answers_file  # your file is only ever read

    never = lambda prompt: pytest.fail("a job waiting for answers costs no agent calls")  # noqa: E731
    assert apply.apply_now(cfg, job, headless=True, ask=never) == 0 and len(sent) == 1

    db.answer_question(conn, db.open_questions(conn)[0]["id"], "I like robots.", False, "2026-11-12T11:00:00+00:00")
    ask, prompts = scripted(FILL, SUBMIT, step("done", reason="Thank you."))
    assert apply.apply_now(cfg, job, headless=True, ask=ask) == 0
    assert "- Why Example?: I like robots." in prompts[0] and "- First name: Alex" in prompts[0]
    assert db.job_row(conn, 1)["stage"] == "applied"  # sent in auto mode: the tracker knows


def test_the_queue_is_worked_through_in_one_window_and_each_job_keeps_its_result(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("companies: []\nnotify: [telegram]\n", encoding="utf-8")
    (tmp_path / "answers.yaml").write_text("answers:\n- question: First name\n  answer: Alex\n", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    conn = db.connect(cfg.db_path)
    titles = ["Sent", "Asks", "Waits", "Closed", "Later"]
    db.upsert_jobs(conn, [Job(ats="greenhouse", slug="acme", job_id=str(n), company="Acme", title=title, location="",
                              remote=False, url=f"https://jobs.example/{n}", description="")
                          for n, title in enumerate(titles, 1)], "2026-11-12T10:00:00+00:00")
    ids = {r["title"]: r["id"] for r in conn.execute("SELECT rowid AS id, title FROM jobs")}
    db.add_questions(conn, {"key": "greenhouse:acme:3", "company": "Acme", "title": "Waits", "url": ""},
                     [{"question": "Why Acme?", "suggested": ""}], "2026-11-12T10:00:00+00:00")
    assert db.queue_add(conn, [ids[t] for t in titles[:4]], "2026-11-12T10:01:00+00:00") == 4
    db.set_tracking(conn, ids["Later"], "2026-11-12T10:02:00+00:00", stage="applied")
    assert db.queue_add(conn, [ids["Later"], 999], "2026-11-12T10:03:00+00:00") == 0  # applied to already; no such job
    browser, sent, built = FakeBrowser(), [], []
    monkeypatch.setattr(apply, "Browser", lambda *a, **k: built.append(1) or browser)
    monkeypatch.setattr(apply.Polite, "allowed", lambda self, url: True)
    monkeypatch.setattr(apply.notify, "send", lambda channel, text: sent.append(text))
    ask, _ = scripted(FILL, SUBMIT, step("done", reason="Thank you."),                           # Sent
                      step("ask", questions=[{"question": "Notice period?", "suggested": "None"}]),  # Asks
                      step("give_up", reason="This posting is closed."))                          # Closed (Waits: no call)

    assert apply.apply_queued(cfg, headless=True, ask=ask, mode="auto") == 0
    assert {r["title"]: (r["status"], r["open"]) for r in db.queue_rows(conn)} == {
        "Sent": ("submitted", 0), "Asks": ("questions", 1), "Waits": ("questions", 1), "Closed": ("stopped", 0)}
    assert db.job_row(conn, ids["Sent"])["stage"] == "applied" and db.job_row(conn, ids["Asks"])["stage"] == "new"
    assert built == [1] and browser.opened == [f"https://jobs.example/{n}" for n in (1, 2, 4)]  # one window; 3 never opened
    assert browser.tabs == 1  # nothing was left for you to send, so no tab stays
    assert "Notice period?" in sent[0] and sent[1].startswith("Shortlist worked through 4 queued applications:")
    assert "- Acme: Sent: sent" in sent[1] and "- Acme: Closed: stopped (This posting is closed.)" in sent[1]

    # Nothing is tried again by itself, except a job whose questions have all been answered since.
    never = lambda prompt: pytest.fail("nothing to do")  # noqa: E731
    assert apply.apply_queued(cfg, headless=True, ask=never, mode="auto") == 0 and built == [1]
    db.answer_question(conn, db.open_questions(conn, "greenhouse:acme:2")[0]["id"], "None", True, "2026-11-12T11:00:00+00:00")
    ask, prompts = scripted(FILL, SUBMIT)
    assert apply.apply_queued(cfg, headless=True, ask=ask, mode="review") == 0
    row = next(r for r in db.queue_rows(conn) if r["title"] == "Asks")
    assert row["status"] == "ready" and "yours to press" in row["note"] and "- Notice period?: None" in prompts[0]
    assert browser.tabs == 2  # the filled-in form keeps its tab


def test_a_handed_over_job_keeps_its_tab_and_you_hear_of_it_at_once(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("companies: []\nnotify: [telegram]\n", encoding="utf-8")
    (tmp_path / "answers.yaml").write_text("answers:\n- question: First name\n  answer: Alex\n", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    conn = db.connect(cfg.db_path)
    db.upsert_jobs(conn, [Job(ats="greenhouse", slug="acme", job_id=str(n), company="Acme", title=title, location="",
                              remote=False, url=f"https://jobs.example/{n}", description="")
                          for n, title in ((1, "Code"), (2, "Closed"))], "2026-11-12T10:00:00+00:00")
    db.queue_add(conn, [1, 2], "2026-11-12T10:01:00+00:00")
    browser, sent = FakeBrowser(), []
    monkeypatch.setattr(apply, "Browser", lambda *a, **k: browser)
    monkeypatch.setattr(apply.Polite, "allowed", lambda self, url: True)

    def send(channel, text):
        sent.append(text)
        if len(sent) == 1:  # the message about the code goes out before the next job starts
            assert browser.opened == ["https://jobs.example/1"]
    monkeypatch.setattr(apply.notify, "send", send)
    ask, _ = scripted(SUBMIT, step("hand_over", reason="Type the security code the site emailed."),
                      step("give_up", reason="This posting is closed."))
    assert apply.apply_queued(cfg, ask=ask, mode="auto") == 0
    assert {r["title"]: (r["status"], r["note"]) for r in db.queue_rows(conn)} == {
        "Code": ("your_turn", "Type the security code the site emailed."), "Closed": ("stopped", "This posting is closed.")}
    assert db.job_row(conn, 1)["stage"] == "new"  # not sent until you type the code
    assert sent[0].startswith("Shortlist: Acme: Code waits for you in its tab") and "security code" in sent[0]
    assert "- Acme: Code: left open for you (Type the security code" in sent[1]
    assert browser.tabs == 2 and browser.waited == 1  # its tab stays, and the window waits for you

    # Without a window (headless) there is nothing to hand over: it is stopped.
    db.queue_add(conn, [1], "2026-11-12T11:00:00+00:00")
    ask, _ = scripted(step("hand_over", reason="Verify you are human."))
    assert apply.apply_queued(cfg, headless=True, ask=ask) == 0
    assert next(r for r in db.queue_rows(conn) if r["title"] == "Code")["status"] == "stopped"


def test_the_queue_skips_jobs_applied_to_meanwhile_and_stops_on_a_question_asked_again(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("companies: []\nnotify: [telegram]\n", encoding="utf-8")
    (tmp_path / "answers.yaml").write_text("answers:\n- question: First name\n  answer: Alex\n", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    conn = db.connect(cfg.db_path)
    db.upsert_jobs(conn, [Job(ats="greenhouse", slug="acme", job_id=str(n), company="Acme", title=title, location="",
                              remote=False, url=f"https://jobs.example/{n}", description="")
                          for n, title in ((1, "By hand"), (2, "Asks again"))], "2026-11-12T10:00:00+00:00")
    db.queue_add(conn, [1, 2], "2026-11-12T10:01:00+00:00")
    db.set_tracking(conn, 1, "2026-11-12T10:02:00+00:00", stage="applied")  # you applied on the site yourself
    db.add_questions(conn, {"key": "greenhouse:acme:2", "company": "Acme", "title": "Asks again", "url": ""},
                     [{"question": "Notice period?", "suggested": ""}], "2026-11-12T10:00:00+00:00")
    db.answer_question(conn, db.open_questions(conn)[0]["id"], "Two weeks", False, "2026-11-12T10:03:00+00:00")
    browser, sent = FakeBrowser(), []
    monkeypatch.setattr(apply, "Browser", lambda *a, **k: browser)
    monkeypatch.setattr(apply.Polite, "allowed", lambda self, url: True)
    monkeypatch.setattr(apply.notify, "send", lambda channel, text: sent.append(text))
    ask, prompts = scripted(step("ask", questions=[{"question": "Notice period?", "suggested": "Two weeks"}]))
    assert apply.apply_queued(cfg, headless=True, ask=ask, mode="auto") == 0
    assert browser.opened == ["https://jobs.example/2"] and len(prompts) == 1
    row = next(r for r in db.queue_rows(conn) if r["title"] == "Asks again")
    assert row["status"] == "stopped" and "asked again what you answered" in row["note"]
    assert not any(db.queue_waiting(r) for r in db.queue_rows(conn))  # the next start has nothing to do
    assert len(sent) == 1 and "asked again" in sent[0]  # the summary; no new-question message


def test_closing_the_window_stops_the_queue_and_the_rest_stays_queued(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("companies: []\n", encoding="utf-8")
    (tmp_path / "answers.yaml").write_text("answers:\n- question: First name\n  answer: Alex\n", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    conn = db.connect(cfg.db_path)
    db.upsert_jobs(conn, [Job(ats="greenhouse", slug="acme", job_id=str(n), company="Acme", title=f"Job {n}", location="",
                              remote=False, url=f"https://jobs.example/{n}", description="") for n in (1, 2, 3)],
                   "2026-11-12T10:00:00+00:00")
    db.queue_add(conn, [1, 2, 3], "2026-11-12T10:01:00+00:00")
    browser = FakeBrowser()
    monkeypatch.setattr(apply, "Browser", lambda *a, **k: browser)
    monkeypatch.setattr(apply.Polite, "allowed", lambda self, url: True)

    def ask(prompt):
        if "Job 2" in prompt:  # you close the window while the second form is being filled
            browser.closed = True
            raise RuntimeError("Target page, context or browser has been closed")
        return step("give_up", reason="This posting is closed.")
    assert apply.apply_queued(cfg, headless=True, ask=ask) == 0
    assert [r["status"] for r in db.queue_rows(conn)] == ["stopped", "queued", "queued"]


def test_missing_answers_file_is_empty(tmp_path):
    assert apply.load_answers(tmp_path / "answers.yaml") == {"files": {}, "answers": []}


def test_apply_now_refuses_without_answers(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("companies: []\n", encoding="utf-8")
    monkeypatch.setattr(apply, "Browser", lambda *a, **k: pytest.fail("no browser without answers"))
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.apply.mode == "review" and cfg.apply.answers_path == str(tmp_path / "answers.yaml")
    assert apply.apply_now(cfg, JOB) == 2
