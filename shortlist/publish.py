"""The public list: every open role in chosen groups (categories.py) that the config's filters let through, written as
Markdown pages (by group, field, and company) and a jobs.json into a clone of a git repo, then committed and pushed
when a role changed.

Only roles read from the companies' own job boards are listed. Jobs known only from other people's lists (SimplifyJobs)
are theirs to publish, not ours, so they are left out.
"""
import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import db
from .categories import FIELDS, LABELS, groups
from .config import Config, pack_path
from .fetchers import Job
from .polite import PROJECT_URL
from .prefilter import build_filter

log = logging.getLogger(__name__)

# GitHub cuts a README off after 500 KiB, and people report the same for other Markdown pages. Pages stay well under
# that (about 1,200 rows), so they show whole and load quickly; a longer list goes on over several pages.
PAGE_BYTES = 300_000
# A role is open while its board is still being read and the role isn't closed (db.CLOSED). Boards that list every open
# job show a closed one at once. Paged boards (Workday) are read only to the first page with nothing new, and to the
# end once a week, so an older role on them is seen weekly: it stays listed up to db.UNSEEN_DAYS after it was last seen.
BOARD_READ_WITHIN = timedelta(hours=48)
NOT_OURS = ("simplify",)
HEADINGS = {"intern": "Internships", "new-grad": "New grad", "phd": "PhD & postdoc", "research": "Research"}
GENERAL = ("general", "General")  # a role in no field but the page's own (a "Research Scientist" on the research page)
TABLE_HEAD = "| Company | Role | Location | Found |\n|---|---|---|---|\n"
# "  - workday:ag.wd3/Airbus   # Airbus": boards often give an ID for the company ("ag"); a pack's comment has the name.
_NAMED = re.compile(r"^\s*-\s*(\w+:\S+)\s+#\s*(.+?)\s*$", re.M)


def board_names(cfg: Config) -> dict[str, str]:
    """"ats:slug" -> company name, from the comments in the config's packs."""
    names = {}
    for pack in cfg.packs:
        path = pack_path(pack, Path(cfg.db_path).parent)
        if path.exists():
            for board, name in _NAMED.findall(path.read_text(encoding="utf-8")):
                names.setdefault(board.lower(), name)
    return names


def _count(n: int, one: str, many: str = "") -> str:
    return f"{n:,} {one if n == 1 else many or one + 's'}"


def _order(role: dict) -> tuple:
    return role["company"].casefold(), role["title"].casefold(), role["url"]


def open_roles(conn, wanted: list[str], now: datetime, names: dict[str, str] | None = None,
               reject: Callable[[Job], str | None] | None = None) -> list[dict]:
    """Open roles in any of the `wanted` groups that are also in a field (research, ML, software...: the list is for
    these, not every internship) and that `reject` (the config's filters) lets through, by company and title.
    Postings of one role in several places are one row, linking to the newest; the same database gives the same list
    in the same order."""
    read = (now - BOARD_READ_WITHIN).isoformat(timespec="seconds")
    seen = (now - timedelta(days=db.UNSEEN_DAYS)).isoformat(timespec="seconds")
    names, found = names or {}, {}
    for r in conn.execute(f"SELECT j.ats, j.slug, j.job_id, j.company, j.title, j.location, j.url, j.first_seen"
                          f" FROM jobs j JOIN {db.BOARD_SEEN} b USING (ats, slug) WHERE b.board_seen >= ?"
                          f" AND j.last_seen >= ? AND NOT {db.CLOSED} AND j.ats NOT IN {NOT_OURS + ('',)}",
                          (read, seen)):
        tags = groups(r["title"])
        if not set(tags) & set(wanted) or not set(tags) & set(FIELDS):
            continue
        if reject and reject(Job(ats=r["ats"], slug=r["slug"], job_id=r["job_id"], company=r["company"],
                                 title=r["title"], location=r["location"], remote="remote" in r["location"].lower(),
                                 url=r["url"], description="")):
            continue
        company = names.get(f"{r['ats']}:{r['slug']}".lower()) or r["company"]
        company = company[0].upper() + company[1:] if company.islower() else company
        role = found.setdefault((company.casefold(), r["title"].casefold()), {
            "company": company, "title": r["title"], "locations": [], "url": r["url"], "found": r["first_seen"][:10],
            "groups": list(tags)})
        if r["location"] and r["location"] not in role["locations"]:
            role["locations"].append(r["location"])
        if (r["first_seen"][:10], r["url"]) > (role["found"], role["url"]):
            role.update(found=r["first_seen"][:10], url=r["url"])
    for role in found.values():
        role["locations"].sort()
    return sorted(found.values(), key=_order)


