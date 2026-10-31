"""Saved responses come from real websites, which sometimes embed their own API keys. None may land in this repo,
and each file stays a small excerpt."""
import re
from pathlib import Path

import pytest

SECRET_PATTERNS = {
    "Google API key": r"AIza[0-9A-Za-z_-]{35}",
    "AWS access key": r"(?:AKIA|ASIA)[0-9A-Z]{16}",
    "Stripe live key": r"(?:sk|pk|rk)_live_[0-9A-Za-z]{10,}",
    "Slack token": r"xox[abprs]-[0-9A-Za-z-]{10,}",
    "GitHub token": r"gh[pousr]_[0-9A-Za-z]{30,}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}


@pytest.mark.parametrize("path", sorted((Path(__file__).parent / "fixtures").iterdir()), ids=lambda p: p.name)
def test_fixture_has_no_secrets(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    found = [name for name, pattern in SECRET_PATTERNS.items() if re.search(pattern, text)]
    assert not found, f"{path.name} contains {found}; redact it before committing"


@pytest.mark.parametrize("path", sorted((Path(__file__).parent / "fixtures").iterdir()), ids=lambda p: p.name)
def test_fixture_is_trimmed(path):
    """Keep a few real entries of a saved response, enough for the cases the tests check (see CONTRIBUTING)."""
    assert path.stat().st_size < 120_000, f"{path.name} is {path.stat().st_size} bytes; trim it"
