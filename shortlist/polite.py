"""How every request leaves Shortlist: robots.txt is respected (RFC 9309) and each host gets at most one request
per second. Shared by worker threads."""
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

# Identifies the tool to the sites it reads, with a page that explains it (robots.txt rules match "shortlist").
USER_AGENT = "shortlist/0.1 (+https://github.com/navkar98/shortlist; personal job alert tool)"


class Blocked(Exception):
    """robots.txt disallows the URL."""


class Polite:
    def __init__(self, client: httpx.Client, interval: float = 1.0):
        self.client, self.interval, self.lock = client, interval, threading.Lock()
        self.robots: dict[str, RobotFileParser] = {}
        self.next_slot: dict[str, float] = {}

    def _paced(self, method: str, url: str, **kwargs) -> httpx.Response:
        host = urlparse(url).netloc
        with self.lock:
            slot = max(time.monotonic(), self.next_slot.get(host, 0.0))
            self.next_slot[host] = slot + self.interval
        time.sleep(max(0.0, slot - time.monotonic()))
        return self.client.request(method, url, **kwargs)

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self.robots:
            rules = RobotFileParser()
            try:
                r = self._paced("GET", base + "/robots.txt")
                if r.status_code == 200:
                    rules.parse(r.text.splitlines())
                elif 400 <= r.status_code < 500:  # RFC 9309 2.3.1.3: unavailable, any resource may be accessed
                    rules.allow_all = True
                else:  # RFC 9309 2.3.1.4: unreachable (5xx), assume complete disallow
                    rules.disallow_all = True
            except httpx.HTTPError:
                rules.disallow_all = True
            self.robots[base] = rules
        return self.robots[base].can_fetch(USER_AGENT, url)

    def request(self, method: str, url: str, *, respect_robots: bool = True, **kwargs) -> httpx.Response:
        """A paced request that raises Blocked when robots.txt disallows it and HTTPStatusError on 4xx/5xx.
        `respect_robots=False` only when the user set `ignore_robots` for that board."""
        if respect_robots and not self.allowed(url):
            raise Blocked(url)
        return self._paced(method, url, **kwargs).raise_for_status()

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)
