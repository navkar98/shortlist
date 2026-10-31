import json
import subprocess
from pathlib import Path

import pytest

from shortlist import scoring
from shortlist.config import Scoring
from shortlist.fetchers import Job


def job(n):
    return Job(ats="lever", slug="acme", job_id=str(n), company="acme", title=f"ML Engineer {n}", location="NYC",
               remote=False, url="https://example.com", description=f"Build ranking models #{n}")


def result(key, score=80):
    return {"job_id": key, "score": score, "matched_skills": ["PyTorch"], "missing_skills": [], "dealbreakers": [],
            "one_line_reason": "Ranking role."}


@pytest.fixture
def cfg(tmp_path):
    return Scoring(claude_path="claude-test", model="sonnet", batch_size=3, timeout_seconds=42,
                   workdir=str(tmp_path / "wd"))


def fake_run(stdout, returncode=0, calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="boom" if returncode else "")
    return run


def test_claude_cli_command_env_and_output(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    calls = []
    stdout = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                         "structured_output": {"results": [result("lever:acme:1")]}})
    monkeypatch.setattr(subprocess, "run", fake_run(stdout, calls=calls))

    assert scoring.claude_cli("PROMPT", scoring.SCHEMA, cfg) == {"results": [result("lever:acme:1")]}
    [(cmd, kwargs)] = calls
    assert cmd[:2] == ["claude-test", "-p"]
    for flag, value in [("--output-format", "json"), ("--permission-mode", "dontAsk"), ("--model", "sonnet"),
                        ("--tools", "")]:
        assert cmd[cmd.index(flag) + 1] == value
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == scoring.SCHEMA
    assert "--bare" not in cmd and "--no-session-persistence" in cmd
    assert kwargs["input"] == "PROMPT" and kwargs["cwd"] == cfg.workdir and kwargs["timeout"] == 42
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]


@pytest.mark.parametrize("stdout, returncode", [
    ("", 1),
    (json.dumps({"is_error": True, "subtype": "error_max_turns", "result": "x"}), 0),
    (json.dumps({"is_error": False, "result": "plain text, no structured output"}), 0),
])
def test_claude_cli_raises_on_failure(cfg, monkeypatch, stdout, returncode):
    monkeypatch.setattr(subprocess, "run", fake_run(stdout, returncode))
    with pytest.raises(RuntimeError):
        scoring.claude_cli("PROMPT", scoring.SCHEMA, cfg)


def test_missing_or_invalid_results_are_retried_alone_once(cfg, monkeypatch):
    jobs = [job(1), job(2), job(3)]
    prompts = []

    def backend(prompt, schema, _cfg):
        prompts.append(prompt)
        if len(prompts) == 1:  # batch: job 1 valid, job 2 has an out-of-range score, job 3 missing
            return {"results": [result("lever:acme:1"), result("lever:acme:2", score=150)]}
        if '"lever:acme:2"' in prompt:
            return {"results": [result("lever:acme:2", score=55)]}
        return {"results": []}  # job 3 still missing when retried alone

    monkeypatch.setitem(scoring.BACKENDS, "claude_cli", backend)
    scores, failed = scoring.score_jobs(jobs, "RESUME", "PREFS", cfg)
    assert {k: s.score for k, s in scores.items()} == {"lever:acme:1": 80, "lever:acme:2": 55}
    assert failed == {"lever:acme:3"}
    assert len(prompts) == 3 and "RESUME" in prompts[0] and "PREFS" in prompts[0]


def test_claude_cli_error_names_the_reason_not_the_usage_counts(cfg, monkeypatch):
    stdout = json.dumps({"type": "result", "subtype": "success", "is_error": True, "duration_api_ms": 0,
                         "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0} | {
                             f"padding_{i}": 0 for i in range(30)}, "result": "Usage limit reached"})
    monkeypatch.setattr(subprocess, "run", fake_run(stdout, 1))
    with pytest.raises(RuntimeError, match="claude exited 1: Usage limit reached"):
        scoring.claude_cli("PROMPT", scoring.SCHEMA, cfg)
    monkeypatch.setattr(subprocess, "run", fake_run("not json", 1))
    with pytest.raises(RuntimeError, match="claude exited 1: boom"):  # stderr, when the output is not JSON
        scoring.claude_cli("PROMPT", scoring.SCHEMA, cfg)


