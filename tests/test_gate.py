"""Step 4: the approval gate (hard rule 1)."""

import ast
import csv
import getpass
import inspect
import json
import os
import pty
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from job_agent import gate as gate_module
from job_agent.gate import (
    DRIFT_FLAG,
    Gate,
    GateRefused,
    SubmitFailed,
    SubmitNotRecorded,
    hash_documents,
    take_snapshot,
    valid_timestamp,
)
from job_agent.tracker import Tracker, UnknownRow

SRC = Path(__file__).resolve().parents[1] / "src"
ROW = "S01"
CV_REL = "cv/tailored/S01/cv.txt"
LETTER_REL = "cv/tailored/S01/letter.txt"
ANSWERS_REL = "cv/tailored/S01/answers.json"
ANSWERS = {"right_to_work": "Needs sponsorship", "salary_expectation": 32000, "portfolio_link": "https://example.invalid/sample"}
FIXED_NOW = datetime.fromisoformat("2026-11-02T09:30:00+00:00")


class Recorder:
    """Stands in for the real submit step: records calls, never touches a site."""

    def __init__(self, result=True, raises=None):
        self.calls = []
        self.snapshots = []
        self.result = result
        self.raises = raises

    def __call__(self, row, snapshot):
        self.calls.append(dict(row))
        self.snapshots.append(snapshot)
        if self.raises:
            raise self.raises
        return self.result


def write_answers(base, answers):
    (base / ANSWERS_REL).write_text(json.dumps(answers, indent=2), encoding="utf-8")


@pytest.fixture
def env(tmp_path, sample_tracker_csv):
    base = tmp_path / "work"
    (base / "cv/tailored/S01").mkdir(parents=True)
    (base / CV_REL).write_text("Sample CV text for the invented applicant.\n", encoding="utf-8")
    (base / LETTER_REL).write_text("Sample cover letter text.\n", encoding="utf-8")
    write_answers(base, ANSWERS)
    tracker = Tracker(sample_tracker_csv)
    with tracker.edit() as tx:
        tx.update(ROW, {"cv_path": CV_REL, "letter_path": LETTER_REL, "answers_path": ANSWERS_REL}, actor="agent")
        tx.set_status(ROW, "Tailored", actor="agent")
        tx.set_status(ROW, "Ready", actor="agent")
    gate = Gate(tracker, base, log_dir=tmp_path / "logs", clock=lambda: FIXED_NOW)
    return gate, tracker, base


def row_of(tracker, row_id=ROW):
    return {r["id"]: r for r in tracker.read()}[row_id]


def approved(env):
    gate, tracker, base = env
    gate.approve(ROW, approver="user")
    return gate, tracker, base


def set_cells(tracker, **cells):
    """Edit the CSV directly, the way a person or another program could."""
    path = tracker.path
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        names = list(rows[0])
    for r in rows:
        if r["id"] == ROW:
            r.update(cells)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)


# --- hashing ------------------------------------------------------------------


def test_hash_changes_when_either_document_changes(env):
    _, _, base = env
    before = hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL)
    assert before.startswith("sha256:")
    (base / LETTER_REL).write_text("Sample cover letter text, edited.\n", encoding="utf-8")
    assert hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL) != before


def test_hash_tells_the_two_documents_apart(env):
    _, _, base = env
    before = hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL)
    cv, letter = (base / CV_REL).read_bytes(), (base / LETTER_REL).read_bytes()
    (base / CV_REL).write_bytes(letter)
    (base / LETTER_REL).write_bytes(cv)
    assert hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL) != before


def test_hash_ignores_file_times_and_names(env):
    _, _, base = env
    before = hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL)
    os.utime(base / CV_REL, (1, 1))
    assert hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL) == before


@pytest.mark.parametrize("bad", ["../outside.txt", "/etc/hosts", ""])
def test_documents_must_sit_inside_the_base_directory(env, bad):
    _, _, base = env
    with pytest.raises(GateRefused):
        hash_documents(base, bad, LETTER_REL, ANSWERS_REL)


def test_a_link_pointing_outside_the_base_directory_is_refused(env, tmp_path):
    _, _, base = env
    outside = tmp_path / "secret.txt"
    outside.write_text("not a document", encoding="utf-8")
    (base / "cv/tailored/S01/link.txt").symlink_to(outside)
    with pytest.raises(GateRefused):
        hash_documents(base, "cv/tailored/S01/link.txt", LETTER_REL, ANSWERS_REL)


def test_timestamps(env):
    assert valid_timestamp("2026-11-02T09:30:00+00:00")
    assert valid_timestamp("2026-11-02T09:30:00Z")
    assert valid_timestamp("2026-11-02 09:30:00")
    for bad in ("", "   ", "yesterday", "2026-11-02", "2026-13-02T09:30:00", None):
        assert not valid_timestamp(bad)


