import json
import logging
from pathlib import Path

import httpx

from shortlist.config import Listings
from shortlist.details import greenhouse_job_id
from shortlist.listings import fetch_listings, parse_listings

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = json.loads((FIXTURES / "listings_simplify_sample.json").read_text(encoding="utf-8"))  # raw entries
NOW = max(x["date_updated"] for x in SAMPLE)  # roughly when the sample was captured
SOURCE = Listings(name="SimplifyJobs new-grad", url="https://raw.example.com/listings.json",
                  keep_categories=["AI/ML/Data"], keep_degrees=["PhD"], max_age_days=120,
                  drop_sponsorship=["U.S. Citizenship is Required", "Does Not Offer Sponsorship"])


def test_listing_filters():
    kept = {j.url for j in parse_listings(SAMPLE, SOURCE, NOW)}
    dropped = lambda pred: [x for x in SAMPLE if pred(x)]
    assert dropped(lambda x: not x["active"]) and not any(x["url"] in kept for x in dropped(lambda x: not x["active"]))
    no_fit = dropped(lambda x: x["active"] and x["category"] != "AI/ML/Data" and "PhD" not in x["degrees"])
    assert no_fit and not any(x["url"] in kept for x in no_fit)
    citizens = dropped(lambda x: x["sponsorship"] == "U.S. Citizenship is Required")
    assert citizens and not any(x["url"] in kept for x in citizens)
    old = dropped(lambda x: NOW - x["date_posted"] > 120 * 86400)
    assert old and not any(x["url"] in kept for x in old)
    phd_other_field = dropped(lambda x: x["active"] and x["category"] != "AI/ML/Data" and "PhD" in x["degrees"]
                              and x["sponsorship"] == "Other" and NOW - x["date_posted"] <= 120 * 86400)
    assert phd_other_field and all(x["url"] in kept for x in phd_other_field)


def test_board_links_get_the_board_identity():
    jobs = parse_listings(SAMPLE, SOURCE, NOW)
    by_host = {j.url.split("/")[2]: j for j in jobs}
    gh = by_host["job-boards.greenhouse.io"]
    assert (gh.ats, gh.job_id) == ("greenhouse", gh.url.rstrip("/").split("/")[-1])
    assert by_host["jobs.lever.co"].ats == "lever" and by_host["jobs.ashbyhq.com"].ats == "ashby"
    tiktok = by_host["lifeattiktok.com"]
    assert (tiktok.ats, tiktok.slug, tiktok.company) == ("simplify", "tiktok", "TikTok")
    for j in jobs:
        assert j.detail_url == j.url and j.description.startswith("[Listed on SimplifyJobs new-grad] category: ")
        assert j.description.endswith(f"link: {j.url}")


def test_fetch_listings_skips_known_greenhouse_ids_and_failed_sources(caplog):
    with_gh_jid = [x for x in parse_listings(SAMPLE, SOURCE, NOW) if greenhouse_job_id(x.url)]
    assert len(with_gh_jid) >= 2
    known = greenhouse_job_id(with_gh_jid[0].url)

    def handler(request):
        if request.url.path == "/listings.json":
            return httpx.Response(200, json=SAMPLE)
        return httpx.Response(404, text="Not Found")

    caplog.set_level(logging.INFO, logger="shortlist.listings")
    broken = SOURCE.model_copy(update={"name": "broken", "url": "https://raw.example.com/missing.json"})
    jobs = fetch_listings([SOURCE, broken], skip_greenhouse_ids={known}, now=NOW, transport=httpx.MockTransport(handler))
    assert with_gh_jid[0].url not in {j.url for j in jobs} and with_gh_jid[1].url in {j.url for j in jobs}
    assert f"SimplifyJobs new-grad: {len(SAMPLE)} listings" in caplog.text and "(1 already fetched" in caplog.text
    assert "broken: skipped (HTTP 404)" in caplog.text
