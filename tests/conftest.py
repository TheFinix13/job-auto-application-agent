import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis.configuration import set_hypothesis_home_dir

# Keep Hypothesis's working files out of the repository.
set_hypothesis_home_dir(tempfile.mkdtemp(prefix="hypothesis-"))

# No example database: keeps stray .hypothesis/ directories out of the repository.
settings.register_profile(
    "job-agent",
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("job-agent")

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def sample_tracker_csv(tmp_path) -> Path:
    """A writable copy of the sample tracker."""
    dst = tmp_path / "tracker.csv"
    dst.write_bytes((FIXTURES / "tracker.sample.csv").read_bytes())
    return dst