def test_scoring_stops_when_calls_keep_failing(cfg, monkeypatch, caplog):
    calls = []

    def backend(prompt, schema, _cfg):
        calls.append(prompt)
        raise RuntimeError("claude exited 1: Usage limit reached")

    monkeypatch.setitem(scoring.BACKENDS, "claude_cli", backend)
    caplog.set_level("INFO", logger="shortlist.scoring")
    one_each = cfg.model_copy(update={"batch_size": 1})
    scores, failed = scoring.score_jobs([job(n) for n in range(1, 7)], "R", "P", one_each, recheck_from=0)
    assert len(calls) == 3 and scores == {} and failed == set()  # all six wait for the next run
    assert "stopping after 3 failed calls in a row: 3 jobs are left for the next run" in caplog.text


def test_failed_batch_is_logged_and_skipped(cfg, monkeypatch, caplog):
    jobs = [job(n) for n in range(1, 6)]  # batch_size 3 -> batches of 3 and 2

    def backend(prompt, schema, _cfg):
        if '"lever:acme:1"' in prompt:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=42)
        return {"results": [result("lever:acme:4"), result("lever:acme:5")]}

    monkeypatch.setitem(scoring.BACKENDS, "claude_cli", backend)
    caplog.set_level("INFO", logger="shortlist.scoring")
    scores, failed = scoring.score_jobs(jobs, "RESUME", "PREFS", cfg)
    assert set(scores) == {"lever:acme:4", "lever:acme:5"} and failed == set()  # batch 1 stays unscored for next run
    assert "batch 1/2: failed after 0s (timed out after 42s)" in caplog.text
    assert "batch 2/2: 2/2 jobs scored" in caplog.text


def test_schema_is_one_result_per_job():
    item = scoring.SCHEMA["properties"]["results"]["items"]
    assert set(item["required"]) == {"job_id", "score", "matched_skills", "missing_skills", "dealbreakers",
                                     "one_line_reason"}
    assert item["properties"]["score"]["minimum"] == 0 and item["properties"]["score"]["maximum"] == 100


def codex_run(answer, returncode=0, stderr="", calls=None):
    """Like `codex exec`: writes the final message to the -o file (verified with codex-cli 0.165.0)."""
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        if answer is not None:
            Path(cmd[cmd.index("-o") + 1]).write_text(answer, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, stdout=answer or "", stderr=stderr)
    return run


