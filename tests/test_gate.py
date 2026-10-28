"""Step 4: the approval gate (hard rule 1)."""

import csv
import inspect
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from job_agent import gate as gate_module
from job_agent.gate import Gate, GateRefused, SubmitFailed, hash_documents, valid_timestamp
from job_agent.tracker import Tracker, UnknownRow

SRC = Path(__file__).resolve().parents[1] / "src"
ROW = "S01"
CV_REL = "cv/tailored/S01/cv.txt"
LETTER_REL = "cv/tailored/S01/letter.txt"
FIXED_NOW = datetime.fromisoformat("2026-11-02T09:30:00+00:00")


class Recorder:
    """Stands in for the real submit step: records calls, never touches a site."""

    def __init__(self, result=True, raises=None):
        self.calls = []
        self.result = result
        self.raises = raises

    def __call__(self, row):
        self.calls.append(dict(row))
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture
def env(tmp_path, sample_tracker_csv):
    base = tmp_path / "work"
    (base / "cv/tailored/S01").mkdir(parents=True)
    (base / CV_REL).write_text("Sample CV text for the invented applicant.\n", encoding="utf-8")
    (base / LETTER_REL).write_text("Sample cover letter text.\n", encoding="utf-8")
    tracker = Tracker(sample_tracker_csv)
    with tracker.edit() as tx:
        tx.update(ROW, {"cv_path": CV_REL, "letter_path": LETTER_REL}, actor="agent")
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
    before = hash_documents(base, CV_REL, LETTER_REL)
    assert before.startswith("sha256:")
    (base / LETTER_REL).write_text("Sample cover letter text, edited.\n", encoding="utf-8")
    assert hash_documents(base, CV_REL, LETTER_REL) != before


def test_hash_tells_the_two_documents_apart(env):
    _, _, base = env
    before = hash_documents(base, CV_REL, LETTER_REL)
    cv, letter = (base / CV_REL).read_bytes(), (base / LETTER_REL).read_bytes()
    (base / CV_REL).write_bytes(letter)
    (base / LETTER_REL).write_bytes(cv)
    assert hash_documents(base, CV_REL, LETTER_REL) != before


def test_hash_ignores_file_times_and_names(env):
    _, _, base = env
    before = hash_documents(base, CV_REL, LETTER_REL)
    os.utime(base / CV_REL, (1, 1))
    assert hash_documents(base, CV_REL, LETTER_REL) == before


@pytest.mark.parametrize("bad", ["../outside.txt", "/etc/hosts", ""])
def test_documents_must_sit_inside_the_base_directory(env, bad):
    _, _, base = env
    with pytest.raises(GateRefused):
        hash_documents(base, bad, LETTER_REL)


def test_a_link_pointing_outside_the_base_directory_is_refused(env, tmp_path):
    _, _, base = env
    outside = tmp_path / "secret.txt"
    outside.write_text("not a document", encoding="utf-8")
    (base / "cv/tailored/S01/link.txt").symlink_to(outside)
    with pytest.raises(GateRefused):
        hash_documents(base, "cv/tailored/S01/link.txt", LETTER_REL)


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
    assert row["approved_doc_hash"] == hash_documents(base, CV_REL, LETTER_REL)


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
    assert row["approved_by"] == "user" and row["approved_doc_hash"] == hash_documents(base, CV_REL, LETTER_REL)
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


def test_cli_approve_check_and_reconcile(env):
    _, tracker, base = env
    assert run_cli(env, "check", ROW).returncode == 1  # Ready: not submittable
    done = run_cli(env, "approve", ROW)
    assert done.returncode == 0, done.stderr
    assert row_of(tracker)["approved_by"] == "user"
    assert run_cli(env, "check", ROW).returncode == 0
    (base / CV_REL).write_text("edited\n", encoding="utf-8")
    out = run_cli(env, "reconcile")
    assert out.returncode == 0 and ROW in out.stdout
    assert row_of(tracker)["status"] == "Ready"


def test_cli_has_no_submit_command(env):
    assert run_cli(env, "submit", ROW).returncode != 0
