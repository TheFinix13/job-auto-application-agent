"""Step 3: de-duplication."""

import csv

import pytest

from job_agent.dedupe import (
    DedupeIndex,
    add_new_rows,
    canonical_url,
    normalise_company,
    normalise_location,
    normalise_title,
)
from job_agent.tracker import Tracker


@pytest.fixture
def sample_rows(fixtures_dir):
    with open(fixtures_dir / "tracker.sample.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def cand(**over):
    row = {
        "source": "Indeed",
        "company": "G-Research",
        "role": "Graduate Machine Learning Engineer",
        "location": "London",
        "link": "https://uk.indeed.com/viewjob?jk=abc123",
    }
    row.update(over)
    return row


# --- normalisers -------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("HTTPS://WWW.TargetJobs.co.uk/jobs/x-1/", "https://targetjobs.co.uk/jobs/x-1"),
        ("http://targetjobs.co.uk/jobs/x-1", "https://targetjobs.co.uk/jobs/x-1"),
        ("https://targetjobs.co.uk/jobs/x-1#apply", "https://targetjobs.co.uk/jobs/x-1"),
        ("https://targetjobs.co.uk/jobs/x-1?utm_source=a&utm_medium=b", "https://targetjobs.co.uk/jobs/x-1"),
        ("https://x.com/j?id=5&gclid=zz&fbclid=1", "https://x.com/j?id=5"),
        ("https://x.com/j?b=2&a=1", "https://x.com/j?a=1&b=2"),
    ],
)
def test_canonical_url_ignores_noise(a, b):
    assert canonical_url(a) == canonical_url(b) == b


def test_canonical_url_keeps_the_job_identifying_parameter():
    assert canonical_url("https://uk.indeed.com/viewjob?jk=abc") != canonical_url("https://uk.indeed.com/viewjob?jk=xyz")


def test_canonical_url_of_nothing_is_empty():
    assert canonical_url("") == ""
    assert canonical_url("not a url") == ""


@pytest.mark.parametrize(
    "a,b",
    [
        ("G-Research Ltd", "G-Research"),
        ("NFU Mutual", "nfu mutual"),
        ("Arcadis UK Limited", "Arcadis"),
        ("Barclays PLC", "Barclays"),
        ("Johnson & Johnson", "Johnson and Johnson"),
        ("Société Générale", "Societe Generale"),
    ],
)
def test_company_names_match_across_styles(a, b):
    assert normalise_company(a) == normalise_company(b)


def test_different_companies_stay_different():
    assert normalise_company("Arcadis") != normalise_company("Arup")


@pytest.mark.parametrize(
    "a,b",
    [
        ("Graduate Machine Learning Engineer", "graduate machine-learning engineer"),
        ("Graduate Data Scientist (London)", "Graduate Data Scientist"),
        ("Grad Data Scientist", "Graduate Data Scientist"),
        ("Data  Scientist - Graduate Programme", "Data Scientist - Graduate Programme"),
    ],
)
def test_titles_match_across_styles(a, b):
    assert normalise_title(a) == normalise_title(b)


def test_level_words_and_years_still_separate_roles():
    assert normalise_title("Data Scientist") != normalise_title("Senior Data Scientist")
    assert normalise_title("Graduate Programme 2026") != normalise_title("Graduate Programme 2027")


def test_locations_ignore_country_and_working_pattern():
    assert normalise_location("London, UK") == normalise_location("london")
    assert normalise_location("Stratford-upon-Avon (hybrid)") == normalise_location("Stratford upon Avon")
    assert normalise_location("Newport / Titchfield (hybrid)") == normalise_location("Titchfield, Newport")


# --- finding duplicates ------------------------------------------------------


def test_same_role_on_two_sites_is_one_row(sample_rows):
    index = DedupeIndex(sample_rows)
    assert index.find(cand()) == "S01"  # same company, title and city, different site and URL


