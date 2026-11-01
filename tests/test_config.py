from pathlib import Path

import pytest
from pydantic import ValidationError

from shortlist.config import available_packs, load_config


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_paths_resolve_next_to_config_and_companies_dedupe(tmp_path):
    cfg = load_config(write(tmp_path, "companies: [greenhouse:acme, greenhouse:acme, lever:foo]\n"))
    assert cfg.companies == ["greenhouse:acme", "lever:foo"]
    assert Path(cfg.db_path) == tmp_path / "shortlist.db"
    assert Path(cfg.scoring.workdir) == Path.home() / ".shortlist" / "scorer_workdir"
    assert cfg.scoring.batch_size == 12 and cfg.scoring.backend == "claude_cli"


@pytest.mark.parametrize("company", ["smartrecruiters:acme", "workday:acme", "workday:acme/External", "greenhouse:", "acme"])
def test_rejects_bad_company_entries(tmp_path, company):
    with pytest.raises(ValidationError):
        load_config(write(tmp_path, f"companies: ['{company}']\n"))


def test_rejects_unknown_backend(tmp_path):
    with pytest.raises(ValidationError, match="unknown scoring backend"):
        load_config(write(tmp_path, "companies: [lever:foo]\nscoring: {backend: gemini_cli}\n"))


def test_rejects_workdir_below_a_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# project rules\n", encoding="utf-8")
    with pytest.raises(ValueError, match="CLAUDE.md"):
        load_config(write(tmp_path, "companies: [lever:foo]\nscoring: {workdir: scorer_workdir}\n"))


def test_example_config_and_every_bundled_pack_load():
    cfg = load_config(Path(__file__).parents[1] / "config.example.yaml")
    assert cfg.packs == ["ai-labs"] and cfg.notify == [] and cfg.scoring.model == "sonnet"
    assert len(cfg.career_pages) == 99 and "workable:huggingface" in cfg.companies  # from the pack
    packs = available_packs()
    assert {"ai-labs", "consumer-tech", "new-grad", "ai-ml-data", "software", "hardware", "product", "quant"} <= set(packs)
    assert all(p.description for p in packs.values())
    assert all(p.companies or p.career_pages or p.github_listings for p in packs.values())


def test_packs_add_sources_after_the_configs_own_without_duplicates(tmp_path):
    (tmp_path / "mine.yaml").write_text(
        "description: my list\ncompanies: [lever:foo, ashby:bar]\n"
        "career_pages:\n  - {name: Acme, url: 'https://acme.example/jobs'}\n  - {name: Other, url: 'https://o.example'}\n",
        encoding="utf-8")
    cfg = load_config(write(tmp_path, "packs: [mine.yaml]\ncompanies: [ashby:bar, greenhouse:x]\n"
                                      "career_pages:\n  - {name: Acme Inc, url: 'https://acme.example/jobs'}\n"))
    assert cfg.companies == ["ashby:bar", "greenhouse:x", "lever:foo"]
    assert [(p.name, p.url) for p in cfg.career_pages] == [("Acme Inc", "https://acme.example/jobs"),
                                                          ("Other", "https://o.example")]  # the config's entry wins


def test_unknown_pack_lists_the_bundled_ones(tmp_path):
    with pytest.raises(ValueError, match="company pack .*nope.yaml not found .*ai-labs"):
        load_config(write(tmp_path, "packs: [nope]\n"))


def test_notify_channels_are_checked(tmp_path):
    assert load_config(write(tmp_path, "notify: [telegram, telegram]\n")).notify == ["telegram"]
    with pytest.raises(ValidationError, match="unknown notification channel"):
        load_config(write(tmp_path, "notify: [pager]\n"))


def test_model_defaults_per_backend(tmp_path):
    assert load_config(write(tmp_path, "scoring: {backend: anthropic_api}\n")).scoring.model == "claude-opus-5-5"
    with pytest.raises(ValidationError, match="scoring.model is required for the openai_api backend"):
        load_config(write(tmp_path, "scoring: {backend: openai_api}\n"))
    (tmp_path / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")  # API backends don't run claude in a workdir
    cfg = load_config(write(tmp_path, "scoring: {backend: openai_api, model: m, workdir: wd}\n"))
    assert cfg.scoring.model == "m"
