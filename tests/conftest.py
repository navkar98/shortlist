import pytest

from shortlist import polite, render


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    """Tests use mocked hosts, so skip the one-request-per-second wait."""
    monkeypatch.setattr(polite.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    """Tests never start a real browser; tests that need rendering replace Renderer.html themselves."""
    def unavailable(self, url, wait_for=""):
        raise render.RenderUnavailable("browser disabled in tests")

    monkeypatch.setattr(render.Renderer, "html", unavailable)
