"""Fill in descriptions for jobs that came from a list without one (GitHub listings, Workday, HTML boards).

Order: the job board's API when the link is a Greenhouse or Lever posting; otherwise the page itself, preferring its
schema.org JobPosting data and falling back to its readable text; a page that loaded but had nothing readable is
rendered in a browser. Requests go through polite.Polite (robots.txt, about one request per second per site).
"""
import html
import json
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup, Comment

from .fetchers import MAX_DESCRIPTION_CHARS, Job, clean_text, describe_error, html_to_text, parse_greenhouse, parse_lever
from .polite import USER_AGENT, Blocked, Polite
from .render import Renderer, RenderUnavailable

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 300  # shorter page text is usually an expired or error page, not a description
GREENHOUSE_HOSTS = {"job-boards.greenhouse.io", "job-boards.eu.greenhouse.io", "boards.greenhouse.io"}
SLUG_FIRST_HOSTS = {"jobs.lever.co": "lever", "jobs.ashbyhq.com": "ashby"}  # https://<host>/<slug>/<job id>[/apply]


def canonical_identity(url: str) -> tuple[str, str, str] | None:
    """(ats, slug, job_id) when the link is a Greenhouse, Lever, or Ashby posting, so it matches our own fetches."""
    parsed = urlparse(url)
    host, parts = parsed.netloc.lower(), [p for p in parsed.path.split("/") if p]
    if host in GREENHOUSE_HOSTS and len(parts) >= 3 and parts[1] == "jobs":
        return "greenhouse", parts[0], parts[2]
    if host in SLUG_FIRST_HOSTS and len(parts) >= 2:
        return SLUG_FIRST_HOSTS[host], parts[0], parts[1]
    return None


def greenhouse_job_id(url: str) -> str | None:
    """The `gh_jid` of a company careers page that embeds a Greenhouse posting."""
    return (parse_qs(urlparse(url).query).get("gh_jid") or [None])[0]


# How careers pages name the Greenhouse board they embed: the embed script (`.../embed/job_board/js?for=<board>`), the
# application frame (`.../embed/job_app?for=<board>`), board links, or calls to the board API.
GREENHOUSE_BOARD = re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_(?:board(?:/js)?|app)\?for=)?"
                              r"([\w-]+)|boards-api\.greenhouse\.io/v1/boards/([\w-]+)")


def greenhouse_board(page: str) -> str | None:
    """The Greenhouse board a careers page embeds, when it names one."""
    for match in GREENHOUSE_BOARD.finditer(page):
        board = match.group(1) or match.group(2)
        if board not in ("embed", "v1"):
            return board
    return None


def job_postings(soup: BeautifulSoup) -> list[dict]:
    """Every schema.org JobPosting in the page's JSON-LD scripts."""
    found = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "ItemList":  # a job list page
                items += [e.get("item", e) for e in item.get("itemListElement") or [] if isinstance(e, dict)]
            kind = item.get("@type") if isinstance(item, dict) else None
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                found.append(item)
    return found


def posting_text(posting: dict) -> str:
    desc = str(posting.get("description") or "")
    return html_to_text(html.unescape(desc) if "&lt;" in desc else desc)


def posting_location(posting: dict) -> str:
    places = posting.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    found = []
    for place in places:
        address = place.get("address") if isinstance(place, dict) else None
        if isinstance(address, dict):
            country = address.get("addressCountry")
            country = country.get("name") if isinstance(country, dict) else country
            found.append(", ".join(str(p) for p in (address.get("addressLocality"), address.get("addressRegion"), country) if p))
    if posting.get("jobLocationType") == "TELECOMMUTE":
        found.append("Remote")
    return "; ".join(dict.fromkeys(p for p in found if p))


def _jsonld_description(soup: BeautifulSoup) -> str:
    return next((text for p in job_postings(soup) if (text := posting_text(p))), "")


def _from_page(soup: BeautifulSoup, how: str) -> tuple[str, str, str]:
    """(description, how, location) from a job page: its JobPosting data, else its readable text."""
    for posting in job_postings(soup):
        if text := posting_text(posting):
            return text, "JobPosting data" if how == "page" else f"JobPosting data ({how})", posting_location(posting)
    text = _page_text(soup)
    return (text, f"{how} text", "") if len(text) >= MIN_TEXT_CHARS else ("", "no readable text", "")


# --- reading the description out of a job page's HTML ----------------------------------------------------------------
# Structure, not per-site rules: page chrome is recognized by what it is (navigation, banners, dialogs, link lists), the
# description by being where the page's prose is, and lines are laid out as a browser would (blocks break lines).

BLOCKS = {"address", "article", "blockquote", "dd", "div", "dl", "dt", "figcaption", "figure", "h1", "h2", "h3", "h4",
          "h5", "h6", "hr", "li", "main", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead",
          "tr", "ul"}