def _cell(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
    return text.replace("|", "\\|").replace("<", "&lt;").replace("[", "(").replace("]", ")")


def _row(role: dict, company: str = "") -> str:
    """A table row. The company is named on its first row; the rest show ↳ (`company` empty)."""
    places = role["locations"]
    where = "; ".join(places[:2]) + (f" (+{len(places) - 2} more)" if len(places) > 2 else "")
    return f"| {company or '↳'} | [{_cell(role['title'], 110)}](<{role['url']}>) | {_cell(where, 80)} | {role['found']} |\n"


def by_field(roles: list[dict], group: str) -> list[tuple[str, str, list[dict]]]:
    """(file name, label, roles) per field, each role in its first field other than the page's own group."""
    fields = {field: [] for field in FIELDS if field != group}
    general = []
    for role in roles:
        field = next((g for g in role["groups"] if g in fields), None)
        (fields[field] if field else general).append(role)
    return [(f, LABELS[f], rs) for f, rs in fields.items() if rs] + ([(*GENERAL, general)] if general else [])


def _companies(roles: list[dict]) -> list[tuple[str, list[dict]]]:
    """(company, its roles) A to Z, each company's newest roles first."""
    blocks: dict[str, list[dict]] = {}
    for role in roles:  # already in company order
        blocks.setdefault(role["company"].casefold(), []).append(role)
    # sorted() keeps the company order for roles found the same day
    return [(rs[0]["company"], sorted(rs, key=lambda r: r["found"], reverse=True)) for rs in blocks.values()]


def _initial(company: str) -> str:
    return company[:1].upper() if company[:1].isalpha() else "#"


def _split(blocks: list[tuple[str, list[dict]]], budget: int) -> list[list[str]]:
    """Rows per page. A page holds whole first letters while they fit, so a new role moves no other page's start.
    A letter too long for one page starts a page and breaks between companies; a company too long for one page
    starts a page and breaks between its roles."""
    pages: list[list[str]] = [[]]
    size = 0

    def add(rows: list[str], fresh: bool = False) -> None:
        nonlocal size
        need = sum(len(r.encode()) for r in rows)
        if pages[-1] and (fresh or size + need > budget):
            pages.append([])
            size = 0
        pages[-1].extend(rows)
        size += need

    letters: dict[str, list[tuple[str, list[dict]]]] = {}
    for company, roles in blocks:
        letters.setdefault(_initial(company), []).append((company, roles))
    for letter in letters.values():
        rows = {company: [_row(r, "" if i else f"**{_cell(company, 40)}**") for i, r in enumerate(roles)]
                for company, roles in letter}
        if sum(len(r.encode()) for rs in rows.values() for r in rs) <= budget:
            add([r for rs in rows.values() for r in rs])
            continue
        for n, (company, company_rows) in enumerate(rows.items()):
            if sum(len(r.encode()) for r in company_rows) <= budget:
                add(company_rows, fresh=n == 0)
                continue
            for i, row in enumerate(company_rows):
                before = len(pages)
                add([row], fresh=i == 0)
                if len(pages) > before and i:  # the company goes on over a page break: name it again
                    pages[-1][-1] = row.replace("| ↳ |", f"| **{_cell(company, 40)}** (continued) |", 1)
    return pages


def _labels(firsts: list[str], lasts: list[str]) -> list[str]:
    """Guide words for pages, like a dictionary's: "A–R", "S–Sm", "Sn–Z"; as long as needed to tell pages apart."""
    def shared(a: str, b: str) -> int:
        n = 0
        while n < min(len(a), len(b)) and a[n].casefold() == b[n].casefold():
            n += 1
        return n

    labels = []
    for i, (first, last) in enumerate(zip(firsts, lasts)):
        a = first[:shared(first, lasts[i - 1]) + 1] if i else _initial(first)
        b = last[:shared(last, firsts[i + 1]) + 1] if i + 1 < len(firsts) else _initial(last)
        a, b = a[:1].upper() + a[1:], b[:1].upper() + b[1:]
        labels.append(a if a == b else f"{a}–{b}")
    return labels


def field_pages(roles: list[dict], group: str, name: str, label: str,
                budget: int = PAGE_BYTES) -> list[tuple[str, str, str]]:
    """(file name, label, text) for one field of a group: every role, by company A-Z, over as many pages as fit.
    Pages carry no date or count, so a page whose roles didn't change stays the same file."""
    blocks = _companies(roles)
    pages = _split(blocks, budget - 3_000)  # room for the page's header
    firsts, lasts, at = [], [], 0  # each page's first and last company
    order = [c for c, rs in blocks for _ in rs]
    for rows in pages:
        firsts.append(order[at])
        at += len(rows)
        lasts.append(order[at - 1])
    files = [f"{name}.md" if n == 0 else f"{name}-{n + 1}.md" for n in range(len(pages))]
    labels = _labels(firsts, lasts)
    heading = HEADINGS.get(group, LABELS[group])
    out = []
    for n, (file, rows) in enumerate(zip(files, pages)):
        nav = " · ".join(f"**{lab}**" if k == n else f"[{lab}]({f})" for k, (f, lab) in enumerate(zip(files, labels)))
        head = (f"# {heading} · {label}\n\n[{heading}](README.md) · [All groups](../README.md)"
                + (f"\n\nPages: {nav}" if len(pages) > 1 else "")
                + "\n\nBy company, A to Z, newest roles first. A role drops off once its company stops listing it.\n\n")
        out.append((file, labels[n], head + TABLE_HEAD + "".join(rows)))
    return out


def group_index(group: str, fields: list[tuple[str, str, list[dict], list]]) -> str:
    total = sum(len(rs) for _, _, rs, _ in fields)  # each role is in one field of a group
    companies = len({r["company"].casefold() for _, _, rs, _ in fields for r in rs})
    text = (f"# {HEADINGS.get(group, LABELS[group])}\n\n{_count(total, 'open role')} at"
            f" {_count(companies, 'company', 'companies')}."
            f" [All groups](../README.md)\n\n| Field | Roles | Companies | Pages |\n|---|---|---|---|\n")
    for name, label, rs, pages in fields:
        links = " · ".join(f"[{lab}]({file})" for file, lab, _ in pages)
        text += f"| [{label}]({pages[0][0]}) | {len(rs):,} | {len({r['company'].casefold() for r in rs}):,} | {links} |\n"
    return text


def front_page(layout: dict[str, list], title: str, updated: str, total: int, companies: int) -> str:
    columns = [(f, LABELS[f]) for f in FIELDS] + [GENERAL]
    columns = [(name, label) for name, label in columns
               if any(name == f for fields in layout.values() for f, *_ in fields)]
    text = (f"# {title}\n\nOpen roles from the job boards of {_count(companies, 'company', 'companies')}, listed by"
            f" company. Every link goes to the company's own posting. The lists are rebuilt every few hours, and a role"
            f" drops off once its company stops listing it.\n\nUpdated {updated} · {_count(total, 'open role')}\n\n"
            f"| | {' | '.join(label for _, label in columns)} | All |\n|---|{'---|' * (len(columns) + 1)}\n")
    for group, fields in layout.items():
        found = {name: (rs, pages) for name, _, rs, pages in fields}
        cells = [f"[{len(found[n][0]):,}]({group}/{found[n][1][0][0]})" if n in found else "–" for n, _ in columns]
        all_roles = sum(len(rs) for rs, _ in found.values())
        text += f"| [{HEADINGS.get(group, LABELS[group])}]({group}/README.md) | {' | '.join(cells)} | {all_roles:,} |\n"
    text += (f"\nA role can be in more than one group: a PhD research internship is in Internships, PhD & postdoc, and"
             f" Research. [jobs.json](jobs.json) has every role, for anyone building on the list.\n\n---\n\n"
             f"Made with [Shortlist]({PROJECT_URL}), which reads these boards, can score new roles against your resume,"
             f" and can send the ones that fit to Telegram.\n")
    return text


def _put(path: Path, text: str) -> None:
    """Write a file unless it already holds `text`."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    path.write_text(text, encoding="utf-8", newline="\n")


def write(cfg: Config, conn, now: datetime | None = None) -> tuple[int, dict[str, int]]:
    """Write README.md, each group's pages, and jobs.json into `publish.folder`, and remove pages no longer needed.
    Returns how many roles there are, and how many each group has (a role can be in several)."""
    now = now or datetime.now(timezone.utc)
    folder = Path(cfg.publish.folder)
    roles = open_roles(conn, cfg.publish.groups, now, board_names(cfg), build_filter(cfg.filters))
    by_group = {g: [r for r in roles if g in r["groups"]] for g in cfg.publish.groups}
    counts = {g: len(rs) for g, rs in by_group.items()}
    try:  # the same roles as last time: leave the files alone, so there's nothing to commit
        if json.loads((folder / "jobs.json").read_text(encoding="utf-8"))["roles"] == roles:
            log.info("public list: the same %d roles as last time; not rewritten", len(roles))
            return len(roles), counts
    except (OSError, ValueError, KeyError, TypeError):
        pass
    layout, written = {}, set()
    for group, rs in by_group.items():
        (folder / group).mkdir(parents=True, exist_ok=True)
        layout[group] = []
        for name, label, field_roles in by_field(rs, group):
            pages = field_pages(field_roles, group, name, label)
            for file, _, text in pages:
                _put(folder / group / file, text)
                written.add(folder / group / file)
            layout[group].append((name, label, field_roles, pages))
        _put(folder / group / "README.md", group_index(group, layout[group]))
        written.add(folder / group / "README.md")
    for group in by_group:  # pages from earlier runs that the lists no longer need: closed roles must not linger
        for page in (folder / group).glob("*.md"):
            if page not in written:
                page.unlink()
    companies = len({r["company"].casefold() for r in roles})
    _put(folder / "README.md", front_page(layout, cfg.publish.title, now.strftime("%Y-%m-%d %H:%M UTC"), len(roles),
                                          companies))
    # One role per line: a change shows as a few changed lines, and the repo's history grows slowly.
    lines = ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in roles)
    _put(folder / "jobs.json", f'{{"updated":"{now.isoformat(timespec="seconds")}","roles":[\n{lines}\n]}}\n')
    log.info("public list: %d roles written to %s (%s)", len(roles), folder,
             ", ".join(f"{HEADINGS.get(g, LABELS[g])} {n:,}" for g, n in counts.items()))
    return len(roles), counts


def redact(text: str) -> str:
    """Error text without credentials, should a remote URL carry any."""
    return re.sub(r"(\w+://)[^/\s@]+@", r"\1***@", text.strip().splitlines()[-1] if text.strip() else "")[:200]


def push(folder: str | Path, message: str) -> bool:
    """Commit everything in the clone and push it, if anything changed. Failures are logged, never raised."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    try:
        if (r := git("add", "-A")).returncode != 0:
            log.warning("public list: git add failed (exit %d): %s", r.returncode, redact(r.stderr))
            return False
        if git("diff", "--cached", "--quiet").returncode == 0:
            log.info("public list: nothing changed since the last push")
            return True
        for step in (("commit", "-q", "-m", message), ("push", "-q")):
            if (r := git(*step)).returncode != 0:
                log.warning("public list: git %s failed (exit %d): %s", step[0], r.returncode, redact(r.stderr))
                return False
    except (OSError, subprocess.TimeoutExpired) as e:  # no git, or a push that hangs
        log.warning("public list: git failed (%s)", type(e).__name__)
        return False
    log.info("public list: pushed")
    return True


def publish(cfg: Config, conn, *, push_changes: bool | None = None) -> None:
    """Write the lists and, when the config (or `push_changes`) says so, commit and push them."""
    total, counts = write(cfg, conn)
    if cfg.publish.push if push_changes is None else push_changes:
        push(cfg.publish.folder, f"Update the lists: {_count(total, 'open role')} ("
                                 + ", ".join(f"{HEADINGS.get(g, LABELS[g])} {n:,}" for g, n in counts.items()) + ")")
