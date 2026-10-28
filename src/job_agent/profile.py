"""Load profile.yaml and check that the required answers are filled in."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_REQUIRED = ("name", "email", "phone", "right_to_work", "salary_floor_gbp", "preferred_cities")


class ProfileIncomplete(Exception):
    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("profile.yaml is missing: " + ", ".join(missing))


def load_profile(path: Path | str) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True  # numbers and booleans, including 0 and False, are answers


def _right_to_work_filled(value: Any) -> bool:
    if not isinstance(value, dict):
        return _filled(value)
    return _filled(value.get("status")) and value.get("needs_sponsorship") is not None


def missing_required(profile: dict[str, Any]) -> list[str]:
    required = profile.get("required_fields") or list(DEFAULT_REQUIRED)
    missing = []
    for field in required:
        value = profile.get(field)
        ok = _right_to_work_filled(value) if field == "right_to_work" else _filled(value)
        if not ok:
            missing.append(field)
    return missing


def ensure_ready(profile: dict[str, Any]) -> None:
    missing = missing_required(profile)
    if missing:
        raise ProfileIncomplete(missing)


def resolved_fact_ids(profile: dict[str, Any]) -> set[str]:
    return set(profile.get("resolved_facts") or [])
