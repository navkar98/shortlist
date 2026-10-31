# Shortlist

Fetch open jobs from configured companies and careers pages, pre-filter them with cheap rules, score new ones
against the user's resume with an LLM, and send a digest (Telegram). A local web app browses
matches and tracks applications. Notify only: never apply to anything automatically. See README.md for users and
CONTRIBUTING.md for contributors; this file holds the rules and verified facts for working on the code.

## Layout (flat modules in `shortlist/`)
- `__main__.py` CLI and the run pipeline (discover -> fetch -> store -> pre-filter -> details -> score -> notify).
- `config.py` pydantic config, company packs (`packs/*.yaml`), and config.yaml editing that keeps comments.
- `fetchers.py` ATS APIs (`FETCHERS`: greenhouse, lever, ashby, workday, workable, amazon); `html_boards.py` CSS
  recipes; `listings.py` SimplifyJobs lists; `details.py` descriptions for list-only jobs; `render.py` Playwright.
- `discover.py` careers page -> board or recipe (links, name guesses, JobPosting data, then a capped agent loop).
- `scoring.py` `BACKENDS` (claude_cli, codex_cli, anthropic_api, openai_api): prompt + JSON schema in, dict out.
- `notify.py` digest formatting and `SENDERS`; `db.py` SQLite; `polite.py` robots.txt + pacing; `prefilter.py`.
- `web.py` + `templates/` + `static/` the web app; `wizard.py` `init` and `check`; `install.py` (repo root) setup.

## Rules
1. Don't guess. Before writing a parser, call the endpoint for real (two companies), save the raw responses to
   `tests/fixtures/`, and parse only fields seen there. Check `--help` / docs of the installed version before using
   a flag or library feature. If something can't be verified, say so; don't assume.
2. Don't say something works unless you ran it. Run the tests (`python -m pytest -q`) and the code itself.
3. Fixtures: trimmed excerpts of real responses (< 120 KB each), scanned for secrets by `tests/test_fixtures_clean.py`.
4. Every request goes through `polite.Polite`: robots.txt (RFC 9309), ~1 request/second per host, the Shortlist
   User-Agent. Never work around 403/429/bot challenges. The browser renders only pages that return 200 with HTML
   but no readable content. `ignore_robots` is set only by the user, per board.
5. One company, page, batch, or channel failing is logged on one line and skipped; it never crashes a run.
6. Secrets come from the environment or `.env`; never log them or URLs that contain them.
7. Small and flat: plain functions and dicts, no ABCs, no frameworks. Ask before adding a dependency. Don't use the
   anthropic SDK (the API backend is plain httpx). No LangChain, agent frameworks, Scrapy, or Selenium.
8. Write for every user: no personal paths, names, or preferences in committed files. Personal settings live in the
   gitignored config.yaml, resume.md, .env, and CLAUDE.local.md.

## Verified facts
- `claude -p` loads CLAUDE.md from its cwd AND every parent folder (canary test), so the claude_cli scorer runs in
  `~/.shortlist/scorer_workdir`, outside the repo, and config refuses a workdir below any CLAUDE.md. The scorer passes
  `-p --output-format json --json-schema <schema> --permission-mode dontAsk --model <m> --no-session-persistence
  --tools ""` (never `--bare`), reads `structured_output`, checks `is_error` and the exit code, and removes
  ANTHROPIC_API_KEY from its environment. With `--tools ""` structured output still works (`stop_reason: tool_use`)
  and calls are ~5x cheaper.
- `codex exec` (codex-cli 0.165.0): `--output-schema FILE` constrains the final message, `-o FILE` writes it, `--sandbox
  read-only --ephemeral --skip-git-repo-check`, prompt on stdin with `-`. It loads AGENTS.md from its cwd (canary;
  not from parents outside a git repo). On Windows npm installs `codex.cmd`, so the path is resolved with
  `shutil.which`. 0.121 rejected a newer config model with "requires a newer version of Codex".