# --- approving ----------------------------------------------------------------


def test_approve_records_user_time_and_hash(env):
    gate, tracker, base = env
    gate.approve(ROW, approver="user")
    row = row_of(tracker)
    assert row["status"] == "Approved"
    assert row["approved_by"] == "user"
    assert row["approved_at"] == "2026-11-02T09:30:00+00:00"
    assert row["approved_doc_hash"] == hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL)


@pytest.mark.parametrize("who", ["agent", "User", "USER", " user", "", "alex", "gate"])
def test_only_the_user_can_approve(env, who):
    gate, tracker, _ = env
    with pytest.raises(GateRefused):
        gate.approve(ROW, approver=who)
    assert row_of(tracker)["status"] == "Ready"
    assert row_of(tracker)["approved_by"] == ""


def test_only_a_ready_row_can_be_approved(env):
    gate, tracker, _ = env
    with pytest.raises(GateRefused):
        gate.approve("S02", approver="user")  # S02 is Found, with no documents either
    assert row_of(tracker, "S02")["status"] == "Found"


def test_approve_needs_documents_on_disk(env):
    gate, tracker, base = env
    (base / LETTER_REL).unlink()
    with pytest.raises(GateRefused):
        gate.approve(ROW, approver="user")
    assert row_of(tracker)["status"] == "Ready"


def test_approve_needs_document_paths_on_the_row(env):
    gate, tracker, _ = env
    with tracker.edit() as tx:
        tx.update(ROW, {"letter_path": ""}, actor="agent")
    with pytest.raises(GateRefused):
        gate.approve(ROW, approver="user")


def test_a_fully_approved_row_cannot_be_approved_again(env):
    gate, _, _ = approved(env)
    with pytest.raises(GateRefused):
        gate.approve(ROW, approver="user")


def test_a_hand_set_approved_status_can_be_completed_by_the_user(env):
    gate, tracker, base = env
    with tracker.edit() as tx:
        tx.set_status(ROW, "Approved", actor="user")  # status set in the sheet, no record yet
    with pytest.raises(GateRefused):
        gate.submit(ROW, Recorder())  # not submittable yet
    gate.approve(ROW, approver="user")
    row = row_of(tracker)
    assert row["approved_by"] == "user" and row["approved_doc_hash"] == hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL)
    recorder = Recorder()
    gate.submit(ROW, recorder)
    assert len(recorder.calls) == 1


# --- submitting ---------------------------------------------------------------


def test_approved_row_submits_once_and_becomes_applied(env):
    gate, tracker, _ = approved(env)
    recorder = Recorder()
    gate.submit(ROW, recorder)
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["id"] == ROW
    row = row_of(tracker)
    assert row["status"] == "Applied"
    assert row["date_applied"] == "2026-11-02"
    assert row["approved_by"] == "user" and row["approved_doc_hash"]  # the record is kept


def test_a_row_cannot_be_submitted_twice(env):
    gate, _, _ = approved(env)
    recorder = Recorder()
    gate.submit(ROW, recorder)
    with pytest.raises(GateRefused):
        gate.submit(ROW, recorder)
    assert len(recorder.calls) == 1


def test_unknown_row(env):
    gate, _, _ = env
    with pytest.raises(UnknownRow):
        gate.submit("NOPE", Recorder())


@pytest.mark.parametrize(
    "cells",
    [
        {"status": "Ready"},
        {"status": "Tailored"},
        {"status": "approved"},
        {"approved_by": ""},
        {"approved_by": "agent"},
        {"approved_by": "User"},
        {"approved_by": "user "},
        {"approved_at": ""},
        {"approved_at": "soon"},
        {"approved_at": "2026-11-02"},
        {"approved_doc_hash": ""},
        {"approved_doc_hash": "sha256:" + "0" * 64},
        {"cv_path": ""},
        {"cv_path": "../elsewhere.txt"},
        {"answers_path": ""},
        {"answers_path": "../elsewhere.json"},
        {"flags": "submit_unconfirmed"},
    ],
    ids=lambda c: "-".join(f"{k}={v}" for k, v in c.items()),
)
def test_every_missing_or_wrong_condition_blocks_the_submit(env, cells):
    gate, tracker, _ = approved(env)
    set_cells(tracker, **cells)
    recorder = Recorder()
    with pytest.raises(GateRefused):
        gate.submit(ROW, recorder)
    assert recorder.calls == []
    assert row_of(tracker)["status"] != "Applied"


