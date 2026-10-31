"""`python -m shortlist init`: first-time setup by questions. `python -m shortlist check`: is everything ready?

init never overwrites what you already have: an existing config.yaml, resume.md, or .env value is kept.
"""
import getpass
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from . import db, notify
from .config import Config, available_packs, load_config, replace_sections, with_packs
from .scoring import BACKENDS, DEFAULT_MODELS, KEY_ENV, _describe, api_key, needs_key

ROOT = Path(__file__).resolve().parents[1]
RESUME_MARKER = "<!-- shortlist: replace this template with your resume -->"
RESUME_TEMPLATE = f"""{RESUME_MARKER}
# Your Name

Paste your resume here as plain text or Markdown: summary, experience, education, skills, and publications.
The scorer reads this file together with `preferences` in config.yaml. You can also edit it in the web app
(python -m shortlist web, Settings page).
"""
LABELS = {True: " ok ", False: "FAIL", None: "note"}
CHECK_SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


def ask(question: str, default: str = "", *, yes: bool = False, secret: bool = False) -> str:
    if yes:
        return default
    prompt = f"{question}{f' [{default}]' if default and not secret else ''}: "
    try:  # getpass reads the console directly, so piped input (and tests) use input()
        answer = getpass.getpass(prompt) if secret and sys.stdin.isatty() else input(prompt)
    except EOFError:
        answer = ""
    return answer.strip() or default


def confirm(question: str, default: bool, *, yes: bool) -> bool:
    answer = ask(f"{question} [{'Y/n' if default else 'y/N'}]", yes=yes).lower()
    return default if not answer else answer.startswith("y")


def number(question: str, default: int, *, yes: bool) -> int:
    while not (answer := ask(question, str(default), yes=yes)).isdigit():
        print("  Type a whole number.")
    return int(answer)


def choose(question: str, options: dict[str, str], default: str, *, yes: bool) -> str:
    keys = list(options)
    print(question)
    for i, key in enumerate(keys, 1):
        print(f"  {i}. {options[key]}")
    while True:
        answer = ask("Number", str(keys.index(default) + 1), yes=yes)
        if answer.isdigit() and 1 <= int(answer) <= len(keys):
            return keys[int(answer) - 1]
        print("  Type one of the numbers above.")


def terms(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\n]", text) if t.strip()]


def find_claude() -> str:
    """The Claude Code CLI: on PATH, or where its native installer puts it."""
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    return str(local) if local.exists() else ""


def describe_sources(sources, *, every_kind: bool = False) -> str:
    """"6 job boards, 99 careers pages, ..." for a Config or a Pack."""
    counts = [(len(getattr(sources, attr)), one, many) for attr, one, many in (
        ("companies", "job board", "job boards"), ("career_pages", "careers page", "careers pages"),
        ("html_boards", "page search", "page searches"), ("github_listings", "GitHub job list", "GitHub job lists"))]
    return ", ".join(f"{n} {one if n == 1 else many}" for n, one, many in counts if n or every_kind)