NEVER_TEXT = ["script", "style", "noscript", "template", "svg", "canvas", "iframe", "img", "picture", "video", "audio",
              "button", "select", "option", "input", "textarea"]
CHROME_TAGS = ["nav", "header", "footer", "aside", "form", "dialog", "menu"]
CHROME_ROLES = {"navigation", "banner", "contentinfo", "complementary", "dialog", "alertdialog", "search", "menu",
                "menubar", "toolbar", "tablist"}
CHROME_NAMES = re.compile(r"(?<![a-z])(?:cookies?|consent|gdpr|banner|modal|popup|overlay|newsletter|breadcrumbs?|share|"
                          r"sharing|social|footer|header|navbar|nav|navigation|menu|sidebar|language|locale|lang|skip)"
                          r"(?![a-z])", re.I)
CHROME_SHARE = 0.3   # page chrome is small: a block holding more of the page's text than this is never treated as chrome
MAIN_SHARE = 0.85    # the description is the smallest element holding this share of the page's prose


def _text_len(el) -> int:
    return len(el.get_text(" ", strip=True))


def _is_landmark(el) -> bool:
    """Page navigation, banner, footer, or dialog. A `<header>` or `<footer>` inside an article, main, or section is
    that section's own header (often the job title), not the page's: the rule screen readers follow (HTML-AAM)."""
    if (el.get("role") or "").lower() in CHROME_ROLES:
        return True
    if el.name in ("header", "footer"):
        return not el.find_parent(["article", "aside", "main", "nav", "section"])
    return el.name in CHROME_TAGS and el.name != "form"


def _is_chrome(el, page_chars: int) -> bool:
    """Landmarks unless they wrap the main content; and, when small, hidden elements, forms (some sites wrap the whole
    page in one), and blocks whose class or id names them as chrome."""
    if _is_landmark(el):
        return not el.find(["main", "article"]) and not el.find(attrs={"role": "main"})
    # Hidden: mobile menus and closed dialogs are small; a large hidden block is content that a script reveals or moves
    # into place (React streams server-rendered pages as `<div hidden id="S:0">`), and a hidden tab is still the posting.
    hidden = (el.has_attr("hidden") or el.get("aria-hidden") == "true" or
              re.search(r"display:\s*none|visibility:\s*hidden", el.get("style") or "")) and el.get("role") != "tabpanel"
    named = el.name == "form" or CHROME_NAMES.search(" ".join([el.get("id") or "", *(el.get("class") or [])]))
    return bool(hidden or named) and _text_len(el) < CHROME_SHARE * page_chars


def _is_prose(text: str) -> bool:
    """A sentence or more, as opposed to a menu entry, button, or label (character count covers scripts without spaces)."""
    return len(text.split()) >= 7 or len(text) >= 80


def _main_block(root):
    """The smallest element that holds MAIN_SHARE of the page's prose, the text grouped by the block holding it."""
    paragraphs: dict[int, list] = {}
    for s in root.find_all(string=True):
        if isinstance(s, Comment) or not s.strip():
            continue
        block = s.find_parent(BLOCKS) or root
        paragraphs.setdefault(id(block), [block, []])[1].append(s.strip())
    weight: Counter = Counter()
    for block, parts in paragraphs.values():
        text = " ".join(parts)
        if _is_prose(text):
            for node in (block, *block.parents):
                weight[id(node)] += len(text)
                if node is root:
                    break
    node, total = root, weight[id(root)]
    while total:
        child = next((c for c in node.find_all(True, recursive=False) if weight[id(c)] >= MAIN_SHARE * total), None)
        if child is None:
            break
        node = child
    return node


def _laid_out_text(root) -> str:
    """The text with a line break at every block boundary and `<br>`, and inline pieces joined as on screen."""
    mark = "\x00"
    for s in root.find_all(string=True):
        if isinstance(s, Comment):
            s.extract()
        elif not s.strip():  # whitespace between tags is one space at most
            s.replace_with(" ")
        elif "\n" in s.strip():  # line breaks inside text are kept: postings are often plain text shown as written
            s.replace_with(re.sub(r"\s*\n\s*", mark, s))
    for br in root.find_all("br"):
        br.replace_with(mark)
    for block in root.find_all(BLOCKS):
        block.insert_before(mark)
        block.append(mark)
    text = re.sub(r"[​-‍⁠﻿]", "", root.get_text(""))  # zero-width characters
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split(mark)]
    return "\n".join(line for i, line in enumerate(lines) if line and (i == 0 or line != lines[i - 1]))


