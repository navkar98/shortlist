"""Score jobs against the resume through a pluggable backend: a prompt and a JSON schema in, a JSON object out.

- claude_cli / codex_cli: the Claude Code or OpenAI Codex CLI, using your subscription or login. With
  `-p --output-format json --json-schema`, claude prints one JSON object whose `structured_output` holds the answer,
  plus `is_error` and `subtype`.
- anthropic_api: the Messages API over plain HTTP (no SDK), with structured outputs (`output_config.format`).
- openai_api: the OpenAI API's `/chat/completions` with a `json_schema` response format, over plain HTTP.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, ValidationError

from .config import Scoring
from .fetchers import Job

log = logging.getLogger(__name__)


class JobScore(BaseModel):
    job_id: str
    score: int = Field(ge=0, le=100)
    matched_skills: list[str]
    missing_skills: list[str]
    dealbreakers: list[str]
    one_line_reason: str


SCHEMA = {"type": "object", "required": ["results"],
          "properties": {"results": {"type": "array", "items": JobScore.model_json_schema()}}}

# One rule per line (a backslash joins two source lines), so the text reads well when edited in Settings.
INSTRUCTIONS = """You screen job postings for one candidate. Score each job from 0 to 100 for how well it fits the \
candidate's resume and preferences: 85-100 apply now, 70-84 strong fit, 40-69 partial fit, 0-39 poor fit.
Judge each job on its own, against the resume and preferences only: never compare jobs with each other, and don't \
spread the scores across the list. The same job must get the same score whatever else is in the list.
Judge role type, seniority, domain, required skills, location and work authorization against the preferences.
Dealbreakers are only what the preferences call dealbreakers. If one applies, name it in dealbreakers and score the \
job 20 or lower. Other mismatches (a level, years of experience, or domain the preferences call a poor fit) lower the \
score through the bands above and belong in one_line_reason, not in dealbreakers.
Some jobs have only a short listing note instead of a full description; score those on title, company, location, \
and the note, and say in one_line_reason that the description was not available.
Return exactly one result per job, copying its job_id exactly. Keep one_line_reason under 25 words, and refer to \
the candidate without gendered pronouns.
Use only the text below."""

FEEDBACK = """How the candidate judged some jobs scored earlier. Read them as evidence of preferences beyond the \
resume and the preferences above: when a job below is like one of these, let that judgment move its score. A judgment \
that disagrees with its earlier score shows where scoring went wrong."""


def fingerprint(resume: str, preferences: str, instructions: str) -> str:
    """Identifies what a job was scored against, so jobs scored with an earlier resume or prompt can be found. Spacing
    and line breaks don't count: reflowing a paragraph changes nothing for the model."""
    parts = (" ".join(s.split()) for s in (resume, preferences, instructions))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:12]


def build_prompt(jobs: list[Job], resume: str, preferences: str, feedback: list[dict] = (),
                 instructions: str = INSTRUCTIONS) -> str:
    payload = [{"job_id": j.key, "company": j.company, "title": j.title, "location": j.location, "remote": j.remote,
                "description": j.description} for j in jobs]
    examples = "".join(f"\n- {'good fit' if e['good'] else 'poor fit'}: {e['title']} at {e['company']}"
                       + (f" ({e['location']})" if e["location"] else "") + f"; scored {e['score']}" for e in feedback)
    return (f"{instructions.strip()}\n\n<resume>\n{resume.strip()}\n</resume>\n\n<preferences>\n{preferences.strip()}\n"
            f"</preferences>\n\n" + (f"<feedback>\n{FEEDBACK}{examples}\n</feedback>\n\n" if feedback else "")
            + f"<jobs>\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n</jobs>\n")


def claude_cli(prompt: str, schema: dict, cfg: Scoring) -> dict:
    """Run one `claude -p` call and return its `structured_output`. Raises on any failure."""
    Path(cfg.workdir).mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cmd = [cfg.claude_path, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
           "--permission-mode", "dontAsk", "--model", cfg.model, "--no-session-persistence", "--tools", ""]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=cfg.workdir,
                          env=env, timeout=cfg.timeout_seconds,
                          # Windows: don't pop up a console window when started from the windowless scheduled task
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {_claude_error(proc)}")
    out = json.loads(proc.stdout)
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"no structured output (subtype={out.get('subtype')}): {str(out.get('result'))[:300]}")
    return out["structured_output"]


def _claude_error(proc: subprocess.CompletedProcess) -> str:
    """What claude said went wrong: with --output-format json that is the answer's `result` (the usage counts come
    first and would push it past any length cap), else the start of its output."""
    try:
        out = json.loads(proc.stdout)
        reason = out.get("result") or out.get("subtype")
    except (ValueError, AttributeError):
        reason = None
    return str(reason or (proc.stderr or proc.stdout).strip())[:300]