def set_env(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in .env (created from .env.example if missing), keeping every other line."""
    source = path if path.exists() else ROOT / ".env.example"
    text = source.read_text(encoding="utf-8") if source.exists() else ""
    for key, value in values.items():
        line = f'{key}="{value}"' if re.search(r"[\s#\"']", value) else f"{key}={value}"
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
        text = pattern.sub(lambda m: line, text, count=1) if pattern.search(text) else f"{text.rstrip()}\n{line}\n"
    path.write_text(text, encoding="utf-8", newline="\n")


# --- init ------------------------------------------------------------------------------------------------------------

def _scoring(starter: dict, *, yes: bool, env: dict) -> dict:
    claude, codex = find_claude(), shutil.which("codex") or ""
    have_anthropic, have_openai = bool(os.environ.get("ANTHROPIC_API_KEY")), bool(os.environ.get("OPENAI_API_KEY"))
    default = "claude_cli" if claude else "codex_cli" if codex else "anthropic_api" if have_anthropic else \
        "openai_api" if have_openai else "claude_cli"
    backend = choose("\nWhich model should score jobs?", {
        "claude_cli": "Claude Code CLI: uses your Claude subscription, no API key" +
                      (f" (found {claude})" if claude else " (not found; install Claude Code first)"),
        "codex_cli": "OpenAI Codex CLI: uses your ChatGPT or Codex login, no API key" +
                     (f" (found {codex})" if codex else " (not found; install Codex first)"),
        "anthropic_api": "Anthropic API: pay per use with an API key",
        "openai_api": "OpenAI API: pay per use with an API key",
    }, default, yes=yes)
    scoring, key_env = {"backend": backend}, ""
    if backend == "claude_cli":
        scoring["model"] = ask("Model (sonnet, opus, haiku, or a full model name)", DEFAULT_MODELS["claude_cli"], yes=yes)
        scoring["claude_path"] = ask("Path to the claude executable", claude or "claude", yes=yes)
    elif backend == "codex_cli":
        scoring["model"] = ask("Model (blank: the one in your Codex config)", "", yes=yes)
        scoring["codex_path"] = ask("Path to the codex executable", codex or "codex", yes=yes)
    elif backend == "anthropic_api":
        scoring["model"] = ask("Model", DEFAULT_MODELS["anthropic_api"], yes=yes)
        key_env = KEY_ENV["anthropic_api"]
    else:
        while not (model := ask("Model name, as OpenAI writes it", "", yes=yes)) and not yes:
            print("  A model name is required for this backend.")
        scoring["model"] = model or "set-a-model-name"
        key_env = KEY_ENV["openai_api"]
    if backend not in ("claude_cli", "codex_cli"):
        scoring["max_jobs_per_run"] = number("Most jobs to score per run (API calls cost money; 0 = no cap)", 100,
                                             yes=yes)
    scoring.update({key: value for key, value in starter.items() if key in ("batch_size", "timeout_seconds")})
    if backend in ("claude_cli", "codex_cli"):
        scoring["workdir"] = starter.get("workdir", "~/.shortlist/scorer_workdir")
    if key_env and not os.environ.get(key_env):
        key = ask(f"{key_env} (input hidden; blank: set it later in .env)", "", yes=yes, secret=True)
        if key:
            env[key_env] = key
    return scoring


def _notifications(*, yes: bool, env: dict) -> list[str]:
    channel = choose("\nWhere should new matches be sent?", {
        "none": "Nowhere: I'll look in the web app", "telegram": "Telegram (a bot you create with @BotFather)",
    }, "none", yes=yes)
    questions = {
        "telegram": [("TELEGRAM_BOT_TOKEN", "Bot token from @BotFather", "", True),
                     ("TELEGRAM_CHAT_ID", "Your chat id (see README: Telegram)", "", False)],
    }.get(channel, [])
    for name, question, default, secret in questions:
        if not os.environ.get(name) and (value := ask(f"{question} (blank: set {name} later in .env)", default,
                                                      yes=yes, secret=secret)):
            env[name] = value
    return [] if channel == "none" else [channel]


def _packs(default: list[str], *, yes: bool) -> list[str]:
    packs = available_packs()
    if not packs:
        return []
    print("\nCompany packs add ready-made lists of companies to follow:")
    names = list(packs)
    for i, (name, pack) in enumerate(packs.items(), 1):
        print(f"  {i}. {name}: {pack.description} ({describe_sources(pack)})")
    shown = ",".join(str(names.index(n) + 1) for n in default if n in names) or "none"
    while True:
        answer = ask("Packs to use (numbers separated by commas, or none)", shown, yes=yes)
        picks = [p.strip() for p in answer.split(",") if p.strip() and answer.strip().lower() != "none"]
        if all(p.isdigit() and 1 <= int(p) <= len(names) for p in picks):
            return list(dict.fromkeys(names[int(p) - 1] for p in picks))
        print("  Type numbers from the list, separated by commas.")


def _schedule(config_path: Path, *, yes: bool) -> None:
    if not confirm("\nRun Shortlist automatically in the background (at most twice a day, see `schedule`)?", False,
                   yes=yes):
        return
    if os.name == "nt":
        script = ROOT / "scripts" / "schedule_windows.ps1"
        result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                 "-Config", str(config_path)], capture_output=True, text=True)
        print((result.stdout or result.stderr).strip())
        return
    python = Path(sys.executable)
    print("Add this line with `crontab -e` (it checks hourly; runs happen only as `schedule` in config.yaml allows):\n"
          f"  0 * * * * cd {ROOT} && {python} -m shortlist run --if-due --config {config_path} "
          f"--log-file {ROOT / 'shortlist.log'}")


def init(config_path: Path, *, yes: bool = False) -> int:
    config_path = config_path.resolve()
    folder, env = config_path.parent, {}
    print("Shortlist setup. Press Enter to accept the [default]; you can change everything later in config.yaml or "
          "in the web app's Settings page.")
    if config_path.exists():
        print(f"\nKeeping your {config_path.name} as it is.")
    else:
        text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        starter = yaml.safe_load(text)
        filters = starter.get("filters", {})
        print("\nPre-filters drop jobs before any model call. Matching ignores case and uses whole words.")
        values = {"filters": {
            "title_include": terms(ask("Job titles or keywords to look for, comma-separated",
                                       ", ".join(filters.get("title_include", [])), yes=yes)),
            "title_exclude": terms(ask("Title words that rule a job out", ", ".join(filters.get("title_exclude", [])),
                                       yes=yes)),
            "locations": terms(ask("Places you can work: countries, states, cities (blank: anywhere)",
                                   ", ".join(filters.get("locations", [])), yes=yes)),
            "allow_remote": confirm("Keep remote jobs that don't name a place?", filters.get("allow_remote", True),
                                    yes=yes),
        }}
        values["packs"] = _packs(starter.get("packs", []), yes=yes)
        about = ask("\nIn a few sentences: the roles you want, seniority, domains, and any dealbreakers such as visa "
                    "sponsorship (blank: edit `preferences` in config.yaml later)", "", yes=yes)
        if about:
            values["preferences"] = about + "\n"
        values["scoring"] = _scoring(starter.get("scoring", {}), yes=yes, env=env)
        values["notify"] = _notifications(yes=yes, env=env)
        values["min_score"] = min(100, number("\nNotify me about jobs scoring at least (0-100)",
                                              starter.get("min_score", 70), yes=yes))
        new_text = replace_sections(text, values)
        with_packs(Config.model_validate(yaml.safe_load(new_text)), folder)  # never write a config that won't load
        config_path.write_text(new_text, encoding="utf-8", newline="\n")
        print(f"\nWrote {config_path}")

    resume = folder / "resume.md"
    if not resume.exists():
        source = ask("\nPath to your resume as a text or Markdown file (blank: create a template to fill in)", "",
                     yes=yes).strip('"')
        if source and Path(source).suffix.lower() in (".pdf", ".doc", ".docx"):
            print("  Shortlist reads plain text, so paste your resume's text into resume.md (or the web app's "
                  "Settings page).")
            source = ""
        if source and Path(source).expanduser().is_file():
            shutil.copyfile(Path(source).expanduser(), resume)
            print(f"Copied your resume to {resume}")
        else:
            resume.write_text(RESUME_TEMPLATE, encoding="utf-8", newline="\n")
            print(f"Created {resume}: replace the template with your resume before the first run.")
    env_path = folder / ".env"
    if env or not env_path.exists():
        set_env(env_path, env)
        print(f"Saved {len(env)} secret(s) to {env_path} (never committed: .env is in .gitignore)." if env
              else f"Created {env_path} for API keys and notification secrets.")
    _schedule(config_path, yes=yes)
    print("\nNext steps:\n"
          "  python -m shortlist check                     # one small test call, then reports what's missing\n"
          "  python -m shortlist run --dry-run --limit 20  # first run: fetch, score 20 jobs, print the digest\n"
          "  python -m shortlist web                       # browse and track matches")
    if not yes and confirm("\nRun the check now?", True, yes=yes):
        print()
        return check(config_path)
    return 0


# --- check -----------------------------------------------------------------------------------------------------------

def check(config_path: Path, *, call: bool = True, send_test: bool = False) -> int:
    failures = 0

    def report(ok: bool | None, text: str) -> None:
        nonlocal failures
        failures += ok is False
        print(f"[{LABELS[ok]}] {text}")

    if not config_path.exists():
        report(False, f"{config_path} not found; run: python -m shortlist init")
        return 1
    try:
        cfg = load_config(config_path)
    except Exception as e:
        report(False, f"{config_path.name} does not load: {_describe(e)}")
        return 1
    report(True, f"{config_path.name}: {describe_sources(cfg, every_kind=True)}"
                 + (f" (packs: {', '.join(cfg.packs)})" if cfg.packs else ""))
    if not (cfg.companies or cfg.career_pages or cfg.html_boards or cfg.github_listings):
        report(False, "no sources: add `packs`, `companies`, or `career_pages` to config.yaml")
    if not cfg.filters.title_include:
        report(None, "filters.title_include is empty, so every title is scored: that can be thousands of model calls")

    resume = Path(cfg.resume_path)
    text = resume.read_text(encoding="utf-8") if resume.exists() else ""
    if not text.strip() or RESUME_MARKER in text:
        report(False, f"{resume.name} is {'missing' if not resume.exists() else 'still the template'}: "
                      "paste your resume into it (or the web app's Settings page)")
    else:
        report(True, f"{resume.name}: {len(text.split())} words")
    if not cfg.preferences.strip():
        report(None, "`preferences` is empty: the scorer only has your resume to go on")
    elif "(for example" in cfg.preferences:
        report(None, "`preferences` still has the starter text in parentheses: say what you want instead")

    try:
        conn = db.connect(cfg.db_path)
        report(True, f"database {Path(cfg.db_path).name}: {conn.execute('SELECT count(*) FROM jobs').fetchone()[0]} jobs stored")
    except Exception as e:
        report(False, f"database {cfg.db_path}: {_describe(e)}")

    s = cfg.scoring
    where = f"{s.backend}, model {s.model or '(Codex default)'}"
    cli = {"claude_cli": s.claude_path, "codex_cli": s.codex_path}.get(s.backend)
    if cli and not (shutil.which(cli) or Path(cli).is_file()):
        report(False, f"scoring ({where}): {cli} not found; install the CLI or set scoring."
                      f"{'claude' if s.backend == 'claude_cli' else 'codex'}_path")
    elif needs_key(s) and not api_key(s):
        report(False, f"scoring ({where}): {KEY_ENV[s.backend]} is not set (add it to .env)")
    elif call:
        started = time.monotonic()
        try:
            answer = BACKENDS[s.backend]("This is a connection test. Answer with ok set to true.", CHECK_SCHEMA, s)
            report(answer.get("ok") is True, f"scoring ({where}): answered in {time.monotonic() - started:.0f}s")
        except Exception as e:
            report(False, f"scoring ({where}): {_describe(e)}")
    else:
        report(None, f"scoring ({where}): test call skipped")

    if not cfg.notify:
        report(None, "notifications: none set (`notify` in config.yaml); matches are in the web app")
    for channel in cfg.notify:
        missing = notify.missing_env([channel])
        if missing:
            report(False, f"notify by {channel}: set {', '.join(missing)} in .env")
        elif send_test:
            try:
                notify.send(channel, "Shortlist: test message. Notifications work.")
                report(True, f"notify by {channel}: test message sent")
            except RuntimeError as e:
                report(False, f"notify by {channel}: {e}")
        else:
            report(True, f"notify by {channel}: settings present (--send-test sends a test message)")

    from .render import RenderUnavailable, Renderer

    try:
        with Renderer() as renderer:
            renderer.html("about:blank")
        report(True, "browser for JavaScript-only careers pages: ready")
    except RenderUnavailable:
        report(None, "browser for JavaScript-only careers pages: not installed, so those pages are skipped. To add "
                     "it: python -m playwright install chromium" + (" (on Linux also: python -m playwright "
                     "install-deps chromium)" if sys.platform.startswith("linux") else ""))
    print("\nAll set." if not failures else f"\n{failures} problem(s) to fix.")
    return 1 if failures else 0