@pytest.mark.parametrize("which", ["cv", "letter"])
def test_editing_a_document_after_approval_blocks_and_resets(env, which):
    gate, tracker, base = approved(env)
    target = base / (CV_REL if which == "cv" else LETTER_REL)
    target.write_text(target.read_text() + "One more line.\n", encoding="utf-8")
    recorder = Recorder()
    with pytest.raises(GateRefused) as err:
        gate.submit(ROW, recorder)
    assert "changed" in str(err.value)
    assert recorder.calls == []
    row = row_of(tracker)
    assert row["status"] == "Ready"
    assert row["approved_by"] == row["approved_at"] == row["approved_doc_hash"] == ""


def test_a_deleted_document_blocks_the_submit(env):
    gate, tracker, base = approved(env)
    (base / CV_REL).unlink()
    recorder = Recorder()
    with pytest.raises(GateRefused):
        gate.submit(ROW, recorder)
    assert recorder.calls == []


def test_reapproval_is_needed_after_an_edit(env):
    gate, tracker, base = approved(env)
    (base / LETTER_REL).write_text("Rewritten by the user.\n", encoding="utf-8")
    with pytest.raises(GateRefused):
        gate.submit(ROW, Recorder())
    recorder = Recorder()
    with pytest.raises(GateRefused):
        gate.submit(ROW, recorder)  # Ready again, not Approved
    gate.approve(ROW, approver="user")  # the user reads the new version and approves it
    gate.submit(ROW, recorder)
    assert len(recorder.calls) == 1


def test_rewriting_a_document_with_the_same_content_keeps_the_approval(env):
    gate, tracker, base = approved(env)
    (base / CV_REL).write_bytes((base / CV_REL).read_bytes())
    recorder = Recorder()
    gate.submit(ROW, recorder)
    assert len(recorder.calls) == 1


def test_reconcile_resets_rows_whose_documents_changed(env):
    gate, tracker, base = approved(env)
    (base / CV_REL).write_text("edited\n", encoding="utf-8")
    assert gate.reconcile() == [ROW]
    assert row_of(tracker)["status"] == "Ready"
    assert gate.reconcile() == []


def test_reconcile_leaves_unchanged_and_hand_set_rows_alone(env):
    gate, tracker, _ = approved(env)
    assert gate.reconcile() == []
    assert row_of(tracker)["status"] == "Approved"
    with tracker.edit() as tx:
        tx.set_status("S02", "Rejected", actor="agent")
    assert gate.reconcile() == []


def test_a_failed_submit_leaves_the_row_approved(env):
    gate, tracker, _ = approved(env)
    with pytest.raises(SubmitFailed):
        gate.submit(ROW, Recorder(result=False))
    assert row_of(tracker)["status"] == "Approved"
    gate.submit(ROW, Recorder())  # a clean failure may be retried
    assert row_of(tracker)["status"] == "Applied"


def test_an_unconfirmed_submit_is_flagged_and_not_retried_blindly(env):
    gate, tracker, _ = approved(env)
    with pytest.raises(RuntimeError):
        gate.submit(ROW, Recorder(raises=RuntimeError("connection dropped")))
    row = row_of(tracker)
    assert row["status"] == "Approved"
    assert "submit_unconfirmed" in row["flags"]
    retry = Recorder()
    with pytest.raises(GateRefused):
        gate.submit(ROW, retry)
    assert retry.calls == []


# --- no way round the gate ------------------------------------------------------


def test_there_is_no_force_option():
    for func in (Gate.submit, Gate.approve, Gate.reconcile, gate_module.main):
        assert not [p for p in inspect.signature(func).parameters if "force" in p.lower()]
    sources = [p.read_text() for p in (SRC / "job_agent").glob("*.py")]
    assert not [s for s in sources if re.search(r"--force|force\s*=", s)]


