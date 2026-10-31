"""Load config.yaml (and .env next to it, and any company packs it names) into a validated Config, and edit it."""
import json
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

from .fetchers import FETCHERS

PACKS_DIR = Path(__file__).resolve().parents[1] / "packs"


class Filters(BaseModel):
    title_include: list[str] = []
    title_exclude: list[str] = []
    locations: list[str] = []
    allow_remote: bool = True


class Scoring(BaseModel):
    backend: str = "claude_cli"     # claude_cli | codex_cli | anthropic_api | openai_api
    model: str = ""                 # empty: the backend's default (scoring.DEFAULT_MODELS)
    batch_size: int = Field(12, ge=1)
    timeout_seconds: int = Field(300, gt=0)
    max_jobs_per_run: int = Field(0, ge=0)  # 0: no cap. API backends bill per call; a cap bounds each run's cost
    recheck_alone: bool = True      # score again alone every job within 15 points of min_score or above it
    feedback_examples: int = Field(20, ge=0)  # jobs you rated, shown to the scorer as examples; 0: none
    instructions: str = ""          # your own scoring instructions (Settings > Scoring prompt); empty: the built-in ones
    claude_path: str = "claude"     # claude_cli only
    codex_path: str = "codex"       # codex_cli only
    workdir: str = "~/.shortlist/scorer_workdir"  # CLI backends; outside any folder with a CLAUDE.md or AGENTS.md
    effort: str = ""                # anthropic_api, codex_cli: e.g. low | medium | high; empty: the model's default

    @field_validator("backend")
    @classmethod
    def _known_backend(cls, backend: str) -> str:
        from .scoring import BACKENDS  # imported here: scoring imports this module

        if backend not in BACKENDS:
            raise ValueError(f"unknown scoring backend {backend!r}; available: {sorted(BACKENDS)}")
        return backend

    @model_validator(mode="after")
    def _model_for_backend(self):
        from .scoring import DEFAULT_MODELS

        self.model = self.model or DEFAULT_MODELS.get(self.backend, "")
        if not self.model and self.backend == "openai_api":
            raise ValueError(f"scoring.model is required for the {self.backend} backend")
        return self


class Listings(BaseModel):
    """A listings.json file in the SimplifyJobs format: a JSON list of job entries without descriptions."""
    name: str
    url: str
    keep_categories: list[str] = []  # keep a listing if its category is here...
    keep_degrees: list[str] = []     # ...or it names one of these degrees (both empty: keep every category)
    drop_sponsorship: list[str] = []
    max_age_days: int | None = None


class HtmlBoard(BaseModel):
    """A careers page without a known job board, read with CSS selectors (written by hand here, or found by
    discovery and kept in the database). With no `jobs` selector, the page's schema.org JobPosting data is read."""
    name: str
    url: str                       # first page of the job list
    jobs: str = ""                 # one element per job
    title: str = ""                # inside a job element; default: the job's link text
    link: str = "a[href]"          # inside a job element; the element itself when it is a link
    location: str = ""             # inside a job element
    next_page: str = ""            # link to the next page of results
    max_pages: int = Field(20, ge=1)
    render: bool = False           # load the page in a browser first (JavaScript-only sites)
    ignore_robots: bool = False    # only you set this, per board, after checking the site's terms


class CareerPage(BaseModel):
    """A company careers page; every run turns new ones into a board or recipe (see discover.py)."""
    name: str
    url: str


class Discovery(BaseModel):
    """How careers pages are turned into boards. Results are stored in the database, so each site costs agent calls
    only when it is new, or when its board stopped returning jobs."""
    model: str = ""                                  # model for the agent; empty: scoring.model
    # On a sample of 100 careers pages, every site the agent solved took at most 5 calls; calls beyond 5 only went to
    # sites it never solved. 0 disables the agent (known boards and JobPosting data only).
    max_agent_calls_per_site: int = Field(5, ge=0)
    max_agent_calls_per_run: int = Field(40, ge=0)   # sites left over are discovered in later runs
    rediscover_after_empty_runs: int = Field(2, ge=1)


class Schedule(BaseModel):
    """Limits for `run --if-due`, which the scheduled task calls; manual runs are not limited."""
    max_runs_per_day: int = Field(2, ge=1)
    min_hours_between_runs: float = Field(6, ge=0)


def _check_companies(companies: list[str]) -> list[str]:
    bad = [c for c in companies if c.partition(":")[0] not in FETCHERS or not c.partition(":")[2]]
    if bad:
        raise ValueError(f"expected 'ats:slug' with ats in {sorted(FETCHERS)}, got {bad}")
    bad = [c for c in companies if c.startswith("workday:") and not re.fullmatch(r"[\w-]+\.wd\d+/[\w-]+", c[8:])]
    if bad:
        raise ValueError(f"Workday entries look like 'workday:<tenant>.wd<N>/<site>', got {bad}")
    return list(dict.fromkeys(companies))


class Pack(BaseModel):
    """A shareable list of sources (packs/<name>.yaml); a config adds them with `packs: [<name>, ...]`."""
    description: str = ""
    companies: list[str] = []
    html_boards: list[HtmlBoard] = []
    career_pages: list[CareerPage] = []
    github_listings: list[Listings] = []

    @field_validator("companies")
    @classmethod
    def _known_ats(cls, companies: list[str]) -> list[str]:
        return _check_companies(companies)


