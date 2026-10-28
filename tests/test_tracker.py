"""Step 2: the tracker module."""

import csv
import os
import subprocess
import sys
from pathlib import Path

import pytest

from job_agent import tracker as tracker_module
from job_agent.tracker import (
    APPROVAL_COLUMNS,
    STATUSES,
    BackwardTransition,
    ColumnError,
    DuplicateRow,
    NotPermitted,
    ProtectedColumn,
    StatusError,
    Tracker,
    TrackerLockTimeout,
    UnknownRow,
)

SRC = Path(__file__).resolve().parents[1] / "src"
ROLE_FIT = "role_fit (0-40)"


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def make_row(**over):
    row = {"source": "Test", "company": "Example Ltd", "role": "Graduate Analyst", "location": "London"}
    row.update(over)
    return row


@pytest.fixture
def tracker(sample_tracker_csv):
    return Tracker(sample_tracker_csv)


# --- loading and saving ------------------------------------------------------


def test_sample_loads(tracker):
    rows = tracker.read()
    assert [r["id"] for r in rows] == ["S01", "S02", "S03", "S04", "S05"]
    assert rows[0]["company"] == "G-Research"
    assert rows[1]["salary"] == "£34,587"


def test_status_order_is_the_documented_one():
    assert STATUSES == ("Found", "Tailored", "Ready", "Approved", "Applied", "Interview", "Rejected", "Offer")


def test_missing_columns_are_added_on_first_write(tracker, sample_tracker_csv):
    before = read_csv(sample_tracker_csv)
    with tracker.edit():
        pass
    after = read_csv(sample_tracker_csv)
    for column in ("human_edited_cols", "approved_by", "approved_at", "approved_doc_hash", "cv_path", "letter_path"):
        assert column in after[0]
    for old, new in zip(before, after):
        assert all(new[k] == v for k, v in old.items())


def test_values_with_commas_quotes_and_newlines_round_trip(tracker):
    nasty = 'He said "yes", then\nleft; ünïcode £1,000'
    with tracker.edit() as tx:
        tx.update("S01", {"comments": nasty}, actor="human")
    assert tracker.read()[0]["comments"] == nasty


def test_no_temp_files_left_behind(tracker, sample_tracker_csv):
    with tracker.edit() as tx:
        tx.add_row(make_row())
    names = {p.name for p in sample_tracker_csv.parent.iterdir()}
    assert not [n for n in names if "tmp" in n]


def test_exception_inside_edit_writes_nothing(tracker, sample_tracker_csv):
    before = sample_tracker_csv.read_bytes()
    with pytest.raises(RuntimeError):
        with tracker.edit() as tx:
            tx.add_row(make_row())
            raise RuntimeError("stop")
    assert sample_tracker_csv.read_bytes() == before


def test_failed_replace_leaves_the_original_intact(tracker, sample_tracker_csv, monkeypatch):
    before = sample_tracker_csv.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(tracker_module.os, "replace", boom)
    with pytest.raises(OSError):
        with tracker.edit() as tx:
            tx.add_row(make_row())
    assert sample_tracker_csv.read_bytes() == before
    assert not [p for p in sample_tracker_csv.parent.iterdir() if "tmp" in p.name]


def test_rows_cannot_be_deleted():
    from job_agent.tracker import Txn

    assert not [n for n in dir(Txn) if "delete" in n.lower() or "remove" in n.lower()]


# --- adding rows ------------------------------------------------------------


def test_add_row_defaults_to_found_and_generates_an_id(tracker):
    with tracker.edit() as tx:
        new_id = tx.add_row(make_row())
    row = {r["id"]: r for r in tracker.read()}[new_id]
    assert row["status"] == "Found"
    assert new_id not in ("S01", "S02", "S03", "S04", "S05")


def test_generated_ids_are_unique_inside_one_edit(tracker):
    with tracker.edit() as tx:
        ids = [tx.add_row(make_row(role=f"Role {i}")) for i in range(5)]
    assert len(set(ids)) == 5


def test_duplicate_id_is_refused(tracker):
    with pytest.raises(DuplicateRow):
        with tracker.edit() as tx:
            tx.add_row(make_row(id="S01"))


def test_unknown_column_is_refused(tracker):
    with pytest.raises(ColumnError):
        with tracker.edit() as tx:
            tx.add_row(make_row(favourite_colour="blue"))


def test_new_row_cannot_start_past_found(tracker):
    with pytest.raises(StatusError):
        with tracker.edit() as tx:
            tx.add_row(make_row(status="Approved"))