def test_the_cli_offers_no_force_flag():
    run = subprocess.run(
        [sys.executable, "-m", "job_agent.gate", "--help"],
        env={"PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0
    assert "force" not in run.stdout.lower()


def test_the_submit_step_has_one_call_site_and_lives_only_in_the_gate():
    gate_source = (SRC / "job_agent" / "gate.py").read_text()
    assert len(re.findall(r"\bsubmit_fn\(", gate_source)) == 1
    for path in (SRC / "job_agent").glob("*.py"):
        if path.name != "gate.py":
            assert "submit_fn" not in path.read_text(), path.name


def test_the_tracker_alone_cannot_mark_a_row_applied_without_the_gate(env):
    gate, tracker, _ = approved(env)
    from job_agent.tracker import NotPermitted

    with pytest.raises(NotPermitted):
        with tracker.edit() as tx:
            tx.set_status(ROW, "Applied", actor="agent")


# --- the action log -------------------------------------------------------------


def read_log(tmp_path):
    lines = []
    for path in sorted((tmp_path / "logs").glob("*.jsonl")):
        lines += [json.loads(line) for line in path.read_text().splitlines()]
    return lines


def test_every_gate_action_is_logged(env, tmp_path):
    gate, tracker, base = env
    with pytest.raises(GateRefused):
        gate.approve(ROW, approver="agent")
    gate.approve(ROW, approver="user")
    (base / CV_REL).write_text("edited\n", encoding="utf-8")
    with pytest.raises(GateRefused):
        gate.submit(ROW, Recorder())
    events = read_log(tmp_path)
    assert [(e["action"], e["outcome"]) for e in events] == [
        ("approve", "refused"),
        ("approve", "ok"),
        ("submit", "refused"),
    ]
    for event in events:
        assert set(event) >= {"timestamp", "action", "source", "role_id", "inputs_hash", "outcome"}
        assert event["role_id"] == ROW
    assert (tmp_path / "logs" / "2026-11-02.jsonl").exists()


def test_the_log_never_holds_document_text_or_credentials(env, tmp_path):
    gate, _, _ = approved(env)
    gate.submit(ROW, Recorder())
    text = "".join(p.read_text() for p in (tmp_path / "logs").glob("*.jsonl"))
    assert "Sample CV text" not in text and "Sample cover letter" not in text
    assert not re.search(r"password|cookie|token|secret", text, re.I)


def test_the_log_is_append_only(env, tmp_path):
    gate, _, _ = env
    gate.approve(ROW, approver="user")
    first = (tmp_path / "logs" / "2026-11-02.jsonl").read_text()
    gate.reconcile()
    second = (tmp_path / "logs" / "2026-11-02.jsonl").read_text()
    assert second.startswith(first)


# --- command line ---------------------------------------------------------------


def run_cli(env, *args):
    _, tracker, base = env
    return subprocess.run(
        [sys.executable, "-m", "job_agent.gate", "--tracker", str(tracker.path), "--base-dir", str(base), "--log-dir", str(base.parent / "cli-logs"), *args],
        env={"PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
    )


COMPANY, ROLE = "G-Research", "Graduate Machine Learning Engineer"


class FakeTerminal:
    """A stream that says whether it is a terminal, with scripted input lines."""

    def __init__(self, tty=True, lines=(), on_read=None):
        self.tty = tty
        self.lines = list(lines)
        self.on_read = on_read
        self.out = []

    def isatty(self):
        return self.tty

    def readline(self):
        if self.on_read:
            self.on_read()
            self.on_read = None
        return (self.lines.pop(0) + "\n") if self.lines else ""

    def write(self, text):
        self.out.append(text)

    def flush(self):
        pass

    @property
    def text(self):
        return "".join(self.out)


def cli_args(env, *args):
    _, tracker, base = env
    return ["--tracker", str(tracker.path), "--base-dir", str(base), "--log-dir", str(base.parent / "cli-logs"), *args]


def run_cli_in_process(env, command, stdin, stdout=None):
    stdout = stdout or FakeTerminal()
    code = gate_module.main(cli_args(env, command, ROW), stdin=stdin, stdout=stdout)
    return code, stdout


def run_cli_on_a_pty(env, typed):
    """Run the real command with a pseudo-terminal as stdin, stdout and stderr, and type into it."""
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "job_agent.gate", *cli_args(env, "approve", ROW)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={"PYTHONPATH": str(SRC)},
            close_fds=True,
        )
        os.close(slave)
        slave = None
        os.write(master, typed.encode())
        code = proc.wait(timeout=30)
        chunks = b""
        try:
            while True:
                data = os.read(master, 4096)
                if not data:
                    break
                chunks += data
        except OSError:
            pass
        return code, chunks.decode(errors="replace")
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)


def test_cli_check_and_reconcile(env):
    gate, tracker, base = env
    assert run_cli(env, "check", ROW).returncode == 1  # Ready: not submittable
    gate.approve(ROW, approver="user")
    assert run_cli(env, "check", ROW).returncode == 0
    (base / CV_REL).write_text("edited\n", encoding="utf-8")
    out = run_cli(env, "reconcile")
    assert out.returncode == 0 and ROW in out.stdout
    assert row_of(tracker)["status"] == "Ready"


def test_cli_has_no_submit_command(env):
    assert run_cli(env, "submit", ROW).returncode != 0


# --- the snapshot: what was checked is what is submitted ------------------------


def test_the_submit_function_gets_the_bytes_that_were_hashed(env):
    gate, tracker, base = approved(env)
    recorder = Recorder()
    gate.submit(ROW, recorder)
    snap = recorder.snapshots[0]
    assert snap.digest == row_of(tracker)["approved_doc_hash"]
    assert snap.cv == (base / CV_REL).read_bytes()
    assert snap.letter == (base / LETTER_REL).read_bytes()
    assert dict(snap.answers) == ANSWERS


def test_a_file_changed_after_the_check_does_not_change_what_is_submitted(env):
    gate, tracker, base = approved(env)
    original_cv = (base / CV_REL).read_bytes()
    seen = {}

    def submit(row, snapshot):
        (base / CV_REL).write_text("Swapped in after the check.\n", encoding="utf-8")  # the race
        seen["cv"] = snapshot.cv
        return True

    gate.submit(ROW, submit)
    assert seen["cv"] == original_cv  # the upload source is the verified copy, not the disk
    row = row_of(tracker)
    assert row["status"] == "Applied"
    assert DRIFT_FLAG in row["flags"]  # and the record shows the disk moved on


def test_a_submit_that_read_the_files_again_is_caught_by_the_second_hash(env):
    gate, tracker, base = approved(env)

    def submit(row, snapshot):
        (base / ANSWERS_REL).write_text(json.dumps({**ANSWERS, "salary_expectation": 99999}), encoding="utf-8")
        return True

    gate.submit(ROW, submit)
    assert DRIFT_FLAG in row_of(tracker)["flags"]


def test_drift_is_flagged_when_the_submit_raises_too(env):
    gate, tracker, base = approved(env)

    def submit(row, snapshot):
        (base / LETTER_REL).write_text("changed\n", encoding="utf-8")
        raise RuntimeError("connection dropped")

    with pytest.raises(RuntimeError):
        gate.submit(ROW, submit)
    flags = row_of(tracker)["flags"]
    assert "submit_unconfirmed" in flags and DRIFT_FLAG in flags


def test_no_drift_flag_when_nothing_changed(env):
    gate, tracker, _ = approved(env)
    gate.submit(ROW, Recorder())
    assert row_of(tracker)["flags"] == ""


def test_the_snapshot_cannot_be_edited_by_the_submit_function(env):
    gate, _, _ = approved(env)
    recorder = Recorder()
    gate.submit(ROW, recorder)
    snap = recorder.snapshots[0]
    with pytest.raises(TypeError):
        snap.answers["salary_expectation"] = 1
    with pytest.raises(AttributeError):
        snap.cv = b"other"


def test_the_gate_reads_each_file_once_per_check(env, monkeypatch):
    gate, _, base = approved(env)
    reads = []
    original = Path.read_bytes

    def counting(self):
        reads.append(self.name)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    gate.submit(ROW, Recorder())
    # one check before the submit and one hash after it; never a read in between by the gate itself
    assert sorted(reads) == sorted(["cv.txt", "letter.txt", "answers.json"] * 2)


# --- form answers are part of the approval ---------------------------------------


def test_hash_changes_when_a_form_answer_changes(env):
    _, _, base = env
    before = hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL)
    write_answers(base, {**ANSWERS, "salary_expectation": 33000})
    assert hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL) != before


