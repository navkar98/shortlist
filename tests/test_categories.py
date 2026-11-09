from shortlist.categories import FIELDS, LABELS, STAGES, groups


def test_titles_fall_into_stages_and_fields():
    assert groups("Machine Learning Engineer New Grad - Search Ads") == ("new-grad", "ml")
    assert groups("PhD University Grad Machine Learning Engineer (USA)") == ("new-grad", "phd", "ml")
    assert groups("Research Scientist Graduate (TikTok Recommendation) - 2026 Start (PhD)") == (
        "new-grad", "phd", "research", "ml")
    assert groups("Software Engineer, Machine Learning RecSys") == ("ml", "software")
    assert groups("Senior Data Scientist") == ("senior", "data")
    assert groups("Quantitative Researcher") == ("research", "quant")
    assert groups("ML Research Intern, Summer 2026") == ("intern", "research", "ml")
    assert groups("Account Executive") == ()


def test_whole_words_only():
    assert groups("Senior HTML Developer") == ("senior", "software")  # "ml" is not in "HTML"
    assert "intern" not in groups("International Payments Engineer")
    assert "ml" not in groups("Email Marketing Lead")


def test_every_group_has_a_label():
    assert set(LABELS) == set(STAGES) | set(FIELDS) and all(LABELS.values())
