"""Step 4, headline test: nothing is submitted unless every condition holds.

The condition, written out independently of the gate's own code:

    status == "Approved"
    AND approved_by == "user"
    AND approved_at holds a timestamp
    AND the CV, letter and form answers on disk are the ones that were approved
    AND the row is not flagged submit_unconfirmed

Two property tests check it.

* ``test_submit_happens_exactly_when_the_conditions_hold`` generates a row in
  an arbitrary state (any status spelling, any approver, any timestamp, a
  correct, blank, wrong or re-cased hash, any kind of document change, any
  flag) and checks that the submit function runs if and only if the
  conditions hold.
* ``GateMachine`` is a state machine. It performs random sequences of calls
  (advance a row, edit or delete a document, approve as various people, set
  the status by hand, tamper with the approval cells in the file, reconcile,
  submit) and checks after every step that every call to the submit function
  was legitimate, that no row is submitted twice, and that a row only reads
  Applied if the gate submitted it.
"""

import csv
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from job_agent.gate import Gate, GateRefused, SubmitFailed, hash_documents
from job_agent.tracker import STATUSES, Tracker, TrackerError

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime.fromisoformat("2026-11-02T09:30:00+00:00")
GOOD_TIMES = {"2026-11-02T09:30:00+00:00", "2026-11-02 09:30:00"}
ROWS = ["S01", "S02", "S03"]
CV, LETTER, ANSWERS = "cv.txt", "letter.txt", "answers.json"

SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"),
    max_size=8,
)


class Recorder:
    def __init__(self, base: Path, rows_dir):
        self.base, self.rows_dir = base, rows_dir
        self.calls = []

    def __call__(self, row, snapshot):
        # What the submit step is given is the gate's verified copy, not whatever is on disk now.
        self.calls.append({"row": dict(row), "cv": snapshot.cv, "letter": snapshot.letter, "answers": dict(snapshot.answers)})
        return True


def make_world(tmp: Path, row_ids):
    """A tracker copied from the sample, with two documents on disk for each row id."""
    csv_path = tmp / "tracker.csv"
    shutil.copy(FIXTURES / "tracker.sample.csv", csv_path)
    base = tmp / "work"
    tracker = Tracker(csv_path)
    with tracker.edit() as tx:
        for row_id in row_ids:
            folder = base / row_id
            folder.mkdir(parents=True)
            (folder / CV).write_bytes(b"cv " + row_id.encode())
            (folder / LETTER).write_bytes(b"letter " + row_id.encode())
            (folder / ANSWERS).write_bytes(b'{"salary": 30000, "right_to_work": "' + row_id.encode() + b'"}')
            tx.update(
                row_id,
                {"cv_path": f"{row_id}/{CV}", "letter_path": f"{row_id}/{LETTER}", "answers_path": f"{row_id}/{ANSWERS}"},
                actor="agent",
            )
    return tracker, base