def test_hash_ignores_answer_layout_and_key_order(env):
    _, _, base = env
    before = hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL)
    reordered = dict(reversed(list(ANSWERS.items())))
    (base / ANSWERS_REL).write_text(json.dumps(reordered, indent=7), encoding="utf-8")
    assert hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL) == before


@pytest.mark.parametrize(
    "change",
    [
        {"right_to_work": "Has full right to work"},
        {"salary_expectation": 45000},
        {"portfolio_link": "https://example.invalid/other"},
        {"extra_question": "yes"},
        {"salary_expectation": None},
        {"salary_expectation": "32000"},
    ],
    ids=lambda c: next(iter(c)) + "=" + str(next(iter(c.values()))),
)
def test_changing_a_form_answer_after_approval_blocks_and_resets(env, change):
    gate, tracker, base = approved(env)
    write_answers(base, {**ANSWERS, **change})
    recorder = Recorder()
    with pytest.raises(GateRefused) as err:
        gate.submit(ROW, recorder)
    assert "changed" in str(err.value)
    assert recorder.calls == []
    row = row_of(tracker)
    assert row["status"] == "Ready"
    assert row["approved_by"] == row["approved_at"] == row["approved_doc_hash"] == row["approved_os_user"] == ""


def test_removing_a_form_answer_after_approval_blocks(env):
    gate, tracker, base = approved(env)
    write_answers(base, {k: v for k, v in ANSWERS.items() if k != "right_to_work"})
    recorder = Recorder()
    with pytest.raises(GateRefused):
        gate.submit(ROW, recorder)
    assert recorder.calls == [] and row_of(tracker)["status"] == "Ready"


