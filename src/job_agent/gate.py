"""The approval gate: the only place a submit can happen.

A row may be submitted only when all of these hold, checked again at the
moment of submitting and under the tracker lock:

1. its status is exactly ``Approved``;
2. ``approved_by`` is exactly ``user``;
3. ``approved_at`` holds a timestamp;
4. the hash of the CV, the cover letter and the form answers on disk equals
   the hash stored when the row was approved;
5. the row is not flagged ``submit_unconfirmed`` (an earlier attempt that
   raised an error, so the site may already hold an application).

Editing any of the three after approval resets the row to ``Ready`` and clears
the approval. There is no option that skips a check.

The form answers (right to work, salary, links and the like) are a flat JSON
file named by the ``answers_path`` column. They are hashed in canonical form
(sorted keys, no spacing), so a change of value changes the hash and a change
of layout does not.

``Gate.submit`` receives the function that presses Submit and is the only
place that calls it. The form filler (a later step) supplies that function.
The gate reads the three files once, hashes exactly those bytes, and hands the
same bytes to the submit function as a ``Snapshot``. The submit function must
upload from the snapshot and not read the files again. The gate also hashes
the files again after the submit returns; if they differ, the row is flagged
``docs_changed_during_submit`` so the record shows the disk moved on.

The tracker lock is not held while the submit function runs, because the
function may be slow. Before releasing the lock the gate flags the row
``submit_unconfirmed``. That flag blocks a second submit of the same row, and
it stays if the process dies mid-submit. It is cleared only when the submit
function reports a clean failure, or replaced by ``Applied`` on success.

Approval is a person's act at a terminal. The command line ``approve`` needs
an interactive terminal on both stdin and stdout, shows what is being approved,
and asks for the company name and role title to be typed. It records the
operating-system user in ``approved_os_user``. ``Gate.approve`` is called from
that one function in this module and from nowhere else in the program; a test
checks this.

What this module cannot do: it cannot prove who typed at the terminal. A
program that can open a pseudo-terminal could pass these checks, so they raise
the cost of a mistaken or careless approval; they do not authenticate the
person. See the hand-over note for the limits.

Usage:
    python -m job_agent.gate --tracker tracker.csv --base-dir . approve ROW_ID
    python -m job_agent.gate --tracker tracker.csv --base-dir . check ROW_ID
    python -m job_agent.gate --tracker tracker.csv --base-dir . reconcile
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, TextIO

from job_agent.actionlog import log_action
from job_agent.tracker import Tracker, UnknownRow

APPROVER = "user"
UNCONFIRMED_FLAG = "submit_unconfirmed"
DRIFT_FLAG = "docs_changed_during_submit"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_SCALARS = (str, int, float, bool, type(None))


class GateRefused(Exception):
    """The gate said no. ``reasons`` lists every failed check."""

    def __init__(self, reasons: list[str] | str):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__("; ".join(self.reasons))


class SubmitFailed(Exception):
    """The submit function reported that nothing was submitted."""


class SubmitNotRecorded(Exception):
    """The submit function reported success, but the row could not be marked Applied.

    The row keeps the ``submit_unconfirmed`` flag. Check the site and the tracker by hand.
    """


@dataclass(frozen=True)
class Snapshot:
    """The exact bytes and answers that were hashed. The submit function uploads from this."""

    cv: bytes
    letter: bytes
    answers: Mapping[str, Any]
    answers_json: str
    digest: str


def valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not _TIMESTAMP.match(value.strip()):
        return False
    try:
        datetime.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def _resolve_inside(base_dir: Path, relative: str, label: str = "a document") -> Path:
    if not relative or not relative.strip():
        raise GateRefused(f"{label} path is empty")
    if Path(relative).is_absolute():
        raise GateRefused(f"{label} path must be relative: {relative}")
    base = Path(base_dir).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise GateRefused(f"{label} path leaves the base directory: {relative}")
    if not path.is_file():
        raise GateRefused(f"{label} is missing: {relative}")
    return path


def _canonical_answers(raw: bytes, relative: str) -> tuple[dict[str, Any], str]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GateRefused(f"form answers are not valid JSON: {relative}") from None
    if not isinstance(parsed, dict):
        raise GateRefused(f"form answers must be one JSON object: {relative}")
    for key, value in parsed.items():
        if not isinstance(value, _SCALARS):
            raise GateRefused(f"form answer {key!r} must be a string, number, true, false or null")
    try:
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError:
        raise GateRefused(f"form answers hold a value JSON cannot store exactly: {relative}") from None
    return parsed, canonical


def take_snapshot(base_dir: Path | str, cv_path: str, letter_path: str, answers_path: str) -> Snapshot:
    """Read the CV, the letter and the form answers once; hash exactly the bytes read.

    The three parts are labelled and length-prefixed, so swapping two files changes the hash.
    """
    base = Path(base_dir)
    cv = _resolve_inside(base, cv_path, "the CV").read_bytes()
    letter = _resolve_inside(base, letter_path, "the cover letter").read_bytes()
    answers_raw = _resolve_inside(base, answers_path, "the form answers").read_bytes()
    answers, canonical = _canonical_answers(answers_raw, answers_path)
    digest = hashlib.sha256()
    for label, data in (("cv", cv), ("letter", letter), ("answers", canonical.encode("utf-8"))):
        digest.update(label.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return Snapshot(cv, letter, MappingProxyType(dict(answers)), canonical, "sha256:" + digest.hexdigest())


def hash_documents(base_dir: Path | str, cv_path: str, letter_path: str, answers_path: str) -> str:
    """Hash of the CV, the letter and the form answers."""
    return take_snapshot(base_dir, cv_path, letter_path, answers_path).digest


def _now_iso(now: datetime) -> str:
    return now.replace(microsecond=0).isoformat()


def _flag_list(flags: str) -> list[str]:
    return [f.strip() for f in flags.split(";") if f.strip()]


def _with_flag(flags: str, flag: str) -> str:
    items = _flag_list(flags)
    if flag not in items:
        items.append(flag)
    return ";".join(items)


def _without_flag(flags: str, flag: str) -> str:
    return ";".join(f for f in _flag_list(flags) if f != flag)


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

    def _snapshot(self, row: dict[str, str]) -> Snapshot:
        return take_snapshot(self.base_dir, row["cv_path"], row["letter_path"], row["answers_path"])

    def _problems(self, row: dict[str, str]) -> tuple[list[str], Snapshot | None, bool]:
        """Return (reasons it cannot be submitted, snapshot taken now or None, documents changed)."""
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
        snapshot: Snapshot | None = None
        changed = False
        try:
            snapshot = self._snapshot(row)
        except GateRefused as err:
            reasons.extend(err.reasons)
        else:
            if stored and snapshot.digest != stored:
                changed = True
                reasons.append("the documents or form answers changed since approval")
        return reasons, snapshot, changed

    def _reset_if_changed(self, tx, row_id: str, row: dict[str, str], changed: bool) -> bool:
        # A row whose submit is in flight keeps its approval until the submit reports back.
        if UNCONFIRMED_FLAG in row.get("flags", ""):
            return False
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

    def review(self, row_id: str) -> tuple[dict[str, str], Snapshot]:
        """The row and a snapshot of what approving it now would cover. Changes nothing."""
        row = {r["id"]: r for r in self.tracker.read()}.get(row_id)
        if row is None:
            raise UnknownRow(row_id)
        return row, self._snapshot(row)

    def approve(
        self,
        row_id: str,
        approver: str,
        os_user: str | None = None,
        expected_digest: str | None = None,
    ) -> str:
        """Record the user's approval of the documents and answers as they are now. Returns the stored hash.

        ``expected_digest``, when given, is the hash the person was shown; the approval is refused
        if the files changed since.
        """
        digest = ""
        try:
            with self.tracker.edit() as tx:
                row = tx.get(row_id)
                if approver != APPROVER:
                    raise GateRefused(f"only {APPROVER!r} can approve, not {approver!r}")
                who = os_user if os_user is not None else getpass.getuser()
                if not who or not who.strip():
                    raise GateRefused("the operating-system user name is blank")
                record_blank = not (row["approved_by"] and row["approved_at"] and row["approved_doc_hash"])
                if row["status"] == "Ready":
                    pass
                elif row["status"] == "Approved" and record_blank:
                    pass  # status was set by hand; this completes the record
                else:
                    raise GateRefused(f"row is {row['status']!r}; only a Ready row can be approved")
                digest = self._snapshot(row).digest
                if expected_digest is not None and digest != expected_digest:
                    raise GateRefused("the documents or form answers changed while you were reviewing them; review again")
                if row["status"] == "Ready":
                    tx.set_status(row_id, "Approved", actor="gate")
                tx.update(
                    row_id,
                    {
                        "approved_by": APPROVER,
                        "approved_at": _now_iso(self._clock()),
                        "approved_doc_hash": digest,
                        "approved_os_user": who.strip(),
                    },
                    actor="gate",
                )
        except GateRefused as err:
            self._log("approve", row_id, digest, "refused", "; ".join(err.reasons))
            raise
        self._log("approve", row_id, digest, "ok", f"os_user={who.strip()}")
        return digest

    def reconcile(self) -> list[str]:
        """Reset every Approved row whose documents changed since approval. Returns their ids."""
        reset: list[str] = []
        with self.tracker.edit() as tx:
            for row in tx.rows:
                if row["status"] != "Approved":
                    continue
                _, snapshot, changed = self._problems(row)
                if self._reset_if_changed(tx, row["id"], row, changed):
                    reset.append(row["id"])
                    self._log("reset", row["id"], snapshot.digest if snapshot else "", "ok", "documents changed after approval")
        if not reset:
            self._log("reconcile", "", "", "ok", "nothing to reset")
        return reset

    def submit(self, row_id: str, submit_fn: Callable[[dict[str, str], Snapshot], bool]) -> None:
        """Submit one row if, and only if, every check passes.

        The submit function takes the row and the snapshot. It must upload from the snapshot and
        return True only if the application was sent. The tracker lock is not held while it runs.
        """
        # Phase 1, under the lock: check, take the snapshot, and claim the row.
        refusal: GateRefused | None = None
        snapshot: Snapshot | None = None
        claimed: dict[str, str] = {}
        digest = ""
        with self.tracker.edit() as tx:
            row = tx.get(row_id)
            reasons, snap, changed = self._problems(row)
            digest = snap.digest if snap else ""
            if reasons:
                did_reset = self._reset_if_changed(tx, row_id, row, changed)
                refusal = GateRefused(reasons)
                self._log("submit", row_id, digest, "refused", "; ".join(reasons) + ("; reset to Ready" if did_reset else ""))
            else:
                snapshot, claimed = snap, dict(row)
                tx.update(row_id, {"flags": _with_flag(row["flags"], UNCONFIRMED_FLAG)}, actor="gate")
        if refusal is not None:
            raise refusal
        assert snapshot is not None

        # The submit itself, with no lock held.
        error: BaseException | None = None
        done = False
        try:
            done = submit_fn(claimed, snapshot) is True
        except BaseException as exc:  # the site may or may not hold an application now
            error = exc
        try:
            drifted = self._snapshot(claimed).digest != snapshot.digest
        except GateRefused:
            drifted = True

        # Phase 2, under the lock: record what happened.
        failure: BaseException | None = None
        with self.tracker.edit() as tx:
            row = tx.get(row_id)
            flags = row["flags"]
            if error is not None:
                tx.update(row_id, {"flags": _with_flag(flags, DRIFT_FLAG) if drifted else flags}, actor="gate")
                self._log("submit", row_id, digest, "unconfirmed", type(error).__name__ + ("; files changed during submit" if drifted else ""))
                failure = error
            elif not done:
                tx.update(row_id, {"flags": _without_flag(flags, UNCONFIRMED_FLAG)}, actor="gate")
                self._log("submit", row_id, digest, "failed", "submit function reported failure")
                failure = SubmitFailed("the submit function reported that nothing was submitted")
            elif row["status"] != "Approved":
                self._log("submit", row_id, digest, "unconfirmed", f"submitted, but the row is now {row['status']!r}")
                failure = SubmitNotRecorded(f"submitted, but the row is now {row['status']!r}; check the site and the tracker")
            else:
                new_flags = _without_flag(flags, UNCONFIRMED_FLAG)
                if drifted:
                    new_flags = _with_flag(new_flags, DRIFT_FLAG)
                tx.set_status(row_id, "Applied", actor="gate")
                tx.update(row_id, {"date_applied": self._clock().date().isoformat(), "flags": new_flags}, actor="gate")
                self._log("submit", row_id, digest, "ok", "files changed during submit" if drifted else "")
        if failure is not None:
            raise failure


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _approve_interactively(gate: Gate, row_id: str, stdin: TextIO, stdout: TextIO) -> str:
    """The one place ``Gate.approve`` is called. A person at a terminal confirms company and role."""
    if not (_is_tty(stdin) and _is_tty(stdout)):
        gate._log("approve", row_id, "", "refused", "stdin or stdout is not an interactive terminal")
        raise GateRefused("approve needs an interactive terminal on stdin and stdout; run it yourself in a terminal window")
    row, snapshot = gate.review(row_id)
    company, role = row.get("company", "").strip(), row.get("role", "").strip()
    if not company or not role:
        gate._log("approve", row_id, snapshot.digest, "refused", "row has no company or role to confirm")
        raise GateRefused("the row has no company or role, so there is nothing to type back; fix the tracker first")
    out = stdout.write
    out(f"Approving {row_id}: {role} at {company}\n")
    out(f"  CV:      {row['cv_path']}\n  Letter:  {row['letter_path']}\n  Answers: {row['answers_path']}\n")
    for key in sorted(snapshot.answers):
        out(f"    {key}: {snapshot.answers[key]}\n")
    out(f"  Hash:    {snapshot.digest}\n")
    out("Read the documents and answers first. Approving lets the agent submit exactly these.\n")
    typed = []
    for prompt in ("Type the company name to confirm: ", "Type the role title to confirm: "):
        out(prompt)
        stdout.flush()
        typed.append(stdin.readline().rstrip("\r\n").strip())
    if typed != [company, role]:
        gate._log("approve", row_id, snapshot.digest, "refused", "typed company or role did not match")
        raise GateRefused("the company or role you typed does not match the tracker; nothing was approved")
    try:
        os_user = getpass.getuser()
    except Exception:
        raise GateRefused("cannot find the operating-system user name") from None
    return gate.approve(row_id, approver=APPROVER, os_user=os_user, expected_digest=snapshot.digest)


def main(argv: list[str] | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approval gate for application rows")
    parser.add_argument("--tracker", type=Path, default=Path("tracker.csv"))
    parser.add_argument("--base-dir", type=Path, default=Path("."))
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("approve", help="record the user's approval of a Ready row (interactive terminal only)").add_argument("row_id")
    sub.add_parser("check", help="say whether a row could be submitted now").add_argument("row_id")
    sub.add_parser("reconcile", help="reset Approved rows whose documents changed")
    args = parser.parse_args(argv)
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    gate = Gate(Tracker(args.tracker), args.base_dir, log_dir=args.log_dir)
    try:
        if args.command == "approve":
            print(f"approved {args.row_id}: {_approve_interactively(gate, args.row_id, stdin, stdout)}", file=stdout)
        elif args.command == "check":
            reasons = gate.check(args.row_id)
            print("can be submitted" if not reasons else "cannot be submitted:\n- " + "\n- ".join(reasons), file=stdout)
            return 0 if not reasons else 1
        else:
            reset = gate.reconcile()
            print("reset to Ready: " + (", ".join(reset) if reset else "none"), file=stdout)
    except GateRefused as err:
        print("refused: " + "; ".join(err.reasons), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