def edit_cells(tracker: Tracker, row_id: str, **cells):
    """Change cells in the CSV file directly, bypassing the tracker's own rules."""
    with open(tracker.path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        names = list(reader.fieldnames)
        rows = list(reader)
    for row in rows:
        if row["id"] == row_id:
            row.update(cells)
    with open(tracker.path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)


# --- property 1: arbitrary single states ------------------------------------------

STATUS_SPELLINGS = list(STATUSES) + ["", "approved", "APPROVED", " Approved", "Approved "]
# Each field is correct two times in three, so that fully valid rows and rows with exactly one
# fault are both common. Uniform draws would almost never produce a row that is valid but for one
# field, which is the case that matters.
STATUS = st.one_of(st.just("Approved"), st.just("Approved"), st.sampled_from(STATUS_SPELLINGS))
APPROVERS = st.one_of(
    st.just("user"),
    st.just("user"),
    st.sampled_from(["User", "USER", " user", "user ", "agent", "", "alex", "gate", "user\u200b"]),
    SAFE_TEXT,
)
TIMES = st.one_of(
    st.just("2026-11-02T09:30:00+00:00"),
    st.just("2026-11-02 09:30:00"),
    st.sampled_from(["", " ", "soon", "2026-11-02", "2026-13-45T99:99:99"]),
)
HASH_MODES = st.one_of(st.just("correct"), st.just("correct"), st.sampled_from(["blank", "wrong", "recased"]))
DOC_CHANGES = st.one_of(
    st.just("none"),
    st.just("same_bytes"),
    st.sampled_from(["cv", "letter", "both", "cv_deleted", "letter_deleted", "answers", "answers_deleted", "answers_reformatted"]),
)
FLAGS = st.one_of(st.just(""), st.just(""), st.sampled_from(["check_visa", "blocked", "submit_unconfirmed", "check_visa;submit_unconfirmed"]))


@given(
    status=STATUS,
    approved_by=APPROVERS,
    approved_at=TIMES,
    hash_mode=HASH_MODES,
    doc_change=DOC_CHANGES,
    flags=FLAGS,
)
@settings(max_examples=400)
def test_submit_happens_exactly_when_the_conditions_hold(status, approved_by, approved_at, hash_mode, doc_change, flags):
    tmp = Path(tempfile.mkdtemp())
    try:
        tracker, base = make_world(tmp, ["S01"])
        good = hash_documents(base, "S01/" + CV, "S01/" + LETTER, "S01/" + ANSWERS)
        stored = {"correct": good, "blank": "", "wrong": "sha256:" + "0" * 64, "recased": good.upper()}[hash_mode]
        edit_cells(
            tracker,
            "S01",
            status=status,
            approved_by=approved_by,
            approved_at=approved_at,
            approved_doc_hash=stored,
            flags=flags,
        )
        cv, letter = base / "S01" / CV, base / "S01" / LETTER
        if doc_change == "same_bytes":
            cv.write_bytes(cv.read_bytes())
        if doc_change in ("cv", "both"):
            cv.write_bytes(cv.read_bytes() + b"!")
        if doc_change in ("letter", "both"):
            letter.write_bytes(letter.read_bytes() + b"!")
        if doc_change == "cv_deleted":
            cv.unlink()
        if doc_change == "letter_deleted":
            letter.unlink()
        answers = base / "S01" / ANSWERS
        if doc_change == "answers":
            answers.write_bytes(b'{"salary": 31000, "right_to_work": "S01"}')
        if doc_change == "answers_deleted":
            answers.unlink()
        if doc_change == "answers_reformatted":  # same answers, different layout and order
            answers.write_bytes(b'{\n "right_to_work": "S01",\n "salary": 30000\n}')

        expected = (
            status == "Approved"
            and approved_by == "user"
            and approved_at in GOOD_TIMES
            and hash_mode == "correct"
            and doc_change in ("none", "same_bytes", "answers_reformatted")
            and "submit_unconfirmed" not in flags
        )

        recorder = Recorder(base, lambda row_id: row_id)
        gate = Gate(tracker, base, clock=lambda: NOW)
        try:
            gate.submit("S01", recorder)
        except (GateRefused, SubmitFailed):
            pass

        final = tracker.read()[0]
        assert (len(recorder.calls) == 1) == expected
        assert len(recorder.calls) <= 1
        if expected:
            assert final["status"] == "Applied"
        elif status != "Applied":
            assert final["status"] != "Applied"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- property 2: random sequences of calls ----------------------------------------


class GateMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.tmp = Path(tempfile.mkdtemp())
        self.tracker, self.base = make_world(self.tmp, ROWS)
        self.gate = Gate(self.tracker, self.base, clock=lambda: NOW)
        self.recorder = Recorder(self.base, lambda row_id: row_id)
        self.approved = {}  # row id -> (cv, letter, answers, hash in the file) at the last successful approve
        self.submitted = []

    def teardown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _doc(self, row_id, name):
        return self.base / row_id / name

    # -- ordinary work --
    @rule(row=st.sampled_from(ROWS))
    def agent_advances_the_row(self, row):
        current = {r["id"]: r["status"] for r in self.tracker.read()}[row]
        if current in ("Found", "Tailored"):
            nxt = STATUSES[STATUSES.index(current) + 1]
            with self.tracker.edit() as tx:
                tx.set_status(row, nxt, actor="agent")

    @rule(row=st.sampled_from(ROWS), who=st.sampled_from(["user", "user", "user", "agent"]))
    def prepare_and_approve(self, row, who):
        """The usual path in one step: the agent readies the row, then someone approves it."""
        current = {r["id"]: r["status"] for r in self.tracker.read()}[row]
        if current in STATUSES:
            with self.tracker.edit() as tx:
                for status in ("Tailored", "Ready"):
                    if STATUSES.index(current) < STATUSES.index(status):
                        tx.set_status(row, status, actor="agent")
                        current = status
        self.someone_approves(row, who)

    @rule(row=st.sampled_from(ROWS), name=st.sampled_from([CV, LETTER]), content=st.sampled_from([b"a", b"b", b"cv S01"]))
    def a_document_is_written(self, row, name, content):
        self._doc(row, name).write_bytes(content)

    @rule(
        row=st.sampled_from(ROWS),
        content=st.sampled_from(
            [
                b'{"salary": 30000, "right_to_work": "S01"}',
                b'{"salary": 31000, "right_to_work": "S01"}',
                b'{"right_to_work": "S02", "salary": 30000}',
                b'{"salary": 30000, "right_to_work": "S03"}',
                b"{}",
                b"not json",
            ]
        ),
    )
    def the_form_answers_are_written(self, row, content):
        self._doc(row, ANSWERS).write_bytes(content)

    @rule(row=st.sampled_from(ROWS), name=st.sampled_from([CV, LETTER, ANSWERS]))
    def a_document_is_deleted(self, row, name):
        self._doc(row, name).unlink(missing_ok=True)

    # -- approval, by the right and the wrong people --
    @rule(row=st.sampled_from(ROWS), who=st.sampled_from(["user", "user", "agent", "User", "", "gate"]))
    def someone_approves(self, row, who):
        try:
            self.gate.approve(row, approver=who)
        except (GateRefused, TrackerError):
            return
        assert who == "user"
        record = {r["id"]: r for r in self.tracker.read()}[row]
        cv, letter = self._doc(row, CV).read_bytes(), self._doc(row, LETTER).read_bytes()
        answers = json.loads(self._doc(row, ANSWERS).read_bytes())
        self.approved[row] = (cv, letter, answers, record["approved_doc_hash"])

    @rule(row=st.sampled_from(ROWS))
    def the_user_sets_the_status_by_hand(self, row):
        try:
            with self.tracker.edit() as tx:
                tx.set_status(row, "Approved", actor="user")
        except TrackerError:
            pass

    @rule(
        row=st.sampled_from(ROWS),
        column=st.sampled_from(["approved_by", "approved_at", "approved_doc_hash", "flags", "status"]),
        value=st.sampled_from(["", "user", "agent", "soon", "2026-11-02T09:30:00+00:00", "sha256:0", "RESTORE", "submit_unconfirmed", "Approved", "Ready", "Found"]),
    )
    def the_file_is_edited_directly(self, row, column, value):
        if value == "RESTORE":
            if column != "approved_doc_hash" or row not in self.approved:
                return
            value = self.approved[row][3]
        edit_cells(self.tracker, row, **{column: value})

    @rule()
    def reconcile_runs(self):
        self.gate.reconcile()

    # -- the call that matters --
    @rule(row=st.sampled_from(ROWS))
    def a_submit_is_attempted(self, row):
        before = len(self.recorder.calls)
        try:
            self.gate.submit(row, self.recorder)
        except (GateRefused, SubmitFailed):
            assert len(self.recorder.calls) == before
            return
        assert len(self.recorder.calls) == before + 1
        call = self.recorder.calls[-1]
        assert row not in self.submitted, "a row was submitted twice"
        self.submitted.append(row)
        seen = call["row"]
        assert seen["id"] == row
        assert seen["status"] == "Approved"
        assert seen["approved_by"] == "user"
        assert seen["approved_at"] in GOOD_TIMES
        assert row in self.approved, "submitted a row that was never approved through the gate"
        cv, letter, answers, stored_hash = self.approved[row]
        assert seen["approved_doc_hash"] == stored_hash
        assert (call["cv"], call["letter"]) == (cv, letter), "documents differ from the approved version"
        assert call["answers"] == answers, "form answers differ from the approved version"

    # -- checked after every step --
    @invariant()
    def only_the_gate_applies_rows(self):
        statuses = {r["id"]: r["status"] for r in self.tracker.read()}
        for row_id, status in statuses.items():
            if status == "Applied":
                assert row_id in self.submitted, f"{row_id} reads Applied but was never submitted by the gate"

    @invariant()
    def submit_calls_match_applied_rows(self):
        assert len(self.recorder.calls) == len(self.submitted)


GateMachine.TestCase.settings = settings(max_examples=200, stateful_step_count=40)
TestGateMachine = GateMachine.TestCase