def test_deleting_the_answers_file_after_approval_blocks(env):
    gate, tracker, base = approved(env)
    (base / ANSWERS_REL).unlink()
    recorder = Recorder()
    with pytest.raises(GateRefused):
        gate.submit(ROW, recorder)
    assert recorder.calls == []


def test_reconcile_resets_a_row_whose_answers_changed(env):
    gate, tracker, base = approved(env)
    write_answers(base, {**ANSWERS, "salary_expectation": 1})
    assert gate.reconcile() == [ROW]
    assert row_of(tracker)["status"] == "Ready"


def test_answers_must_be_approved_before_a_submit(env):
    gate, tracker, base = env
    write_answers(base, {**ANSWERS, "salary_expectation": 40000})  # edited before approval: fine
    gate.approve(ROW, approver="user")
    write_answers(base, ANSWERS)  # edited back after approval: not the approved version
    with pytest.raises(GateRefused):
        gate.submit(ROW, Recorder())


def test_approve_needs_an_answers_path_and_file(env):
    gate, tracker, base = env
    with tracker.edit() as tx:
        tx.update(ROW, {"answers_path": ""}, actor="agent")
    with pytest.raises(GateRefused):
        gate.approve(ROW, approver="user")
    with tracker.edit() as tx:
        tx.update(ROW, {"answers_path": ANSWERS_REL}, actor="agent")
    (base / ANSWERS_REL).unlink()
    with pytest.raises(GateRefused):
        gate.approve(ROW, approver="user")
    assert row_of(tracker)["status"] == "Ready"


@pytest.mark.parametrize(
    "content",
    ["not json", "[1, 2]", '"text"', '{"a": {"nested": 1}}', '{"a": [1]}', '{"a": NaN}', ""],
    ids=["text", "list", "string", "nested-object", "nested-list", "nan", "empty"],
)
def test_answers_must_be_a_flat_json_object(env, content):
    gate, tracker, base = env
    (base / ANSWERS_REL).write_text(content, encoding="utf-8")
    with pytest.raises(GateRefused):
        gate.approve(ROW, approver="user")
    assert row_of(tracker)["status"] == "Ready"


def test_answers_must_sit_inside_the_base_directory(env, tmp_path):
    _, _, base = env
    outside = tmp_path / "answers.json"
    outside.write_text("{}", encoding="utf-8")
    (base / "cv/tailored/S01/link.json").symlink_to(outside)
    for bad in ("../answers.json", "/etc/hosts", "cv/tailored/S01/link.json"):
        with pytest.raises(GateRefused):
            take_snapshot(base, CV_REL, LETTER_REL, bad)


def test_the_log_never_holds_form_answers(env, tmp_path):
    gate, _, _ = approved(env)
    gate.submit(ROW, Recorder())
    text = "".join(p.read_text() for p in (tmp_path / "logs").glob("*.jsonl"))
    assert "Needs sponsorship" not in text and "32000" not in text and "example.invalid" not in text


# --- the tracker lock is not held during the submit ------------------------------


def test_the_lock_is_free_while_the_submit_runs_and_the_row_is_claimed(env):
    gate, tracker, _ = approved(env)
    seen = {}

    def submit(row, snapshot):
        quick = Tracker(tracker.path, lock_timeout=0.5)  # would time out if the gate still held the lock
        seen["row"] = {r["id"]: r for r in quick.read()}[ROW]
        with quick.edit() as tx:
            tx.update("S02", {"comments": "written during the submit"}, actor="human")
        return True

    gate.submit(ROW, submit)
    assert "submit_unconfirmed" in seen["row"]["flags"]  # claimed before the lock was released
    assert seen["row"]["status"] == "Approved"
    after = row_of(tracker)
    assert after["status"] == "Applied" and "submit_unconfirmed" not in after["flags"]
    assert row_of(tracker, "S02")["comments"] == "written during the submit"


def test_a_second_submit_of_the_same_row_during_the_first_is_refused(env):
    gate, tracker, _ = approved(env)
    inner = Recorder()
    outcome = {}

    def submit(row, snapshot):
        try:
            gate.submit(ROW, inner)
        except GateRefused as err:
            outcome["refused"] = str(err)
        return True

    gate.submit(ROW, submit)
    assert "refused" in outcome and "not confirmed" in outcome["refused"]
    assert inner.calls == []


def test_reconcile_during_a_submit_leaves_the_row_alone(env):
    gate, tracker, base = approved(env)
    outcome = {}

    def submit(row, snapshot):
        (base / CV_REL).write_text("edited mid-submit\n", encoding="utf-8")
        outcome["reset"] = gate.reconcile()
        return True

    gate.submit(ROW, submit)
    assert outcome["reset"] == []
    row = row_of(tracker)
    assert row["status"] == "Applied" and DRIFT_FLAG in row["flags"]


