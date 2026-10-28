"""The tracker: tracker.csv is the file of record.

Rules this module enforces:

* Every write is read-modify-write under an exclusive file lock, and the new
  file replaces the old one in a single step, so concurrent writers lose no
  rows and a crash never leaves half a file.
* Rows are never deleted. There is no delete call.
* Statuses only move forward: Found, Tailored, Ready, Approved, Applied,
  Interview, Rejected, Offer. The one way back is ``reset``, which is limited
  to Found, Tailored and Ready and clears any approval.
* Only the gate and the user may set Approved or Applied, and Approved can
  only follow Ready, Applied only follow Approved.
* A cell a human has edited is recorded in ``human_edited_cols`` and the
  scanner never overwrites it. Edits made by hand in a spreadsheet are found by
  comparing the file with a snapshot of what this module last wrote.

Files next to the CSV: ``.<name>.lock`` (the lock) and
``.<name>.snapshot.json`` (the last written cells).
"""

from __future__ import annotations

import csv
import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

STATUSES = ("Found", "Tailored", "Ready", "Approved", "Applied", "Interview", "Rejected", "Offer")
RESET_TARGETS = ("Found", "Tailored", "Ready")
ACTORS = ("agent", "gate", "user", "human")  # "human" is only valid for update()
STATUS_ACTORS = ("agent", "gate", "user")

APPROVAL_COLUMNS = ("approved_by", "approved_at", "approved_doc_hash")
EXTRA_COLUMNS = (
    "flags",
    "human_edited_cols",
    "cv_path",
    "letter_path",
    *APPROVAL_COLUMNS,
    "date_applied",
)
GATE_ONLY_COLUMNS = (*APPROVAL_COLUMNS, "date_applied")
ALWAYS_PROTECTED = ("id", "status", "human_edited_cols")
UNTRACKED_FOR_HUMAN_EDITS = ("id", "human_edited_cols")


class TrackerError(Exception):
    pass


class TrackerLockTimeout(TrackerError):
    pass


class DuplicateRow(TrackerError):
    pass


class UnknownRow(TrackerError):
    pass


class ColumnError(TrackerError):
    pass


class ProtectedColumn(TrackerError):
    pass


class StatusError(TrackerError):
    pass


class BackwardTransition(StatusError):
    pass


class NotPermitted(StatusError):
    pass


@dataclass
class UpdateResult:
    applied: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def _status_index(status: str) -> int:
    try:
        return STATUSES.index(status or "Found")
    except ValueError:
        raise StatusError(f"unrecognised status {status!r} in the tracker") from None


def _split_cols(value: str) -> list[str]:
    return [c for c in (value or "").split(";") if c]