def test_unknown_row_is_refused(tracker):
    with pytest.raises(UnknownRow):
        with tracker.edit() as tx:
            tx.update("NOPE", {"comments": "x"})


# --- statuses ---------------------------------------------------------------


def set_status_path(tx, row_id, *path, actor="agent"):
    for status in path:
        tx.set_status(row_id, status, actor=actor)


def test_forward_moves_are_allowed(tracker):
    with tracker.edit() as tx:
        set_status_path(tx, "S01", "Tailored", "Ready")
        tx.set_status("S02", "Rejected", actor="agent")  # a withdrawn role may skip ahead
    rows = {r["id"]: r["status"] for r in tracker.read()}
    assert rows["S01"] == "Ready" and rows["S02"] == "Rejected"


def test_backward_moves_are_refused(tracker):
    with pytest.raises(BackwardTransition):
        with tracker.edit() as tx:
            set_status_path(tx, "S01", "Tailored", "Ready")
            tx.set_status("S01", "Found", actor="agent")


def test_a_status_the_tracker_does_not_know_is_an_error_not_a_crash(tracker, sample_tracker_csv):
    rows = read_csv(sample_tracker_csv)
    rows[0]["status"] = "Maybe"
    with open(sample_tracker_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(StatusError):
        with tracker.edit() as tx:
            tx.set_status("S01", "Tailored", actor="agent")


def test_unknown_status_is_refused(tracker):
    with pytest.raises(StatusError):
        with tracker.edit() as tx:
            tx.set_status("S01", "Maybe", actor="agent")


@pytest.mark.parametrize("target", ["Approved", "Applied"])
def test_agent_cannot_set_approved_or_applied(tracker, target):
    with pytest.raises(NotPermitted):
        with tracker.edit() as tx:
            set_status_path(tx, "S01", "Tailored", "Ready")
            tx.set_status("S01", target, actor="agent")


def test_approved_must_come_from_ready(tracker):
    with pytest.raises(StatusError):
        with tracker.edit() as tx:
            tx.set_status("S01", "Approved", actor="user")


def test_applied_must_come_from_approved(tracker):
    with pytest.raises(StatusError):
        with tracker.edit() as tx:
            set_status_path(tx, "S01", "Tailored", "Ready")
            tx.set_status("S01", "Applied", actor="gate")


def test_gate_and_user_may_approve_and_apply(tracker):
    with tracker.edit() as tx:
        set_status_path(tx, "S01", "Tailored", "Ready")
        tx.set_status("S01", "Approved", actor="user")
        tx.set_status("S01", "Applied", actor="gate")
        set_status_path(tx, "S01", "Interview", "Offer")
    assert tracker.read()[0]["status"] == "Offer"


def test_reset_clears_approval_fields(tracker):
    with tracker.edit() as tx:
        set_status_path(tx, "S01", "Tailored", "Ready")
        tx.set_status("S01", "Approved", actor="user")
        tx.update("S01", {c: "x" for c in APPROVAL_COLUMNS}, actor="gate")
        tx.reset("S01", "Ready", actor="gate")
    row = tracker.read()[0]
    assert row["status"] == "Ready"
    assert all(row[c] == "" for c in APPROVAL_COLUMNS)


def test_agent_cannot_reset_an_approved_row(tracker):
    with pytest.raises(NotPermitted):
        with tracker.edit() as tx:
            set_status_path(tx, "S01", "Tailored", "Ready")
            tx.set_status("S01", "Approved", actor="user")
            tx.reset("S01", "Ready", actor="agent")


def test_an_applied_row_cannot_be_reset(tracker):
    with pytest.raises(NotPermitted):
        with tracker.edit() as tx:
            set_status_path(tx, "S01", "Tailored", "Ready")
            tx.set_status("S01", "Approved", actor="user")
            tx.set_status("S01", "Applied", actor="gate")
            tx.reset("S01", "Ready", actor="user")


def test_reset_must_go_backwards_and_not_to_approved(tracker):
    with pytest.raises(StatusError):
        with tracker.edit() as tx:
            tx.reset("S01", "Ready", actor="user")  # S01 is Found: Ready is forward
    with pytest.raises(StatusError):
        with tracker.edit() as tx:
            set_status_path(tx, "S01", "Tailored", "Ready")
            tx.set_status("S01", "Approved", actor="user")
            tx.reset("S01", "Approved", actor="gate")


# --- protected columns and human edits ---------------------------------------


@pytest.mark.parametrize("column", ["status", "id", "human_edited_cols", "approved_by", "approved_at", "approved_doc_hash"])
def test_agent_cannot_write_protected_columns(tracker, column):
    with pytest.raises(ProtectedColumn):
        with tracker.edit() as tx:
            tx.update("S01", {column: "x"}, actor="agent")


def test_human_cannot_write_approval_fields_through_update(tracker):
    with pytest.raises(ProtectedColumn):
        with tracker.edit() as tx:
            tx.update("S01", {"approved_by": "user"}, actor="human")


def test_agent_update_applies_to_untouched_cells(tracker):
    with tracker.edit() as tx:
        result = tx.update("S01", {ROLE_FIT: "35", "comments": "scored"}, actor="agent")
    assert result.skipped == []
    row = tracker.read()[0]
    assert row[ROLE_FIT] == "35" and row["comments"] == "scored"


def test_human_edited_cell_survives_a_programmatic_update(tracker):
    with tracker.edit() as tx:
        tx.update("S01", {ROLE_FIT: "40"}, actor="human")
    with tracker.edit() as tx:
        result = tx.update("S01", {ROLE_FIT: "12", "comments": "rescored"}, actor="agent")
    assert result.skipped == [ROLE_FIT]
    assert result.applied == {"comments": "rescored"}
    row = tracker.read()[0]
    assert row[ROLE_FIT] == "40"
    assert row["comments"] == "rescored"
    assert ROLE_FIT in row["human_edited_cols"].split(";")


def test_cell_edited_by_hand_in_the_file_survives(tracker, sample_tracker_csv):
    with tracker.edit() as tx:  # first write records what the program last wrote
        tx.update("S02", {"comments": "first scan"}, actor="agent")
    rows = read_csv(sample_tracker_csv)  # simulate the user editing a cell in a spreadsheet
    rows[1][ROLE_FIT] = "99"
    with open(sample_tracker_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with tracker.edit() as tx:
        result = tx.update("S02", {ROLE_FIT: "1", "comments": "second scan"}, actor="agent")
    assert result.skipped == [ROLE_FIT]
    row = {r["id"]: r for r in tracker.read()}["S02"]
    assert row[ROLE_FIT] == "99" and row["comments"] == "second scan"


def test_human_edit_of_one_row_does_not_lock_another(tracker):
    with tracker.edit() as tx:
        tx.update("S01", {ROLE_FIT: "40"}, actor="human")
    with tracker.edit() as tx:
        result = tx.update("S02", {ROLE_FIT: "20"}, actor="agent")
    assert result.skipped == []


def test_unchanged_value_is_not_recorded_as_a_human_edit(tracker):
    with tracker.edit() as tx:
        tx.update("S01", {ROLE_FIT: "34"}, actor="human")  # same as the file already holds
    assert tracker.read()[0]["human_edited_cols"] == ""


# --- locking and concurrent writers -------------------------------------------

WORKER = """
import sys
from job_agent.tracker import Tracker
path, worker, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
t = Tracker(path, lock_timeout=60)
for i in range(count):
    with t.edit() as tx:
        tx.add_row({"source": "w" + worker, "company": "Co" + worker, "role": "Role " + str(i), "location": "X"})
with t.edit() as tx:
    tx.update("S0" + str(1 + int(worker) % 5), {"comments": "worker " + worker}, actor="agent")
"""


def spawn(path, worker, count):
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.Popen([sys.executable, "-c", WORKER, str(path), str(worker), str(count)], env=env)


def test_concurrent_writers_lose_no_rows(sample_tracker_csv):
    workers, per_worker = 6, 12
    procs = [spawn(sample_tracker_csv, w, per_worker) for w in range(workers)]
    assert [p.wait(timeout=120) for p in procs] == [0] * workers
    rows = read_csv(sample_tracker_csv)
    ids = [r["id"] for r in rows]
    assert len(rows) == 5 + workers * per_worker
    assert len(set(ids)) == len(ids)
    for w in range(workers):
        assert sum(1 for r in rows if r["source"] == f"w{w}") == per_worker
    assert [r["id"] for r in rows[:5]] == ["S01", "S02", "S03", "S04", "S05"]


def test_lock_times_out_instead_of_waiting_forever(sample_tracker_csv):
    first = Tracker(sample_tracker_csv)
    second = Tracker(sample_tracker_csv, lock_timeout=0.2)
    with first.edit():
        with pytest.raises(TrackerLockTimeout):
            with second.edit():
                pass


def test_readers_wait_for_a_writer_and_see_whole_files(sample_tracker_csv):
    first = Tracker(sample_tracker_csv)
    second = Tracker(sample_tracker_csv, lock_timeout=0.2)
    with first.edit():
        with pytest.raises(TrackerLockTimeout):
            second.read()