def test_a_failed_submit_releases_the_claim(env):
    gate, tracker, _ = approved(env)
    with pytest.raises(SubmitFailed):
        gate.submit(ROW, Recorder(result=False))
    assert "submit_unconfirmed" not in row_of(tracker)["flags"]


def test_a_non_true_result_counts_as_a_failure(env):
    gate, tracker, _ = approved(env)
    with pytest.raises(SubmitFailed):
        gate.submit(ROW, Recorder(result="yes"))
    assert row_of(tracker)["status"] == "Approved"


def test_an_interrupted_submit_stays_flagged(env):
    gate, tracker, _ = approved(env)
    with pytest.raises(KeyboardInterrupt):
        gate.submit(ROW, Recorder(raises=KeyboardInterrupt()))
    assert "submit_unconfirmed" in row_of(tracker)["flags"]
    with pytest.raises(GateRefused):
        gate.submit(ROW, Recorder())


def test_a_row_reset_during_the_submit_is_reported_not_hidden(env):
    gate, tracker, _ = approved(env)

    def submit(row, snapshot):
        with tracker.edit() as tx:
            tx.reset(ROW, "Ready", actor="user")
        return True

    with pytest.raises(SubmitNotRecorded):
        gate.submit(ROW, submit)
    row = row_of(tracker)
    assert row["status"] == "Ready" and "submit_unconfirmed" in row["flags"]


# --- approving is a person's act at a terminal ------------------------------------


def test_approve_records_the_operating_system_user(env):
    gate, tracker, _ = env
    gate.approve(ROW, approver="user")
    row = row_of(tracker)
    assert row["approved_by"] == "user"
    assert row["approved_os_user"] == getpass.getuser()


def test_approve_can_be_given_the_os_user_and_refuses_a_blank_one(env):
    gate, tracker, _ = env
    with pytest.raises(GateRefused):
        gate.approve(ROW, approver="user", os_user="  ")
    gate.approve(ROW, approver="user", os_user="someone")
    assert row_of(tracker)["approved_os_user"] == "someone"


def test_approve_refuses_if_the_files_changed_since_they_were_shown(env):
    gate, tracker, base = env
    shown = gate.review(ROW)[1].digest
    write_answers(base, {**ANSWERS, "salary_expectation": 1})
    with pytest.raises(GateRefused):
        gate.approve(ROW, approver="user", expected_digest=shown)
    assert row_of(tracker)["status"] == "Ready"


def test_cli_approve_with_the_right_words_on_a_terminal(env):
    _, tracker, base = env
    code, out = run_cli_in_process(env, "approve", FakeTerminal(lines=[COMPANY, ROLE]), FakeTerminal())
    assert code == 0, out.text
    row = row_of(tracker)
    assert row["status"] == "Approved" and row["approved_by"] == "user"
    assert row["approved_os_user"] == getpass.getuser()
    assert row["approved_doc_hash"] == hash_documents(base, CV_REL, LETTER_REL, ANSWERS_REL)


def test_the_approval_prompt_shows_what_is_being_approved(env):
    _, _, base = env
    _, out = run_cli_in_process(env, "approve", FakeTerminal(lines=[COMPANY, ROLE]))
    for expected in (COMPANY, ROLE, CV_REL, LETTER_REL, ANSWERS_REL, "Needs sponsorship", "32000", "sha256:"):
        assert expected in out.text


@pytest.mark.parametrize("stdin_tty,stdout_tty", [(False, True), (True, False), (False, False)])
def test_cli_approve_refuses_without_a_terminal(env, stdin_tty, stdout_tty):
    _, tracker, _ = env
    stdin = FakeTerminal(tty=stdin_tty, lines=[COMPANY, ROLE])  # even with the right words ready
    code, _ = run_cli_in_process(env, "approve", stdin, FakeTerminal(tty=stdout_tty))
    assert code == 1
    assert row_of(tracker)["status"] == "Ready" and row_of(tracker)["approved_by"] == ""