class Txn:
    """One open read-modify-write. Obtain it from ``Tracker.edit()``."""

    def __init__(self, columns: list[str], rows: list[dict[str, str]]):
        self.columns = columns
        self._rows = rows
        self._index = {r["id"]: r for r in rows}

    # -- reading --
    @property
    def rows(self) -> list[dict[str, str]]:
        return [dict(r) for r in self._rows]

    def get(self, row_id: str) -> dict[str, str]:
        return dict(self._row(row_id))

    def _row(self, row_id: str) -> dict[str, str]:
        try:
            return self._index[row_id]
        except KeyError:
            raise UnknownRow(row_id) from None

    def new_id(self, prefix: str = "J") -> str:
        numbers = [int(i[len(prefix) :]) for i in self._index if i.startswith(prefix) and i[len(prefix) :].isdigit()]
        return f"{prefix}{max(numbers, default=0) + 1:03d}"

    # -- adding --
    def add_row(self, values: dict[str, str]) -> str:
        values = dict(values)
        unknown = set(values) - set(self.columns)
        if unknown:
            raise ColumnError(f"unknown columns: {sorted(unknown)}")
        row_id = values.get("id") or self.new_id()
        if row_id in self._index:
            raise DuplicateRow(row_id)
        status = values.get("status") or "Found"
        if status != "Found":
            raise StatusError("a new row must start at Found")
        row = {c: "" for c in self.columns}
        row.update({k: "" if v is None else str(v) for k, v in values.items()})
        row["id"], row["status"] = row_id, "Found"
        self._rows.append(row)
        self._index[row_id] = row
        return row_id

    # -- cell updates --
    def update(self, row_id: str, changes: dict[str, str], actor: str = "agent") -> UpdateResult:
        if actor not in ACTORS:
            raise TrackerError(f"unknown actor {actor!r}")
        row = self._row(row_id)
        unknown = set(changes) - set(self.columns)
        if unknown:
            raise ColumnError(f"unknown columns: {sorted(unknown)}")
        for column in changes:
            if column in ALWAYS_PROTECTED or (column in GATE_ONLY_COLUMNS and actor != "gate"):
                raise ProtectedColumn(f"{actor} may not write {column!r} directly")
        result = UpdateResult()
        edited = set(_split_cols(row["human_edited_cols"]))
        for column, value in changes.items():
            value = "" if value is None else str(value)
            if actor == "agent" and column in edited:
                result.skipped.append(column)
                continue
            if actor == "human" and row[column] != value:
                edited.add(column)
            row[column] = value
            result.applied[column] = value
        row["human_edited_cols"] = ";".join(sorted(edited))
        return result

    # -- statuses --
    def set_status(self, row_id: str, new: str, actor: str = "agent") -> None:
        if actor not in STATUS_ACTORS:
            raise TrackerError(f"unknown actor {actor!r}")
        if new not in STATUSES:
            raise StatusError(f"unknown status {new!r}")
        row = self._row(row_id)
        current = row["status"] or "Found"
        if new == current:
            return
        if new in ("Approved", "Applied") and actor == "agent":
            raise NotPermitted(f"only the gate or the user may set {new}")
        if _status_index(new) < _status_index(current):
            raise BackwardTransition(f"{current} -> {new} goes backwards; use reset")
        if new == "Approved" and current != "Ready":
            raise StatusError("Approved can only follow Ready")
        if new == "Applied" and current != "Approved":
            raise StatusError("Applied can only follow Approved")
        row["status"] = new

    def reset(self, row_id: str, to: str = "Ready", actor: str = "gate") -> None:
        if actor not in STATUS_ACTORS:
            raise TrackerError(f"unknown actor {actor!r}")
        if to not in RESET_TARGETS:
            raise StatusError(f"cannot reset to {to}")
        row = self._row(row_id)
        current = row["status"] or "Found"
        if _status_index(to) >= _status_index(current):
            raise StatusError(f"reset must move backwards ({current} -> {to})")
        index = _status_index(current)
        if index >= _status_index("Applied") and current != "Rejected":
            raise NotPermitted(f"a row that is {current} cannot be reset")
        if current == "Rejected" and actor != "user":
            raise NotPermitted("only the user may reopen a Rejected row")
        if current == "Approved" and actor == "agent":
            raise NotPermitted("only the gate or the user may reset an Approved row")
        row["status"] = to
        for column in APPROVAL_COLUMNS:
            row[column] = ""


class Tracker:
    def __init__(self, path: Path | str, lock_timeout: float = 30.0):
        self.path = Path(path)
        self.lock_timeout = lock_timeout
        self._lock_path = self.path.with_name(f".{self.path.name}.lock")
        self._snapshot_path = self.path.with_name(f".{self.path.name}.snapshot.json")

    # -- locking --
    @contextmanager
    def _locked(self, exclusive: bool) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = time.monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, mode)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TrackerLockTimeout(f"could not lock {self.path} within {self.lock_timeout}s") from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)  # closing releases the flock

    # -- file io --
    def _load(self) -> tuple[list[str], list[dict[str, str]]]:
        with open(self.path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            columns = list(reader.fieldnames or [])
            rows = [{c: (r.get(c) or "") for c in columns} for r in reader]
        if "id" not in columns:
            raise TrackerError(f"{self.path} has no 'id' column")
        for extra in ("status", *EXTRA_COLUMNS):
            if extra not in columns:
                columns.append(extra)
                for row in rows:
                    row[extra] = ""
        ids = [r["id"] for r in rows]
        if len(set(ids)) != len(ids):
            raise DuplicateRow(f"{self.path} already contains repeated ids")
        return columns, rows

    def _write(self, columns: list[str], rows: list[dict[str, str]]) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.tmp.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _write_snapshot(self, rows: list[dict[str, str]]) -> None:
        snapshot = {r["id"]: {k: v for k, v in r.items() if k not in UNTRACKED_FOR_HUMAN_EDITS} for r in rows}
        tmp = self._snapshot_path.with_name(self._snapshot_path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._snapshot_path)

    def _record_hand_edits(self, rows: list[dict[str, str]]) -> None:
        if not self._snapshot_path.exists():
            return
        snapshot = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        for row in rows:
            last = snapshot.get(row["id"])
            if last is None:
                continue
            changed = {c for c, v in last.items() if c in row and row[c] != v and c not in UNTRACKED_FOR_HUMAN_EDITS}
            if changed:
                row["human_edited_cols"] = ";".join(sorted(set(_split_cols(row["human_edited_cols"])) | changed))

    # -- public --
    def read(self) -> list[dict[str, str]]:
        with self._locked(exclusive=False):
            return self._load()[1]

    @contextmanager
    def edit(self) -> Iterator[Txn]:
        with self._locked(exclusive=True):
            columns, rows = self._load()
            self._record_hand_edits(rows)
            txn = Txn(columns, rows)
            yield txn
            self._write(columns, txn._rows)
            self._write_snapshot(txn._rows)
