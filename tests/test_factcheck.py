"""Step 1: the fact check (hard rule 3: no fact outside the facts file)."""

import pytest

from job_agent.factcheck import Claim, check_claims, check_draft
from job_agent.facts import parse_master_facts


@pytest.fixture
def facts(fixtures_dir):
    return parse_master_facts((fixtures_dir / "master_facts.sample.md").read_text(encoding="utf-8"))


def id_of(facts, fragment):
    return next(f.id for f in facts if fragment in f.text)


def kinds(result):
    return {v.kind for v in result.violations}


# --- accepted drafts ---------------------------------------------------------


def test_reworded_verified_fact_passes(facts):
    draft = "Developed REST APIs in Java for an internal invoicing tool at Example Corp."
    result = check_draft(draft, facts)
    assert result.ok, result.violations


def test_reworded_education_fact_passes(facts):
    draft = "Graduated with a First Class BSc in Computer Science from Sample College in 2023."
    result = check_draft(draft, facts)
    assert result.ok, result.violations


def test_beta_facts_are_usable(facts):
    result = check_draft("Studied Machine Learning on the MSc Data Science at Example University.", facts)
    assert result.ok, result.violations


def test_number_words_are_matched_against_facts(facts):
    assert check_draft("Compared three classifiers on a public dataset.", facts).ok


def test_date_formats_may_be_reworded(facts):
    assert check_draft("Worked at Example Corp from 6/2023 to 8/2025.", facts).ok


# --- rejected drafts ---------------------------------------------------------


def test_invented_number_is_rejected(facts):
    result = check_draft("Developed REST APIs in Java and cut costs by 63%.", facts)
    assert not result.ok
    assert "number" in kinds(result)
    assert any("63" in v.text for v in result.violations)


def test_invented_number_word_is_rejected(facts):
    result = check_draft("Compared five classifiers on a public dataset.", facts)
    assert "number" in kinds(result)


def test_invented_year_is_rejected(facts):
    result = check_draft("Graduated with a First Class BSc in 2021.", facts)
    assert "date" in kinds(result) or "number" in kinds(result)
    assert any("2021" in v.text for v in result.violations)


def test_invented_month_year_is_rejected(facts):
    result = check_draft("Joined Example Corp in March 2022.", facts)
    assert not result.ok


def test_invented_employer_is_rejected(facts):
    result = check_draft("Built REST APIs in Java at Acme Analytics.", facts)
    assert "entity" in kinds(result)
    assert any("Acme" in v.text for v in result.violations)


def test_invented_employer_at_start_of_sentence_is_rejected(facts):
    result = check_draft("Acme built REST APIs in Java.", facts)
    assert "entity" in kinds(result)


def test_invented_tool_is_rejected_even_in_lower_case(facts):
    result = check_draft("Deployed services with docker and tensorflow.", facts)
    assert {v.text.lower() for v in result.violations if v.kind == "tool"} >= {"docker", "tensorflow"}


def test_tool_listed_as_not_claimed_is_rejected(facts):
    result = check_draft("Experienced with Kubernetes.", facts)
    assert not result.ok
    assert any("Kubernetes" in v.text for v in result.violations)


def test_unknown_fact_may_not_appear(facts):
    result = check_draft("Reduced report generation time by 40%.", facts)
    assert not result.ok
    assert "unknown_fact" in kinds(result)


def test_unknown_fact_may_appear_once_the_user_resolves_it(facts):
    pct_id = id_of(facts, "40%")
    result = check_draft("Reduced report generation time by 40%.", facts, resolved_ids={pct_id})
    assert result.ok, result.violations


def test_resolving_one_fact_does_not_release_others(facts):
    pct_id = id_of(facts, "40%")
    result = check_draft("Email me at alex.example@example.com.", facts, resolved_ids={pct_id})
    assert not result.ok


def test_empty_facts_reject_any_number():
    assert not check_draft("Led a team of 4.", []).ok


# --- claim-level tracing -----------------------------------------------------


def test_claim_citing_a_verified_fact_passes(facts):
    fid = id_of(facts, "REST APIs in Java")
    result = check_claims([Claim("Built REST APIs in Java.", [fid])], facts)
    assert result.ok, result.violations


def test_claim_with_no_fact_id_is_rejected(facts):
    result = check_claims([Claim("Built REST APIs in Java.", [])], facts)
    assert "no_fact_id" in kinds(result)


def test_claim_citing_a_missing_fact_id_is_rejected(facts):
    result = check_claims([Claim("Built REST APIs in Java.", ["employment-99"])], facts)
    assert "bad_fact_id" in kinds(result)


def test_claim_citing_an_unknown_fact_is_rejected(facts):
    fid = id_of(facts, "40%")
    result = check_claims([Claim("Reduced report time by 40%.", [fid])], facts)
    assert "unknown_fact" in kinds(result)


def test_claim_may_only_use_values_from_the_facts_it_cites(facts):
    # Java is a real fact, but the claim cites the dissertation fact, which does not contain it.
    fid = id_of(facts, "Dissertation")
    result = check_claims([Claim("Built a model in Java.", [fid])], facts)
    assert not result.ok
