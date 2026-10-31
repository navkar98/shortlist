# Contributing to Shortlist

Thanks for helping. Shortlist is small on purpose: a handful of flat modules, a few dependencies, and tests built on
real responses. The rules below keep it that way and keep it polite to the sites it reads.

## Setup

```bash
git clone https://github.com/navkar98/shortlist.git
cd shortlist
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt        # Windows: .venv\Scripts\pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests make no network calls and need no browser, keys, or config. CI runs them on Windows, macOS, and Linux.

## Ground rules

1. **Build on what you saw, not what you expect.** Before writing a parser for an API or page, call it for real (at
   least two companies), save the raw responses to `tests/fixtures/`, and read only fields that appear there. The same
   goes for CLI flags and library behavior: check the docs or `--help` of the installed version. If you could not
   verify something, say so in the pull request.
2. **Trim and clean fixtures.** Keep a few real entries of a saved response, enough for the cases your test checks,
   and drop scripts, styles, and unrelated markup from saved pages (each file stays under 120 KB). Saved pages
   sometimes contain the site's own API keys: `tests/test_fixtures_clean.py` scans for common ones, but look before
   committing.
3. **Stay polite.** Every request goes through `polite.Polite` (robots.txt per RFC 9309, about one request per second
   per host, the Shortlist User-Agent). Never add code that works around a 403, a 429, or a bot challenge; the headless
   browser is only for pages that load fine but need JavaScript. Sites whose terms forbid automated access (LinkedIn,
   Indeed, Glassdoor, search engines) are out of scope.
4. **Never leak secrets.** Keys and tokens come from the environment or `.env`. Error messages and logs must not
   contain them, nor URLs that embed them (such as Telegram bot URLs).
5. **Fail small.** One company, page, batch, or channel failing is logged on one line and skipped; it never stops a
   run.
6. **Keep it flat and small.** Plain functions and dicts (`FETCHERS`, `BACKENDS`, `SENDERS`), no abstract base
   classes, no frameworks. Please open an issue before adding a dependency.
7. **Write for every user.** No personal paths, names, or preferences in code, docs, or examples.

## Common contributions

### Add companies to a pack, or a new pack

Packs live in `packs/<name>.yaml`:

```yaml
description: >-
  One sentence on what the pack covers and when it was checked.
companies:
  - greenhouse:acme          # Acme (checked 2026-10-20: board returns jobs)
career_pages:
  - {name: "Acme AI", url: "https://acme.ai/careers"}
```

Check each entry before adding it: a board entry must return jobs (`python -m shortlist run --dry-run` with a
config holding just that entry, or open the API URL), and a careers page must load and be allowed by its robots.txt.
`tests/test_config.py` loads every bundled pack.

### Add a job board (ATS) adapter

1. Save real list responses for two companies to `tests/fixtures/<ats>_<slug>.json`, trimmed.
2. Write `parse_<ats>(data, slug) -> list[Job]` and `fetch_<ats>(polite, slug, known=None)` in `fetchers.py`, and
   register the fetcher in `FETCHERS`. Paged boards should stop early when a page has nothing new (see
   `nothing_new`).
3. `tests/test_fetchers.py` parses every fixture whose name starts with a known ATS; add tests for anything special
   (remote detection, locations, paging).
4. If careers pages link to this ATS, add its URL pattern to `SIGNATURES` in `discover.py`.

### Add a scoring backend

A backend is a function `(prompt: str, schema: dict, cfg: Scoring) -> dict` that returns the JSON answer and raises
on any failure. Register it in `BACKENDS` in `scoring.py` (plus `DEFAULT_MODELS` and `KEY_ENV` if it has them), add
it to the wizard's choices in `wizard.py`, document it in the README, and test it with `httpx.MockTransport` against
the response shapes in the provider's documentation (`tests/test_backends.py`). Discovery uses the same function.

### Add a notification channel

Add its message size to `LIMITS`, its environment variables to `REQUIRED_ENV`, and a sender to `SENDERS` in
`notify.py`; then the wizard question in `wizard.py`, a section in `.env.example`, a row in the README, and tests
showing that errors never contain the secret.

## Pull requests

- One topic per pull request, with tests. Run `python -m pytest -q` before pushing.
- Describe what you verified for real (which endpoint, which page, which version) and what you could not verify.
- Match the surrounding code: names, comment density, and line length (about 120).