def test_codex_cli_command_env_and_output(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setattr(scoring.shutil, "which", lambda name: None)
    calls, schemas = [], []
    answer = json.dumps({"results": [result("lever:acme:1")]})

    def run(cmd, **kwargs):
        schemas.append(json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text(encoding="utf-8")))
        return codex_run(answer, calls=calls)(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    cfg = Scoring(backend="codex_cli", codex_path="codex-test", effort="low", timeout_seconds=42,
                  workdir=str(tmp_path / "wd"))
    assert scoring.codex_cli("PROMPT", scoring.SCHEMA, cfg) == {"results": [result("lever:acme:1")]}
    [(cmd, kwargs)] = calls
    assert cmd[:2] == ["codex-test", "exec"] and cmd[-1] == "-" and "--model" not in cmd  # Codex's own default
    for flag, value in [("--sandbox", "read-only"), ("--config", "model_reasoning_effort=low")]:
        assert cmd[cmd.index(flag) + 1] == value
    assert "--ephemeral" in cmd and "--skip-git-repo-check" in cmd
    assert schemas == [scoring.strict_schema(scoring.SCHEMA)]
    assert kwargs["input"] == "PROMPT" and kwargs["cwd"] == cfg.workdir and kwargs["timeout"] == 42
    assert "OPENAI_API_KEY" not in kwargs["env"]  # your Codex login, not API billing
    assert not Path(cmd[cmd.index("-o") + 1]).parent.exists()  # the temporary files are gone


@pytest.mark.parametrize("answer, returncode, message", [
    (None, 1, "codex exited 1: ERROR: The model requires a newer version of Codex."),
    (None, 0, "codex wrote no final message"),
])
def test_codex_cli_raises_on_failure(tmp_path, monkeypatch, answer, returncode, message):
    stderr = "model: x\nERROR: The model requires a newer version of Codex.\n"
    monkeypatch.setattr(subprocess, "run", codex_run(answer, returncode, stderr))
    with pytest.raises(RuntimeError, match=message):
        scoring.codex_cli("PROMPT", scoring.SCHEMA, Scoring(backend="codex_cli", workdir=str(tmp_path / "wd")))


def test_jobs_near_or_above_the_line_are_scored_again_alone(cfg, monkeypatch):
    calls = []

    def backend(prompt, schema, cfg_):
        batch = json.loads(prompt.split("<jobs>\n", 1)[1].split("\n</jobs>", 1)[0])
        calls.append([j["job_id"] for j in batch])
        # in a batch the first job is inflated, alone it gets its honest score
        honest = {"lever:acme:1": 62, "lever:acme:2": 50, "lever:acme:3": 20}
        return {"results": [result(j["job_id"], honest[j["job_id"]] + (20 if len(batch) > 1 and i == 0 else 0))
                            for i, j in enumerate(batch)]}

    monkeypatch.setitem(scoring.BACKENDS, "claude_cli", backend)
    scores, failed = scoring.score_jobs([job(1), job(2), job(3)], "R", "P", cfg, recheck_from=55)
    assert calls == [["lever:acme:1", "lever:acme:2", "lever:acme:3"], ["lever:acme:1"]]  # only the 82 is re-checked
    assert {k: s.score for k, s in scores.items()} == {"lever:acme:1": 62, "lever:acme:2": 50, "lever:acme:3": 20}
    calls.clear()
    scoring.score_jobs([job(1), job(2), job(3)], "R", "P", cfg)  # recheck_from=None: no re-check
    assert calls == [["lever:acme:1", "lever:acme:2", "lever:acme:3"]]


def test_rated_jobs_reach_the_prompt_as_examples():
    examples = [{"title": "Research Scientist, Ranking", "company": "Meta", "location": "Menlo Park", "score": 62,
                 "good": True, "disagrees": True},
                {"title": "Ads Engineer", "company": "Acme", "location": "", "score": 80, "good": False, "disagrees": True}]
    prompt = scoring.build_prompt([job(1)], "R", "P", examples)
    block = prompt.split("<feedback>\n", 1)[1].split("\n</feedback>", 1)[0]
    assert block.endswith("- good fit: Research Scientist, Ranking at Meta (Menlo Park); scored 62\n"
                          "- poor fit: Ads Engineer at Acme; scored 80")
    assert prompt.index("</preferences>") < prompt.index("<feedback>") < prompt.index("<jobs>")
    assert "<feedback>" not in scoring.build_prompt([job(1)], "R", "P")


def test_your_own_instructions_replace_the_built_in_ones(cfg, monkeypatch):
    prompts = []
    monkeypatch.setitem(scoring.BACKENDS, "claude_cli", lambda prompt, schema, cfg_: prompts.append(prompt) or
                        {"results": [result("lever:acme:1")]})
    scoring.score_jobs([job(1)], "R", "P", cfg)
    scoring.score_jobs([job(1)], "R", "P", cfg.model_copy(update={"instructions": "Score strictly.\n"}))
    assert prompts[0].startswith(scoring.INSTRUCTIONS) and prompts[1].startswith("Score strictly.\n\n<resume>")
    assert prompts[1].split("<resume>", 1)[1] == prompts[0].split("<resume>", 1)[1]  # everything after it is the same


def test_fingerprint_changes_with_the_resume_preferences_or_prompt():
    base = scoring.fingerprint("R", "P", "I")
    assert base == scoring.fingerprint(" R\n", "P\n", "I")  # surrounding whitespace is not a change
    assert scoring.fingerprint("R", "P", "one two") == scoring.fingerprint("R", "P", "one\ntwo")  # nor a reflowed line
    assert len({base, scoring.fingerprint("R2", "P", "I"), scoring.fingerprint("R", "P2", "I"),
                scoring.fingerprint("R", "P", "I2")}) == 4
