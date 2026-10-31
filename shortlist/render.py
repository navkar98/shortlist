"""Load JavaScript-rendered pages in headless Chromium (Playwright, optional dependency).

Used only for pages that load fine (HTTP 200) but carry no content in their HTML. Never used to get past a 403 or
other bot block: a site that refuses automated visitors stays refused. Playwright's sync API is not thread-safe, so
a Renderer is used from one thread, one page at a time.
"""
import logging

from .polite import USER_AGENT

log = logging.getLogger(__name__)


class RenderUnavailable(Exception):
    """Playwright or its browser is not installed."""


class Renderer:
    def __init__(self, timeout_ms: int = 45000):
        self.timeout_ms = timeout_ms
        self._playwright = self._browser = self._page = None

    def html(self, url: str, wait_for: str = "") -> str:
        """The page's HTML after JavaScript ran. `wait_for` (a CSS selector) waits until those elements appear."""
        if self._page is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise RenderUnavailable("pip install playwright, then: python -m playwright install chromium") from None
            self._playwright = sync_playwright().start()
            try:
                self._browser = self._playwright.chromium.launch()
            except Exception as e:  # browser binaries missing
                self.close()
                raise RenderUnavailable(f"python -m playwright install chromium ({type(e).__name__})") from None
            self._page = self._browser.new_page(user_agent=USER_AGENT)
        self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        try:  # job lists arrive after load; "network idle" alone sometimes fires before they render
            if wait_for:
                self._page.wait_for_selector(wait_for, timeout=20000)
            else:
                self._page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # timed out: use what rendered so far
            pass
        return self._page.content()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = self._browser = self._page = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