class Config(BaseModel):
    packs: list[str] = []
    companies: list[str] = []
    html_boards: list[HtmlBoard] = []
    github_listings: list[Listings] = []
    career_pages: list[CareerPage] = []
    filters: Filters = Filters()
    preferences: str = ""
    scoring: Scoring = Field(default_factory=Scoring)  # built on use: its validators import scoring.py
    notify: list[str] = []          # telegram; empty: see matches in the web app only
    discovery: Discovery = Discovery()
    schedule: Schedule = Schedule()
    min_score: int = Field(70, ge=0, le=100)
    db_path: str = "shortlist.db"
    resume_path: str = "resume.md"

    @field_validator("companies")
    @classmethod
    def _known_ats(cls, companies: list[str]) -> list[str]:
        return _check_companies(companies)

    @field_validator("notify")
    @classmethod
    def _known_channels(cls, channels: list[str]) -> list[str]:
        from .notify import LIMITS

        bad = [c for c in channels if c not in LIMITS]
        if bad:
            raise ValueError(f"unknown notification channel(s) {bad}; available: {sorted(LIMITS)}")
        return list(dict.fromkeys(channels))


def pack_path(name: str, folder: Path) -> Path:
    """A bundled pack by name (packs/<name>.yaml), or a .yaml file of your own, relative to the config's folder."""
    return folder / name if name.endswith((".yaml", ".yml")) else PACKS_DIR / f"{name}.yaml"


def load_pack(path: Path) -> Pack:
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in PACKS_DIR.glob("*.yaml")))
        raise ValueError(f"company pack {path} not found (bundled packs: {available})")
    return Pack.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def available_packs() -> dict[str, Pack]:
    return {p.stem: load_pack(p) for p in sorted(PACKS_DIR.glob("*.yaml"))}


def with_packs(cfg: Config, folder: Path) -> Config:
    """The config with its packs' sources added after its own; an entry already present (same board or URL) is kept
    once, as the config has it."""
    update = {key: list(getattr(cfg, key)) for key in ("companies", "html_boards", "career_pages", "github_listings")}
    for name in cfg.packs:
        pack = load_pack(pack_path(name, folder))
        update["companies"] = list(dict.fromkeys(update["companies"] + pack.companies))
        for key in ("html_boards", "career_pages", "github_listings"):
            urls = {item.url for item in update[key]}
            update[key] += [item for item in getattr(pack, key) if item.url not in urls]
    return cfg.model_copy(update=update)


def load_config(path: str | Path) -> Config:
    """Relative paths in the config resolve against the config file's folder, so scheduled runs work from any cwd."""
    path = Path(path).resolve()
    load_dotenv(path.parent / ".env")  # never overrides variables already set in the environment
    cfg = with_packs(Config.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {}), path.parent)
    cfg.db_path = str(path.parent / cfg.db_path)
    cfg.resume_path = str(path.parent / cfg.resume_path)
    workdir = path.parent / Path(cfg.scoring.workdir).expanduser()
    # `claude -p` loads CLAUDE.md from its cwd and every parent folder, and `codex exec` loads AGENTS.md from its cwd
    # (tested with both), so a workdir inside this repo would leak the repo's rules into scoring.
    leaks = [p / name for p in (workdir, *workdir.parents) for name in ("CLAUDE.md", "AGENTS.md")
             if (p / name).exists()]
    if leaks and cfg.scoring.backend in ("claude_cli", "codex_cli"):
        raise ValueError(f"scoring.workdir {workdir} is below {leaks[0]}, which the scorer CLI would load; "
                         "use a folder outside the repo")
    cfg.scoring.workdir = str(workdir)
    return cfg


# --- editing config.yaml: rewrite only the sections that changed, so comments elsewhere survive -----------------------

class _Dumper(yaml.SafeDumper):
    pass


# Same layout as the hand-written config: mappings as blocks, lists inline, multi-line text as `|` blocks.
_Dumper.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))
_Dumper.add_representer(list, lambda d, v: d.represent_sequence("tag:yaml.org,2002:seq", v, flow_style=True))
_Dumper.add_representer(dict, lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v, flow_style=False))


def replace_sections(text: str, values: dict) -> str:
    """Replace each top-level `key:` block (up to the next unindented line) with the new value."""
    for key, value in values.items():
        block = yaml.dump({key: value}, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110)
        pattern = re.compile(rf"^{re.escape(key)}:.*?(?=^\S|\Z)", re.S | re.M)
        text = pattern.sub(lambda m: block + "\n", text, count=1) if pattern.search(text) else f"{text.rstrip()}\n\n{block}"
    return text


def add_career_page(text: str, name: str, url: str) -> str:
    """Insert an entry after the last active entry of `career_pages` (commented-out entries stay below it)."""
    entry = f"  - {{name: {json.dumps(name)}, url: {json.dumps(url)}}}"
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("career_pages:")), None)
    if start is None:
        return f"{text.rstrip()}\n\ncareer_pages:\n{entry}\n"
    if lines[start].rstrip().endswith("[]"):  # `career_pages: []` in a new config
        lines[start] = "career_pages:"
    last, i = start, start + 1
    while i < len(lines) and (not lines[i] or lines[i][0] in " \t#"):  # the block: indented, blank, or comment lines
        if lines[i].lstrip().startswith("- "):
            last = i
        i += 1
    lines.insert(last + 1, entry)
    return "\n".join(lines) + "\n"


def write_config(path: Path, new_text: str) -> None:
    """Validate, back up the old file (if any), then write."""
    with_packs(Config.model_validate(yaml.safe_load(new_text) or {}), path.parent)
    if path.exists():
        path.with_suffix(".yaml.bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    path.write_text(new_text, encoding="utf-8", newline="\n")
