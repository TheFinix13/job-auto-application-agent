"""Repository hygiene: the code, tests and notes added in steps 1 to 4 are fit to publish."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

# Written with character classes so that this file does not match itself.
BANNED = re.compile(
    r"[c]laude|[c]ursor|[a]nthropic|[o]penai|[c]hatgpt|[c]opilot|[g]emini|\b[A]I\b|\b[L]LM\b|"
    r"[g]enerated by|[c]o-authored|[m]ade with",
    re.IGNORECASE,
)


def own_files():
    files = [ROOT / "pyproject.toml", ROOT / "profile.template.yaml"]
    files += sorted((ROOT / "src").rglob("*.py"))
    files += sorted(p for p in (ROOT / "tests").glob("*.py"))
    files += sorted((ROOT / "docs").glob("handover-*.md"))
    return [f for f in files if f.exists()]


@pytest.mark.parametrize("path", own_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_tool_or_author_mentions(path):
    hits = [m.group() for m in BANNED.finditer(path.read_text(encoding="utf-8"))]
    assert not hits, f"{path.name}: {hits}"


def test_fixtures_are_exact_copies_of_the_published_examples():
    examples = ROOT / "public-draft" / "examples"
    if not examples.exists():
        pytest.skip("public-draft/examples is not present")
    names = sorted(p.name for p in FIXTURES.iterdir())
    assert names == sorted(p.name for p in examples.iterdir())
    for name in names:
        assert (FIXTURES / name).read_bytes() == (examples / name).read_bytes()


def test_tests_use_no_network_modules():
    network = re.compile(r"^\s*(?:import|from)\s+(?:requests|urllib\.request|http\.client|socket|httpx|aiohttp)\b", re.M)
    for path in own_files():
        if path.suffix == ".py" and path.name != Path(__file__).name:
            assert not network.search(path.read_text(encoding="utf-8")), path.name