def _page_text(soup: BeautifulSoup) -> str:
    root = soup.body or soup
    for tag in root(NEVER_TEXT):
        tag.decompose()
    page_chars = _text_len(root) or 1
    # The page's own heading names the job, often more fully than the listing ("... - 2027 Start"). Read it before
    # chrome goes: some sites put it in a block named "header". Empty ones belong to dialogs filled in by scripts.
    h1 = next((text for h in root.find_all("h1") if (text := h.get_text(" ", strip=True))
               and not any(_is_landmark(p) for p in h.parents if p.name)), "")
    for el in root.find_all(True):
        if not el.decomposed and _is_chrome(el, page_chars):
            el.decompose()
    for el in reversed(root.find_all(["ul", "ol", "dl", "div", "section", "table", "p"])):  # innermost first: link lists
        if el.decomposed:
            continue
        text = el.get_text(" ", strip=True)
        if len(text) > 80 and sum(len(a.get_text(" ", strip=True)) for a in el.find_all("a")) / len(text) > 0.5:
            el.decompose()
    main = _main_block(root)
    lines = _laid_out_text(main).splitlines()
    # Without an h1, the job title is usually the nearest heading above the description.
    h2 = main.find_previous("h2")
    heading = h1 or (h2.get_text(" ", strip=True) if h2 else "")
    if lines and heading and len(heading) <= 200 and heading not in lines[:3]:
        lines.insert(0, heading)
    return clean_text("\n".join(lines))


def _detail(job: Job, polite: Polite) -> tuple[str, str, str]:
    """(description, how it was found, location from the page) or ("", why not, "")."""
    ident = canonical_identity(job.detail_url)
    try:
        if ident and ident[0] == "greenhouse":
            data = polite.get(f"https://boards-api.greenhouse.io/v1/boards/{ident[1]}/jobs/{ident[2]}").json()
            return parse_greenhouse({"jobs": [data]}, ident[1])[0].description, "board API", ""
        if ident and ident[0] == "lever":
            data = polite.get(f"https://api.lever.co/v0/postings/{ident[1]}/{ident[2]}", params={"mode": "json"}).json()
            return parse_lever([data], ident[1])[0].description, "board API", ""
        response = polite.get(job.detail_url)
        if response.status_code != 200 or not response.text.strip():  # a bot challenge: never rendered around
            return "", f"HTTP {response.status_code} without a page", ""
        # A company page that embeds a Greenhouse posting (`?gh_jid=`) often shows only its own careers text around
        # the frame; the posting itself comes from the board's API.
        if (gh_jid := greenhouse_job_id(job.detail_url)) and (board := greenhouse_board(response.text)):
            try:
                data = polite.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{gh_jid}").json()
                return parse_greenhouse({"jobs": [data]}, board)[0].description, "board API (embedded)", ""
            except httpx.HTTPStatusError:
                pass  # not that board's job after all: read the page
        return _from_page(BeautifulSoup(response.text, "html.parser"), "page")
    except Blocked:
        return "", "blocked by robots.txt", ""
    except Exception as e:  # one page failing must never crash the run
        log.info("%s: no description (%s)", job.detail_url, describe_error(e))
        return "", "fetch error", ""


MAX_RENDERS = 40  # browser renders per run (several seconds each); the rest wait for the next run


def _render_details(jobs: list[Job]) -> dict[str, tuple[str, str, str]]:
    """Pages that loaded but had nothing readable in their HTML (JavaScript-only), rendered one by one."""
    found = {}
    try:
        with Renderer() as renderer:
            for job in jobs[:MAX_RENDERS]:
                try:
                    found[job.key] = _from_page(BeautifulSoup(renderer.html(job.detail_url), "html.parser"), "rendered")
                except RenderUnavailable:
                    raise
                except Exception as e:  # one page failing must never crash the run
                    log.info("%s: render failed (%s)", job.detail_url, describe_error(e))
    except RenderUnavailable as e:
        log.warning("JavaScript-only pages skipped: %s", e)
    return found


def fetch_details(jobs: list[Job], *, max_workers: int = 5, transport: httpx.BaseTransport | None = None) -> list[Job]:
    """The same jobs, with the full description put in front of any listing note wherever it could be read."""
    todo = [j for j in jobs if j.detail_url]
    if not todo:
        return jobs
    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}, transport=transport) as client, \
            ThreadPoolExecutor(max_workers) as pool:
        polite = Polite(client)
        found = dict(zip([j.key for j in todo], pool.map(lambda j: _detail(j, polite), todo)))
    found.update(_render_details([j for j in todo if found[j.key][1] == "no readable text"]))
    log.info("descriptions for %d jobs listed without one: %s", len(todo),
             ", ".join(f"{n} {how}" for how, n in Counter(how for _, how, _ in found.values()).most_common()))
    filled = []
    for job in jobs:
        text, _, location = found.get(job.key, ("", "", ""))
        if text:
            text = text[:MAX_DESCRIPTION_CHARS - len(job.description) - 2]
            update = {"description": "\n\n".join(p for p in (text, job.description) if p), "detail_url": ""}
            if location and (not job.location or re.fullmatch(r"\d+ Locations", job.location)):
                update.update(location=location, remote=job.remote or "remote" in location.lower())
            job = job.model_copy(update=update)
        filled.append(job)
    return filled