def test_cli_approve_refuses_when_stdin_is_not_a_terminal_in_a_real_process(env):
    _, tracker, _ = env
    done = subprocess.run(
        [sys.executable, "-m", "job_agent.gate", *cli_args(env, "approve", ROW)],
        env={"PYTHONPATH": str(SRC)},
        input=f"{COMPANY}\n{ROLE}\n",
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1
    assert "interactive terminal" in done.stderr
    assert row_of(tracker)["status"] == "Ready"


@pytest.mark.parametrize(
    "typed",
    [
        ["Other Company", ROLE],
        [COMPANY, "Other Role"],
        [COMPANY.lower(), ROLE],
        [COMPANY, ROLE.upper()],
        ["", ""],
        [ROLE, COMPANY],
        [COMPANY],  # input ends before the role is typed
    ],
    ids=["company", "role", "company-case", "role-case", "blank", "swapped", "short"],
)
def test_cli_approve_needs_the_company_and_role_typed_exactly(env, typed):
    _, tracker, _ = env
    code, _ = run_cli_in_process(env, "approve", FakeTerminal(lines=typed))
    assert code == 1
    assert row_of(tracker)["status"] == "Ready" and row_of(tracker)["approved_by"] == ""


def test_cli_approve_ignores_stray_spaces_around_the_typed_words(env):
    _, tracker, _ = env
    code, _ = run_cli_in_process(env, "approve", FakeTerminal(lines=[f"  {COMPANY} ", f" {ROLE}  "]))
    assert code == 0 and row_of(tracker)["status"] == "Approved"


def test_cli_approve_needs_a_company_and_role_on_the_row(env):
    _, tracker, _ = env
    set_cells(tracker, company="", role="")
    code, _ = run_cli_in_process(env, "approve", FakeTerminal(lines=["", ""]))
    assert code == 1 and row_of(tracker)["status"] == "Ready"


def test_cli_approve_refuses_if_files_change_while_the_person_reads(env):
    _, tracker, base = env
    stdin = FakeTerminal(lines=[COMPANY, ROLE], on_read=lambda: write_answers(base, {**ANSWERS, "salary_expectation": 1}))
    code, _ = run_cli_in_process(env, "approve", stdin)
    assert code == 1 and row_of(tracker)["status"] == "Ready"


def test_a_refused_cli_approval_is_logged_and_a_good_one_names_the_os_user(env, tmp_path):
    _, tracker, base = env
    run_cli_in_process(env, "approve", FakeTerminal(tty=False))
    run_cli_in_process(env, "approve", FakeTerminal(lines=[COMPANY, ROLE]))
    events = [json.loads(line) for p in sorted((base.parent / "cli-logs").glob("*.jsonl")) for line in p.read_text().splitlines()]
    assert [(e["action"], e["outcome"]) for e in events] == [("approve", "refused"), ("approve", "ok")]
    assert getpass.getuser() in events[1]["detail"]


def test_cli_approve_on_a_real_terminal(env):
    _, tracker, _ = env
    code, shown = run_cli_on_a_pty(env, f"{COMPANY}\n{ROLE}\n")
    assert code == 0, shown
    row = row_of(tracker)
    assert row["status"] == "Approved" and row["approved_os_user"] == getpass.getuser()


def test_cli_approve_on_a_real_terminal_with_the_wrong_words(env):
    _, tracker, _ = env
    code, shown = run_cli_on_a_pty(env, "Wrong\nWrong\n")
    assert code == 1, shown
    assert row_of(tracker)["status"] == "Ready"


# --- agent code never approves ----------------------------------------------------

REPO = SRC.parent


def program_files():
    """Every Python file that is part of the program or its scripts, except the gate itself."""
    files = [p for p in (SRC / "job_agent").rglob("*.py") if p.name != "gate.py"]
    files += sorted((REPO / "scripts").glob("*.py")) if (REPO / "scripts").exists() else []
    return files


def approve_calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in ("approve", "_approve_interactively"):
                yield node


def test_no_program_code_calls_approve():
    offenders = [p.name for p in program_files() if list(approve_calls(ast.parse(p.read_text(encoding="utf-8"))))]
    assert offenders == []


def test_no_program_code_imports_the_approval_path_from_the_gate():
    for path in program_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "job_agent.gate":
                names = {a.name for a in node.names}
                assert not names & {"main", "_approve_interactively"}, path.name


def test_no_program_code_runs_the_approve_command_or_the_gate_module():
    pattern = re.compile(r"job_agent\.gate|gate\.main|python\s+-m\s+job_agent\.gate", re.I)
    assert [p.name for p in program_files() if pattern.search(p.read_text(encoding="utf-8"))] == []


def test_inside_the_gate_only_the_terminal_function_calls_approve():
    tree = ast.parse((SRC / "job_agent" / "gate.py").read_text(encoding="utf-8"))
    callers = set()
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for call in approve_calls(func):
                name = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
                if name == "approve":
                    callers.add(func.name)
    assert callers == {"_approve_interactively"}


def test_the_terminal_function_is_called_only_from_the_command_line_entry_point():
    tree = ast.parse((SRC / "job_agent" / "gate.py").read_text(encoding="utf-8"))
    callers = {
        func.name
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef)
        for call in ast.walk(func)
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_approve_interactively"
    }
    assert callers == {"main"}
