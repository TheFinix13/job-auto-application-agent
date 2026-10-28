"""The approval gate: the only place a submit can happen.

A row may be submitted only when all of these hold, checked again at the
moment of submitting and under the tracker lock:

1. its status is exactly ``Approved``;
2. ``approved_by`` is exactly ``user``;
3. ``approved_at`` holds a timestamp;
4. the hash of the CV and cover letter on disk equals the hash stored when
   the row was approved;
5. the row is not flagged ``submit_unconfirmed`` (an earlier attempt that
   raised an error, so the site may already hold an application).

Editing either document after approval resets the row to ``Ready`` and clears
the approval. There is no option that skips a check.

``Gate.submit`` receives the function that presses Submit and is the only
place that calls it. The form filler (a later step) supplies that function.

What this module cannot do: it cannot prove who typed the word ``user``. The
checks make an approval a deliberate, recorded act that goes stale when a
document changes; they do not authenticate the person. See the hand-over
note for the limits.

Usage:
    python -m job_agent.gate --tracker tracker.csv --base-dir . approve ROW_ID
    python -m job_agent.gate --tracker tracker.csv --base-dir . check ROW_ID
    python -m job_agent.gate --tracker tracker.csv --base-dir . reconcile
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_agent.actionlog import log_action
from job_agent.tracker import Tracker, UnknownRow

APPROVER = "user"
UNCONFIRMED_FLAG = "submit_unconfirmed"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


class GateRefused(Exception):
    """The gate said no. ``reasons`` lists every failed check."""

    def __init__(self, reasons: list[str] | str):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__("; ".join(self.reasons))


class SubmitFailed(Exception):
    """The submit function reported that nothing was submitted."""


def valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not _TIMESTAMP.match(value.strip()):
        return False
    try:
        datetime.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def _resolve_inside(base_dir: Path, relative: str) -> Path:
    if not relative or not relative.strip():
        raise GateRefused("a document path is empty")
    if Path(relative).is_absolute():
        raise GateRefused(f"document path must be relative: {relative}")
    base = Path(base_dir).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise GateRefused(f"document path leaves the base directory: {relative}")
    if not path.is_file():
        raise GateRefused(f"document is missing: {relative}")
    return path


def hash_documents(base_dir: Path | str, cv_path: str, letter_path: str) -> str:
    """Hash of the CV and the letter. The two are labelled, so swapping them changes the hash."""
    digest = hashlib.sha256()
    for label, relative in (("cv", cv_path), ("letter", letter_path)):
        data = _resolve_inside(Path(base_dir), relative).read_bytes()
        digest.update(label.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return "sha256:" + digest.hexdigest()


def _now_iso(now: datetime) -> str:
    return now.replace(microsecond=0).isoformat()


class Gate:
    def __init__(
        self,
        tracker: Tracker,
        base_dir: Path | str,
        log_dir: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.tracker = tracker
        self.base_dir = Path(base_dir)
        self.log_dir = log_dir
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- helpers --
    def _log(self, action: str, row_id: str, inputs_hash: str, outcome: str, detail: str = "") -> None:
        log_action(self.log_dir, action, "gate", row_id, inputs_hash, outcome, detail, now=self._clock())

    def _current_hash(self, row: dict[str, str]) -> str:
        return hash_documents(self.base_dir, row["cv_path"], row["letter_path"])

    def _problems(self, row: dict[str, str]) -> tuple[list[str], str, bool]:
        """Return (reasons it cannot be submitted, current hash or '', documents changed)."""
        reasons: list[str] = []
        if row["status"] != "Approved":
            reasons.append(f"status is {row['status']!r}, not 'Approved'")
        if row["approved_by"] != APPROVER:
            reasons.append(f"approved_by is {row['approved_by']!r}, not {APPROVER!r}")
        if not valid_timestamp(row["approved_at"]):
            reasons.append("approved_at holds no valid timestamp")
        stored = row["approved_doc_hash"]
        if not stored:
            reasons.append("no document hash was stored at approval")
        if UNCONFIRMED_FLAG in row.get("flags", ""):
            reasons.append("an earlier submit attempt was not confirmed; check the site and clear the flag")
        current, changed = "", False
        try:
            current = self._current_hash(row)
        except GateRefused as err:
            reasons.extend(err.reasons)
        else:
            if stored and current != stored:
                changed = True
                reasons.append("the documents changed since approval")
        return reasons, current, changed

    def _reset_if_changed(self, tx, row_id: str, row: dict[str, str], changed: bool) -> bool:
        if changed and row["status"] == "Approved":
            tx.reset(row_id, "Ready", actor="gate")
            return True
        return False

    # -- public --
    def check(self, row_id: str) -> list[str]:
        """Reasons the row cannot be submitted now. An empty list means it can. Changes nothing."""
        row = {r["id"]: r for r in self.tracker.read()}.get(row_id)
        if row is None:
            raise UnknownRow(row_id)
        return self._problems(row)[0]

    def approve(self, row_id: str, approver: str) -> str:
        """Record the user's approval of the documents as they are now. Returns the stored hash."""
        digest = ""
        try:
            with self.tracker.edit() as tx:
                row = tx.get(row_id)
                if approver != APPROVER:
                    raise GateRefused(f"only {APPROVER!r} can approve, not {approver!r}")
                record_blank = not (row["approved_by"] and row["approved_at"] and row["approved_doc_hash"])
                if row["status"] == "Ready":
                    pass
                elif row["status"] == "Approved" and record_blank:
                    pass  # status was set by hand; this completes the record
                else:
                    raise GateRefused(f"row is {row['status']!r}; only a Ready row can be approved")
                digest = self._current_hash(row)
                if row["status"] == "Ready":
                    tx.set_status(row_id, "Approved", actor="gate")
                tx.update(
                    row_id,
                    {
                        "approved_by": APPROVER,
                        "approved_at": _now_iso(self._clock()),
                        "approved_doc_hash": digest,
                    },
                    actor="gate",
                )
        except GateRefused as err:
            self._log("approve", row_id, digest, "refused", "; ".join(err.reasons))
            raise
        self._log("approve", row_id, digest, "ok")
        return digest

    def reconcile(self) -> list[str]:
        """Reset every Approved row whose documents changed since approval. Returns their ids."""
        reset: list[str] = []
        with self.tracker.edit() as tx:
            for row in tx.rows:
                if row["status"] != "Approved":
                    continue
                _, current, changed = self._problems(row)
                if self._reset_if_changed(tx, row["id"], row, changed):
                    reset.append(row["id"])
                    self._log("reset", row["id"], current, "ok", "documents changed after approval")
        if not reset:
            self._log("reconcile", "", "", "ok", "nothing to reset")
        return reset

    def submit(self, row_id: str, submit_fn: Callable[[dict[str, str]], bool]) -> None:
        """Submit one row if, and only if, every check passes."""
        refusal: GateRefused | None = None
        digest = ""
        error: BaseException | None = None
        with self.tracker.edit() as tx:
            row = tx.get(row_id)
            reasons, digest, changed = self._problems(row)
            if reasons:
                did_reset = self._reset_if_changed(tx, row_id, row, changed)
                refusal = GateRefused(reasons)
                self._log("submit", row_id, digest, "refused", "; ".join(reasons) + ("; reset to Ready" if did_reset else ""))
            else:
                try:
                    done = submit_fn(dict(row))
                except BaseException as exc:  # the site may or may not hold an application now
                    flags = ";".join(f for f in (row.get("flags", ""), UNCONFIRMED_FLAG) if f)
                    tx.update(row_id, {"flags": flags}, actor="gate")
                    self._log("submit", row_id, digest, "unconfirmed", type(exc).__name__)
                    error = exc
                else:
                    if done is not True:
                        self._log("submit", row_id, digest, "failed", "submit function reported failure")
                        error = SubmitFailed("the submit function reported that nothing was submitted")
                    else:
                        tx.set_status(row_id, "Applied", actor="gate")
                        tx.update(row_id, {"date_applied": self._clock().date().isoformat()}, actor="gate")
                        self._log("submit", row_id, digest, "ok")
        if refusal is not None:
            raise refusal
        if error is not None:
            raise error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approval gate for application rows")
    parser.add_argument("--tracker", type=Path, default=Path("tracker.csv"))
    parser.add_argument("--base-dir", type=Path, default=Path("."))
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("approve", help="record the user's approval of a Ready row").add_argument("row_id")
    sub.add_parser("check", help="say whether a row could be submitted now").add_argument("row_id")
    sub.add_parser("reconcile", help="reset Approved rows whose documents changed")
    args = parser.parse_args(argv)

    gate = Gate(Tracker(args.tracker), args.base_dir, log_dir=args.log_dir)
    try:
        if args.command == "approve":
            print(f"approved {args.row_id}: {gate.approve(args.row_id, approver=APPROVER)}")
        elif args.command == "check":
            reasons = gate.check(args.row_id)
            print("can be submitted" if not reasons else "cannot be submitted:\n- " + "\n- ".join(reasons))
            return 0 if not reasons else 1
        else:
            reset = gate.reconcile()
            print("reset to Ready: " + (", ".join(reset) if reset else "none"))
    except GateRefused as err:
        print("refused: " + "; ".join(err.reasons), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
