from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from workflow.jira_outbox import (
    InvalidStateTransitionError,
    JiraOutbox,
    OperationConflictError,
    OutboxCorruptionError,
    OutboxDirective,
    OutboxState,
    ReconciliationDecision,
    UnsafeRetryError,
    stable_operation_id,
)


FIXED_TIME = datetime(2026, 8, 11, 3, 4, 5, tzinfo=timezone.utc)


def make_outbox(tmp_path):
    return JiraOutbox(tmp_path / "jira-outbox.json", clock=lambda: FIXED_TIME)


def test_stable_operation_id_ignores_mapping_order():
    one = stable_operation_id(
        "add_subtask",
        "APPL-418",
        {"fields": {"summary": "TARA", "labels": ["auto"]}, "date": "2026-08-11"},
    )
    two = stable_operation_id(
        "add_subtask",
        "APPL-418",
        {"date": "2026-08-11", "fields": {"labels": ["auto"], "summary": "TARA"}},
    )
    assert one == two
    assert one.startswith("jop_")


def test_idempotency_key_is_stable_but_payload_conflicts_fail_closed(tmp_path):
    outbox = make_outbox(tmp_path)
    first = outbox.prepare(
        "comment",
        "APPL-423",
        {"body": "progress"},
        idempotency_key="Release_claude-s123",
    )
    same = outbox.prepare(
        "comment",
        "APPL-423",
        {"body": "progress"},
        idempotency_key="Release_claude-s123",
    )
    assert same.operation_id == first.operation_id

    with pytest.raises(OperationConflictError):
        outbox.prepare(
            "comment",
            "APPL-423",
            {"body": "different"},
            idempotency_key="Release_claude-s123",
        )


def test_prepare_and_begin_are_durable_write_ahead_steps(tmp_path):
    path = tmp_path / "jira-outbox.json"
    outbox = JiraOutbox(path, clock=lambda: FIXED_TIME)
    prepared = outbox.prepare("transition", "APPL-423", {"status": "진행 중"})

    reloaded = JiraOutbox(path, clock=lambda: FIXED_TIME)
    assert reloaded.require(prepared.operation_id).state is OutboxState.PENDING
    started = reloaded.begin(prepared.operation_id)
    assert started.state is OutboxState.IN_FLIGHT
    assert started.attempt_count == 1

    after_restart = JiraOutbox(path, clock=lambda: FIXED_TIME)
    assert after_restart.require(prepared.operation_id).state is OutboxState.IN_FLIGHT
    assert after_restart.directive(prepared.operation_id) is OutboxDirective.RECONCILE


def test_crash_recovery_blocks_blind_retry_until_reconciled(tmp_path):
    outbox = make_outbox(tmp_path)
    record = outbox.prepare("add_subtask", "APPL-418", {"summary": "new work"})
    outbox.begin(record.operation_id)

    recovered = outbox.recover_in_flight()
    assert [item.operation_id for item in recovered] == [record.operation_id]
    assert outbox.require(record.operation_id).state is OutboxState.UNCERTAIN
    assert outbox.reconciliation_candidates()[0].operation_id == record.operation_id
    with pytest.raises(UnsafeRetryError):
        outbox.begin(record.operation_id)

    pending = outbox.reconcile(
        record.operation_id,
        ReconciliationDecision.NOT_APPLIED,
        note="Jira search found no matching subtask",
    )
    assert pending.state is OutboxState.PENDING
    assert outbox.directive(record.operation_id) is OutboxDirective.APPLY


def test_uncertain_can_reconcile_to_applied_or_remain_blocked(tmp_path):
    outbox = make_outbox(tmp_path)
    first = outbox.prepare("comment", "APPL-423", {"body": "hello"})
    outbox.begin(first.operation_id)
    outbox.mark_uncertain(first.operation_id, "socket closed after send")

    unknown = outbox.reconcile(
        first.operation_id,
        ReconciliationDecision.UNKNOWN,
        note="comment list unavailable",
    )
    assert unknown.state is OutboxState.UNCERTAIN
    assert outbox.directive(first.operation_id) is OutboxDirective.RECONCILE

    applied = outbox.reconcile(
        first.operation_id,
        ReconciliationDecision.APPLIED,
        result={"comment_id": "10101"},
        note="idempotency marker found in Jira",
    )
    assert applied.state is OutboxState.APPLIED
    assert applied.result == {"comment_id": "10101"}
    assert outbox.directive(first.operation_id) is OutboxDirective.SKIP
    with pytest.raises(UnsafeRetryError):
        outbox.begin(first.operation_id)


def test_definite_failure_requires_explicit_requeue(tmp_path):
    outbox = make_outbox(tmp_path)
    record = outbox.prepare("transition", "APPL-423", {"status": "종료 요청"})
    outbox.begin(record.operation_id)
    failed = outbox.mark_failed(record.operation_id, "HTTP 400 transition unavailable")
    assert failed.state is OutboxState.FAILED
    assert outbox.directive(record.operation_id) is OutboxDirective.REVIEW_FAILURE
    with pytest.raises(InvalidStateTransitionError):
        outbox.begin(record.operation_id)

    pending = outbox.requeue_failed(record.operation_id, "workflow corrected")
    assert pending.state is OutboxState.PENDING
    second_attempt = outbox.begin(record.operation_id)
    assert second_attempt.attempt_count == 2


def test_applied_result_and_terminal_state_survive_reload(tmp_path):
    path = tmp_path / "jira-outbox.json"
    outbox = JiraOutbox(path, clock=lambda: FIXED_TIME)
    record = outbox.prepare("create_task", "", {"summary": "Outbox integration"})
    outbox.begin(record.operation_id)
    outbox.mark_applied(record.operation_id, {"key": "APPL-500"})

    reloaded = JiraOutbox(path, clock=lambda: FIXED_TIME)
    applied = reloaded.require(record.operation_id)
    assert applied.state is OutboxState.APPLIED
    assert applied.result == {"key": "APPL-500"}
    assert reloaded.prepare("create_task", "", {"summary": "Outbox integration"}).state is OutboxState.APPLIED


def test_atomic_replace_failure_preserves_disk_and_memory_state(tmp_path, monkeypatch):
    import workflow.jira_outbox as module

    path = tmp_path / "jira-outbox.json"
    outbox = JiraOutbox(path, clock=lambda: FIXED_TIME)
    record = outbox.prepare("comment", "APPL-423", {"body": "safe"})
    original = path.read_text(encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        outbox.begin(record.operation_id)

    assert outbox.require(record.operation_id).state is OutboxState.PENDING
    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_or_unknown_schema_fails_closed(tmp_path):
    path = tmp_path / "jira-outbox.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(OutboxCorruptionError):
        JiraOutbox(path)

    path.write_text(json.dumps({"schema_version": 999, "records": {}}), encoding="utf-8")
    with pytest.raises(OutboxCorruptionError):
        JiraOutbox(path)
