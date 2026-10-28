"""Step 1: the profile.yaml template and loader."""

from pathlib import Path

import pytest
import yaml

from job_agent.profile import ProfileIncomplete, ensure_ready, load_profile, missing_required

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "profile.template.yaml"


def test_template_exists_and_is_valid_yaml():
    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_template_has_the_same_keys_as_the_sample(fixtures_dir):
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    sample = yaml.safe_load((fixtures_dir / "profile.sample.yaml").read_text(encoding="utf-8"))
    assert set(sample) <= set(template)


def test_template_holds_no_personal_values():
    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    for key in ("name", "email", "phone", "location_city"):
        assert data[key] in ("", None)
    assert data["salary_floor_gbp"] is None
    assert data["right_to_work"]["needs_sponsorship"] is None


def test_unfilled_template_is_refused():
    with pytest.raises(ProfileIncomplete) as err:
        ensure_ready(load_profile(TEMPLATE))
    for field in ("name", "email", "phone", "right_to_work", "salary_floor_gbp", "preferred_cities"):
        assert field in err.value.missing


def test_sample_profile_is_ready(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile.sample.yaml")
    assert missing_required(profile) == []
    ensure_ready(profile)


def test_right_to_work_needs_both_answers(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile.sample.yaml")
    profile["right_to_work"]["needs_sponsorship"] = None
    assert missing_required(profile) == ["right_to_work"]


def test_false_and_zero_count_as_answers(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile.sample.yaml")
    profile["right_to_work"]["needs_sponsorship"] = False
    profile["salary_floor_gbp"] = 0
    assert missing_required(profile) == []


def test_empty_list_and_blank_string_are_missing(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile.sample.yaml")
    profile["preferred_cities"] = []
    profile["email"] = "   "
    assert set(missing_required(profile)) == {"preferred_cities", "email"}


def test_required_fields_come_from_the_file(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile.sample.yaml")
    profile["required_fields"] = ["name"]
    profile["email"] = ""
    assert missing_required(profile) == []


def test_resolved_facts_are_exposed(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("resolved_facts: [employment-02]\n", encoding="utf-8")
    assert load_profile(path)["resolved_facts"] == ["employment-02"]


def test_load_rejects_a_non_mapping(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_profile(path)