def test_same_role_with_a_changed_url_is_one_row(sample_rows):
    moved = cand(
        source="Target Jobs",
        link="https://targetjobs.co.uk/graduate-jobs/graduate-machine-learning-engineer-249717?utm_source=email",
    )
    assert DedupeIndex(sample_rows).find(moved) == "S01"


def test_same_url_with_different_text_is_one_row(sample_rows):
    renamed = cand(company="G Research", role="ML Engineer", location="", link=sample_rows[0]["link"] + "/?utm_campaign=x")
    assert DedupeIndex(sample_rows).find(renamed) == "S01"


def test_missing_location_matches_on_company_and_title(sample_rows):
    assert DedupeIndex(sample_rows).find(cand(location="")) == "S01"


def test_same_role_in_another_city_is_a_new_row(sample_rows):
    assert DedupeIndex(sample_rows).find(cand(location="Manchester")) is None


def test_different_role_at_the_same_company_is_a_new_row(sample_rows):
    assert DedupeIndex(sample_rows).find(cand(role="Graduate Quantitative Researcher")) is None


def test_every_sample_row_finds_itself(sample_rows):
    index = DedupeIndex(sample_rows)
    assert [index.find(r) for r in sample_rows] == [r["id"] for r in sample_rows]


def test_no_sample_row_is_a_duplicate_of_another(sample_rows):
    for row in sample_rows:
        others = DedupeIndex([r for r in sample_rows if r["id"] != row["id"]])
        assert others.find(row) is None


def test_blank_company_and_title_never_match_each_other():
    index = DedupeIndex([{"id": "A", "company": "", "role": "", "location": "", "link": ""}])
    assert index.find({"company": "", "role": "", "location": "", "link": ""}) is None


def test_index_learns_added_rows():
    index = DedupeIndex([])
    first = cand()
    assert index.find(first) is None
    index.add("N1", first)
    assert index.find(cand(source="Another site", link="https://other.example/job/9")) == "N1"


# --- adding to the tracker ----------------------------------------------------


def test_add_new_rows_skips_known_roles_and_reports_them(sample_tracker_csv):
    tracker = Tracker(sample_tracker_csv)
    batch = [
        cand(),  # duplicate of S01
        cand(company="New Co", role="Graduate Analyst", location="Leeds", link="https://new.example/1"),
        cand(company="New Co", role="Graduate Analyst", location="Leeds", link="https://other.example/zz"),  # same, again
    ]
    with tracker.edit() as tx:
        added, duplicates = add_new_rows(tx, batch)
    rows = tracker.read()
    assert len(rows) == 6
    assert len(added) == 1
    assert [d.existing_id for d in duplicates] == ["S01", added[0]]
    assert {r["id"] for r in rows} >= {"S01", added[0]}


def test_add_new_rows_never_touches_existing_rows(sample_tracker_csv):
    tracker = Tracker(sample_tracker_csv)
    before = tracker.read()
    with tracker.edit() as tx:
        add_new_rows(tx, [cand()])
    after = tracker.read()
    assert after == before


def test_a_second_scan_adds_nothing(sample_tracker_csv):
    tracker = Tracker(sample_tracker_csv)
    batch = [cand(company="New Co", role="Graduate Analyst", location="Leeds", link="https://new.example/1")]
    with tracker.edit() as tx:
        first, _ = add_new_rows(tx, batch)
    with tracker.edit() as tx:
        second, dups = add_new_rows(tx, batch)
    assert len(first) == 1 and second == [] and len(dups) == 1
    assert len(tracker.read()) == 6


def test_a_rejected_role_is_still_a_known_role(sample_tracker_csv):
    tracker = Tracker(sample_tracker_csv)
    with tracker.edit() as tx:
        tx.set_status("S05", "Rejected", actor="agent")
    with tracker.edit() as tx:
        added, dups = add_new_rows(tx, [cand(company="MBDA", role="Computer Vision and Artificial Intelligence Engineer, Graduate Programme 2027", location="Bristol")])
    assert added == [] and dups[0].existing_id == "S05"
