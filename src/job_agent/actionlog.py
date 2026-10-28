"""Append-only action log: one JSON line per action in logs/YYYY-MM-DD.jsonl.

Each line holds the time, the action, the source, the role id, a hash of the
inputs and the outcome. Credentials, cookies, document text and email
contents are never passed to this module.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def log_action(
    log_dir: Path | str | None,
    action: str,
    source: str,
    role_id: str,
    inputs_hash: str,
    outcome: str,
    detail: str = "",
    now: datetime | None = None,
) -> None:
    if log_dir is None:
        return
    now = now or datetime.now(timezone.utc)
    record = {
        "timestamp": now.replace(microsecond=0).isoformat(),
        "action": action,
        "source": source,
        "role_id": role_id,
        "inputs_hash": inputs_hash,
        "outcome": outcome,
    }
    if detail:
        record["detail"] = detail
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{now.date().isoformat()}.jsonl"
    line = json.dumps(record, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
