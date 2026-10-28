"""Step 1: the facts.yaml builder."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from job_agent.facts import (
    Fact,
    build_facts_file,
    load_facts,
    parse_master_facts,
)

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def sample_facts(fixtures_dir):
    return parse_master_facts((fixtures_dir / "master_facts.sample.md").read_text(encoding="utf-8"))


def by_text(facts, fragment):
    matches = [f for f in facts if fragment in f.text]
    assert len(matches) == 1, f"{fragment!r} matched {len(matches)} facts"
    return matches[0]


def test_every_fact_has_a_unique_stable_id(sample_facts):
    ids = [f.id for f in sample_facts]
    assert len(ids) == len(set(ids))
    assert all(i == i.lower() and " " not in i for i in ids)


def test_ids_do_not_depend_on_the_other_sections(fixtures_dir):
    text = (fixtures_dir / "master_facts.sample.md").read_text(encoding="utf-8")
    before = {f.id: f.text for f in parse_master_facts(text)}
    after = {f.id: f.text for f in parse_master_facts(text + "\n## 6. Awards\n- A prize. [V]\n")}
    assert all(after[i] == t for i, t in before.items())
    assert "awards-01" in after


def test_status_tags_are_read(sample_facts):
    assert by_text(sample_facts, "BSc Computer Science").status == "V"
    assert by_text(sample_facts, "MSc Data Science").status == "B"
    assert by_text(sample_facts, "Name: Alex Example").status == "V"


def test_unknown_facts_keep_the_question_as_a_note(sample_facts):
    email = by_text(sample_facts, "Email")
    assert email.status == "U"
    assert "which address to print" in email.note
    rtw = by_text(sample_facts, "Right to work")
    assert rtw.status == "U"
    pct = by_text(sample_facts, "40%")
    assert pct.status == "U"
    assert "how it was measured" in pct.note


def test_a_line_with_no_status_tag_is_treated_as_unknown(sample_facts):
    facts = parse_master_facts("## 1. Notes\n- A claim with no tag.\n")
    assert [f.status for f in facts] == ["U"]
    assert "no status tag" in facts[0].note


def test_legend_lines_before_the_first_section_are_not_facts(sample_facts):
    assert not any("verified against a primary document" in f.text for f in sample_facts)


def test_employment_header_is_split_into_parts(sample_facts):
    job = by_text(sample_facts, "Example Corp")
    assert job.section == "employment"
    assert job.status == "V"
    assert job.employer == "Example Corp"
    assert job.title == "Software Engineer"
    assert job.dates == "06/2023 to 08/2025"
    assert "REST APIs in Java" in job.text


def test_bullets_under_a_job_point_at_the_job(sample_facts):
    job = by_text(sample_facts, "Example Corp")
    bullet = by_text(sample_facts, "40%")
    assert bullet.parent == job.id


def test_skill_rows_without_evidence_are_not_claimed(sample_facts):
    languages = by_text(sample_facts, "Python, Java")
    assert languages.status == "B"
    assert languages.evidence == "Sections 3 and 4"
    missing = by_text(sample_facts, "Kubernetes")
    assert missing.status == "N"


def test_facts_yaml_round_trips(tmp_path, fixtures_dir, sample_facts):
    out = tmp_path / "facts.yaml"
    build_facts_file(fixtures_dir / "master_facts.sample.md", out)
    raw = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert isinstance(raw["facts"], list)
    loaded = load_facts(out)
    assert loaded == sample_facts
    assert all(isinstance(f, Fact) for f in loaded)


def test_build_is_deterministic(tmp_path, fixtures_dir):
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    build_facts_file(fixtures_dir / "master_facts.sample.md", a)
    build_facts_file(fixtures_dir / "master_facts.sample.md", b)
    assert a.read_bytes() == b.read_bytes()


def test_builder_refuses_to_overwrite_its_own_input(tmp_path):
    md = tmp_path / "master_facts.md"
    md.write_text("## 1. Notes\n- A fact. [V]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        build_facts_file(md, md)


def test_cli_builds_the_file(tmp_path, fixtures_dir):
    out = tmp_path / "facts.yaml"
    run = subprocess.run(
        [sys.executable, "-m", "job_agent.facts", str(fixtures_dir / "master_facts.sample.md"), "-o", str(out)],
        env={"PYTHONPATH": str(SRC), "PATH": ""},
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert out.exists()
    assert "facts written" in run.stdout