def codex_cli(prompt: str, schema: dict, cfg: Scoring) -> dict:
    """Run one `codex exec` call (read-only sandbox, no saved session) whose final message must match `schema`.

    `--output-schema FILE` constrains the final message and `-o FILE` writes it (codex-cli 0.165). Codex reads
    AGENTS.md from its working folder, so it runs in the same empty workdir as claude_cli.
    """
    Path(cfg.workdir).mkdir(parents=True, exist_ok=True)
    # Your Codex login, not API billing: API calls are what the openai_api backend is for.
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
    with tempfile.TemporaryDirectory() as tmp:  # outside the workdir, which codex can read
        schema_file, answer_file = Path(tmp) / "schema.json", Path(tmp) / "answer.json"
        schema_file.write_text(json.dumps(strict_schema(schema)), encoding="utf-8")
        # npm installs codex as codex.cmd on Windows, which only a PATH lookup finds without the extension
        cmd = [shutil.which(cfg.codex_path) or cfg.codex_path, "exec", "--output-schema", str(schema_file),
               "-o", str(answer_file),
               "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "--color", "never"]
        if cfg.model:
            cmd += ["--model", cfg.model]
        if cfg.effort:  # a bare word is not valid TOML, so codex takes it as a string
            cmd += ["--config", f"model_reasoning_effort={cfg.effort}"]
        proc = subprocess.run([*cmd, "-"], input=prompt, capture_output=True, text=True, encoding="utf-8",
                              cwd=cfg.workdir, env=env, timeout=cfg.timeout_seconds,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            errors = [line for line in proc.stderr.splitlines() if line.startswith("ERROR")] or [proc.stderr.strip()]
            raise RuntimeError(f"codex exited {proc.returncode}: {errors[-1][:300]}")
        if not answer_file.exists():
            raise RuntimeError("codex wrote no final message")
        return _json(answer_file.read_text(encoding="utf-8"))


# Keywords strict structured-output modes reject (pydantic still checks bounds on the answer); titles and defaults
# are just noise for them.
_UNSUPPORTED = {"title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                "minLength", "maxLength", "minItems", "maxItems"}


def strict_schema(node):
    """The schema in the form strict modes accept: every object closed (`additionalProperties: false`) with all of
    its properties required, and without unsupported keywords."""
    if isinstance(node, list):
        return [strict_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key in ("properties", "$defs"):  # their keys are names (a property may be called "title"), not keywords
            out[key] = {name: strict_schema(sub) for name, sub in value.items()}
        elif key not in _UNSUPPORTED:
            out[key] = strict_schema(value)
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out.get("properties", {}))
    return out


def api_key(cfg: Scoring) -> str:
    return os.environ.get(KEY_ENV.get(cfg.backend, ""), "")


def needs_key(cfg: Scoring) -> bool:
    return cfg.backend in KEY_ENV


def _post(url: str, headers: dict, body: dict, cfg: Scoring, transport: httpx.BaseTransport | None) -> dict:
    with httpx.Client(timeout=cfg.timeout_seconds, transport=transport) as client:
        r = client.post(url, headers=headers, json=body)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200:
        error = data.get("error") if isinstance(data, dict) else None
        message = error.get("message") if isinstance(error, dict) else error or r.text
        raise RuntimeError(f"HTTP {r.status_code}: {str(message).strip()[:300]}")
    return data


def _json(text: str) -> dict:
    """Parse the answer, also when it comes wrapped in a ```json fence."""
    return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# Models that accept the server-side refusal fallback (beta) in its "default" mode: a declined request is re-run on
# a fallback model chosen by refusal category, in the same call.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


def anthropic_api(prompt: str, schema: dict, cfg: Scoring, *, transport: httpx.BaseTransport | None = None) -> dict:
    """One Messages API call whose answer must match `schema` (structured outputs). Raises on any failure."""
    key = api_key(cfg)
    if not key:
        raise RuntimeError(f"{KEY_ENV['anthropic_api']} is not set (in the environment or .env)")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    body = {"model": cfg.model, "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": strict_schema(schema)}}}
    if cfg.effort:
        body["output_config"]["effort"] = cfg.effort
    if cfg.model in FALLBACK_MODELS:
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        body["fallbacks"] = "default"
    data = _post(ANTHROPIC_URL, headers, body, cfg, transport)
    if data.get("stop_reason") in ("refusal", "max_tokens"):
        raise RuntimeError(f"no answer (stop_reason={data['stop_reason']})")
    return _json("".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"))


OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def openai_api(prompt: str, schema: dict, cfg: Scoring, *, transport: httpx.BaseTransport | None = None) -> dict:
    """One `/chat/completions` call with a strict `json_schema` response format. Raises on any failure."""
    key = api_key(cfg)
    if not key:
        raise RuntimeError(f"{KEY_ENV['openai_api']} is not set (in the environment or .env)")
    body = {"model": cfg.model, "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer", "schema": strict_schema(schema), "strict": True}}}
    data = _post(OPENAI_URL, {"Authorization": f"Bearer {key}"}, body, cfg, transport)
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if message.get("refusal") or choice.get("finish_reason") == "length":
        raise RuntimeError(f"no answer (finish_reason={choice.get('finish_reason')}, refusal={message.get('refusal')})")
    return _json(message.get("content") or "")