- Scoring in batches is order- and neighbor-sensitive: 12 jobs scored twice in reversed order moved 11.7 points on
  average (max 23) with the old prompt, 8.8 after asking to judge each job alone; the same job scored alone repeated
  its score (72/72, 80/80, 66/66, 38/30). Hence the solo re-check of jobs within 15 points of min_score or above.
  The scorer used to list "not a fit" items (seniority, years) as dealbreakers (431 of 700); the prompt now limits
  dealbreakers to what the preferences call dealbreakers. Re-scoring alone (2026-10-30): 6 jobs that fell from 80-84
  (batch) to 60-68 (alone, current prompt) scored 50-66 alone with the pre-revision prompt, so the old highs were
  batch inflation, not wording. A solo claude_cli (sonnet) call takes ~5-6 s.
- `claude -p` once exited 1 within 2 s using zero tokens for 31 calls in a row (the reason was cut off in the log);
  scoring stops after 3 failed calls in a row. A subprocess timeout reported a negative `timeout` after the computer
  slept mid-call.
- Closed postings: complete-list sources (Greenhouse, Lever, Ashby, Workable, SimplifyJobs) list every open job per
  read; 5 of 5 sampled "closed" Greenhouse jobs answered 404 on the single-job API, 5 of 5 "open" ones 200.
- Anthropic Messages API: POST `https://api.anthropic.com/v1/messages`, headers `x-api-key`, `anthropic-version:
  2023-06-01`; structured outputs via `output_config.format` (`json_schema`; objects need `additionalProperties:
  false`; no numeric/length bounds); 401 body `{"type":"error","error":{"type":...,"message":...}}` (seen live).
- OpenAI API: POST `https://api.openai.com/v1/chat/completions`, `response_format: {type: json_schema, json_schema:
  {name, schema, strict}}`, answer in `choices[0].message.content`, `refusal`, `finish_reason` (`length` = cut off).
  401 body `{"error":{"message":...}}`; gpt-5.4-mini scored 6 of 6 jobs (both seen live). Email, Slack, Discord, and
  OpenAI-compatible servers (Ollama, OpenRouter) were removed before release because they were never tried live.
- Telegram sendMessage: `chat_id`, `text` (1-4096 chars), `link_preview_options: {is_disabled: true}`; responses carry
  `ok` / `description`.
- Greenhouse list `boards-api.greenhouse.io/v1/boards/<slug>/jobs` (without `content`, ~12x smaller); single job
  `.../jobs/<id>` (also serves job-boards.eu boards). Lever `api.lever.co/v0/postings/<slug>?mode=json`, single
  `/<slug>/<id>`. Ashby `api.ashbyhq.com/posting-api/job-board/<slug>` (robots.txt answers 401 = allowed).
- Workday: POST `https://<tenant>.wdN.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs` with `{appliedFacets, limit:
  20, offset, searchText}`; `total` only on the first page; pages past `total` wrap to page 1; newest first; no
  descriptions (each job page has schema.org JobPosting). Locations can read "2 Locations".
- Workable: GET `apply.workable.com/api/v1/widget/accounts/<account>?details=true` (descriptions included). Amazon:
  GET `amazon.jobs/en/search.json?base_query&result_limit=100&offset&sort=recent` (newest 500 per query).
- SimplifyJobs listings.json fields: id, company_name, title, locations, url, active, is_visible, category, degrees,
  sponsorship, date_posted. No descriptions.
- api.smartrecruiters.com robots.txt disallows `*`, so there is no SmartRecruiters adapter. Meta's robots.txt
  disallows `/*cursor=`: Meta boards read one rendered page per search, waiting for the job selector.
- Waymo began answering 202 with an empty body after ~15 quick requests: a bot challenge, logged and skipped.
- Saved pages can contain the site's own API keys (one reached GitHub once): redact before committing.
- Discovery: measured on 100 careers pages, every site the agent solved took <= 5 calls (default cap 5); 63/100
  needed no agent; 92/100 resolved. Company check for guessed boards: Greenhouse/Workable/Workday names must share
  half their distinctive words with the careers page's name; Lever/Ashby need the full name in descriptions.
- Incremental fetching: paged boards stop at the first page whose jobs are all "done" (handled, or rejected by the
  current filters; see `done_keys`), with a full scan per board every 7 days, recorded only for boards that returned
  jobs. A pending match counts as still listed if seen within 48 hours.
- Windows: Python's `write_text` writes CRLF unless `newline="\n"`; config.yaml, resume.md, and .env are written
  with LF.
