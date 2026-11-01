import sys

import pytest
import yaml

from shortlist import scoring, wizard
from shortlist.config import load_config


@pytest.fixture
def answers(monkeypatch):
    """Type these answers, in order, at the wizard's prompts."""
    queue, prompts = [], []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if not queue:
            raise AssertionError(f"unexpected question: {prompt}")
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    return queue, prompts


def test_init_with_defaults_writes_a_config_that_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "find_claude", lambda: "")
    monkeypatch.setattr(wizard.shutil, "which", lambda name: None)  # no codex either
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert wizard.init(tmp_path / "config.yaml", yes=True) == 0
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.packs == ["ai-labs"] and cfg.notify == [] and cfg.scoring.backend == "claude_cli"
    assert cfg.filters.title_include[:2] == ["software engineer", "machine learning"]
    assert wizard.RESUME_MARKER in (tmp_path / "resume.md").read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=" in (tmp_path / ".env").read_text(encoding="utf-8")  # from .env.example
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "# Which model scores jobs" in text and "\r" not in text  # the starter's comments survive


def test_init_by_answers(tmp_path, answers):
    queue, prompts = answers
    resume = tmp_path / "my resume.txt"
    resume.write_text("Jane Doe. Ranking and search engineer.", encoding="utf-8")
    queue += [
        "ml engineer, research scientist",  # titles
        "",                                 # excluded words: keep the starter's
        "berlin, remote",                   # places
        "n",                                # remote jobs without a place
        "3,5",                              # packs: consumer-tech, new-grad (sorted by name)
        "Senior IC roles in ranking. Needs visa sponsorship.",
        "3",                                # backend: Anthropic API
        "claude-sonnet-5-5",                # model
        "50",                               # max jobs per run
        "sk-ant-test",                      # the key (no tty: typed like any answer)
        "2",                                # notify: Telegram
        "123:ABC", "42",                    # bot token, chat id
        "eighty", "80",                     # min score (a word is asked again)
        f'"{resume}"',                      # resume path, quoted as Explorer copies it
        "n",                                # no background schedule
        "n",                                # no check now
    ]
    assert wizard.init(tmp_path / "config.yaml") == 0 and not queue
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.filters.title_include == ["ml engineer", "research scientist"] and cfg.filters.allow_remote is False
    assert cfg.filters.locations == ["berlin", "remote"] and cfg.packs == ["consumer-tech", "new-grad"]
    assert cfg.preferences == "Senior IC roles in ranking. Needs visa sponsorship.\n" and cfg.min_score == 80
    assert (cfg.scoring.backend, cfg.scoring.model, cfg.scoring.max_jobs_per_run) == ("anthropic_api", "claude-sonnet-5-5", 50)
    assert cfg.notify == ["telegram"]
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env and "TELEGRAM_BOT_TOKEN=123:ABC" in env and "TELEGRAM_CHAT_ID=42" in env
    assert (tmp_path / "resume.md").read_text(encoding="utf-8") == "Jane Doe. Ranking and search engineer."
    assert not any("sk-ant-test" in p for p in prompts)


def test_init_keeps_what_exists(tmp_path, answers):
    queue, _ = answers
    (tmp_path / "config.yaml").write_text("companies: [lever:foo]\n", encoding="utf-8")
    (tmp_path / "resume.md").write_text("Mine.", encoding="utf-8")
    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    queue += ["n", "n"]  # no schedule, no check
    assert wizard.init(tmp_path / "config.yaml") == 0
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "companies: [lever:foo]\n"
    assert (tmp_path / "resume.md").read_text(encoding="utf-8") == "Mine."
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "OTHER=1\n"


def test_set_env_replaces_appends_and_quotes(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# comment\nTELEGRAM_CHAT_ID=\nKEEP=me\n", encoding="utf-8")
    wizard.set_env(path, {"TELEGRAM_CHAT_ID": "42", "OPENAI_API_KEY": "two words#"})
    assert path.read_text(encoding="utf-8") == '# comment\nTELEGRAM_CHAT_ID=42\nKEEP=me\nOPENAI_API_KEY="two words#"\n'


def test_check_reports_each_problem(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"companies": ["lever:foo"], "notify": ["telegram"],
                                      "scoring": {"claude_path": sys.executable, "workdir": str(tmp_path / "wd")}}),
                      encoding="utf-8")
    (tmp_path / "resume.md").write_text(wizard.RESUME_TEMPLATE, encoding="utf-8")
    calls = []
    monkeypatch.setitem(scoring.BACKENDS, "claude_cli", lambda prompt, schema, cfg: calls.append(schema) or {"ok": True})
    assert wizard.check(config) == 1
    out = capsys.readouterr().out
    assert "[ ok ] config.yaml: 1 job board, 0 careers pages" in out
    assert "[ ok ] scoring (claude_cli, model sonnet): answered" in out
    assert "[FAIL] resume.md is still the template" in out and "[FAIL] notify by telegram: set TELEGRAM_BOT_TOKEN" in out
    assert "[note] filters.title_include is empty" in out and "browser for JavaScript-only careers pages" in out
    assert calls == [wizard.CHECK_SCHEMA] and "2 problem(s) to fix." in out

    (tmp_path / "resume.md").write_text("Jane Doe, engineer. " * 20, encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert wizard.check(config, call=False) == 0 and "All set." in capsys.readouterr().out


def test_check_without_a_config_points_to_init(tmp_path, capsys):
    assert wizard.check(tmp_path / "config.yaml") == 1
    assert "run: python -m shortlist init" in capsys.readouterr().out


def test_init_with_the_openai_api_asks_for_its_key(tmp_path, answers, monkeypatch):
    queue, prompts = answers
    monkeypatch.setattr(wizard, "find_claude", lambda: "")
    monkeypatch.setattr(wizard.shutil, "which", lambda name: None)  # no codex either
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    queue += ["", "", "", "", "none", "",   # starter filters, no packs, no preferences
              "4", "gpt-5.4-mini", "0", "",  # OpenAI API, model, no cap, key later
              "1", "", "", "n", "n"]        # web app only, min score, resume template, no schedule, no check
    assert wizard.init(tmp_path / "config.yaml") == 0 and not queue
    cfg = load_config(tmp_path / "config.yaml")
    assert (cfg.scoring.backend, cfg.scoring.model) == ("openai_api", "gpt-5.4-mini")
    assert cfg.packs == [] and any(p.startswith("OPENAI_API_KEY") for p in prompts)