# backend name -> function(prompt, schema, scoring config) -> structured output dict
BACKENDS = {"claude_cli": claude_cli, "codex_cli": codex_cli, "anthropic_api": anthropic_api,
            "openai_api": openai_api}
# codex_cli: empty means the model in your Codex config; openai_api: always named
DEFAULT_MODELS = {"claude_cli": "sonnet", "codex_cli": "", "anthropic_api": "claude-opus-5-5"}
KEY_ENV = {"anthropic_api": "ANTHROPIC_API_KEY", "openai_api": "OPENAI_API_KEY"}


def _describe(e: Exception) -> str:
    if isinstance(e, subprocess.TimeoutExpired):  # its timeout can be negative (seen after the computer slept)
        return f"timed out after {e.timeout:.0f}s" if e.timeout > 0 else "timed out"
    return f"{type(e).__name__}: {e}".splitlines()[0][:300]


def _run_batch(jobs: list[Job], resume: str, preferences: str, cfg: Scoring, label: str,
               feedback: list[dict] = ()) -> dict[str, JobScore] | None:
    """Valid scores by job key, or None if the call itself failed."""
    started = time.monotonic()
    try:
        prompt = build_prompt(jobs, resume, preferences, feedback, cfg.instructions or INSTRUCTIONS)
        output = BACKENDS[cfg.backend](prompt, SCHEMA, cfg)
    except Exception as e:  # one batch failing must never crash the run
        log.warning("%s: failed after %.0fs (%s)", label, time.monotonic() - started, _describe(e))
        return None
    wanted, valid = {j.key for j in jobs}, {}
    results = output.get("results")
    for item in results if isinstance(results, list) else []:
        try:
            result = JobScore.model_validate(item)
        except ValidationError:
            continue
        if result.job_id in wanted:
            valid[result.job_id] = result
    log.info("%s: %d/%d jobs scored in %.0fs", label, len(valid), len(jobs), time.monotonic() - started)
    return valid


# Failed calls in a row after which the backend is taken to be down or out of quota: the jobs left wait for the next
# run instead of each failing in turn (seen: a usage limit failed 31 calls in a row, 2 seconds apart).
MAX_FAILED_IN_A_ROW = 3


def score_jobs(jobs: list[Job], resume: str, preferences: str, cfg: Scoring, *, feedback: list[dict] = (),
               recheck_from: int | None = None, on_scores=None) -> tuple[dict[str, JobScore], set[str]]:
    """Score in batches. Returns scores by job key, and the keys still missing or invalid after one solo retry.

    Jobs whose call failed outright (timeout, crash) are in neither, so the next run tries them again. In a batch, a
    job's score moves with the other jobs and their order (measured: 12 points on average, up to 23), while the same
    job scored alone gets the same score again; so jobs scoring `recheck_from` or more are scored again alone, and
    that score is the one kept. `on_scores`, when given, gets each call's scores as they come, to save them as it goes.
    """
    on_scores = on_scores or (lambda got: None)
    scores, failed, alone = {}, set(), set()
    batches = [jobs[i:i + cfg.batch_size] for i in range(0, len(jobs), cfg.batch_size)]
    in_a_row = 0
    for n, batch in enumerate(batches, 1):
        if in_a_row == MAX_FAILED_IN_A_ROW:
            log.warning("stopping after %d failed calls in a row: %d jobs are left for the next run", in_a_row,
                        sum(map(len, batches[n - 1:])))
            break
        got = _run_batch(batch, resume, preferences, cfg, f"batch {n}/{len(batches)}", feedback)
        in_a_row = 0 if got is not None else in_a_row + 1
        if got is None:
            continue
        scores.update(got)
        on_scores(got)
        alone |= set(got) if len(batch) == 1 else set()
        for job in batch:
            if job.key in got:
                continue
            retry = _run_batch([job], resume, preferences, cfg, f"retry {job.key}", feedback)
            if retry is None:
                continue
            if job.key in retry:
                scores.update(retry)
                on_scores(retry)
                alone.add(job.key)
            else:
                failed.add(job.key)
                log.warning("%s: no valid score after retrying alone; skipping it", job.key)
    if recheck_from is not None and in_a_row < MAX_FAILED_IN_A_ROW:
        close = [j for j in jobs if j.key in scores and j.key not in alone and scores[j.key].score >= recheck_from]
        changes = []
        for n, job in enumerate(close, 1):
            if in_a_row == MAX_FAILED_IN_A_ROW:
                log.warning("stopping re-checks after %d failed calls in a row", in_a_row)
                break
            got = _run_batch([job], resume, preferences, cfg, f"re-check {n}/{len(close)}", feedback)
            in_a_row = 0 if got is not None else in_a_row + 1
            if got and job.key in got:
                changes.append(got[job.key].score - scores[job.key].score)
                scores[job.key] = got[job.key]
                on_scores(got)
        if close:
            log.info("re-checked %d jobs scoring %d+ alone: %d changed, by %.1f points on average", len(close),
                     recheck_from, sum(1 for c in changes if c), sum(abs(c) for c in changes) / max(1, len(changes)))
    return scores, failed
