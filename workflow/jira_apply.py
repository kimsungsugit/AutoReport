"""Safe application service for reviewed Jira ``create_task`` proposals.

This module is intentionally narrow: it can create a Jira Task and cannot call
completion or transition APIs.  The stored proposal is the authoritative input.
Every operation is protected by the local write-ahead outbox and an alphanumeric
summary marker derived from the stable proposal id.

All AutoReport processes on one machine must share the same outbox path; a
cross-process lock then serializes marker lookup through Jira POST.  Separate
workstations do not share that lock, so deployments must nominate one Jira-write
worker unless Jira gains a server-side unique external-id field.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from workflow.jira_outbox import (
    JiraOutbox,
    OperationConflictError,
    OutboxDirective,
    OutboxState,
    ReconciliationDecision,
    operation_fingerprint,
    stable_operation_id,
)
from workflow.jira_planning import assess_proposal_quality


_PROPOSAL_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")
_PROJECT_KEY_RE = re.compile(r"[A-Z][A-Z0-9_-]*")
_ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_-]*-[0-9]+")
_HTTP_STATUS_RE = re.compile(r"(?:^|\b)HTTP\s+(\d{3})(?:\b|$)", re.IGNORECASE)
_DERIVED_QUALITY_FIELDS = frozenset(
    {
        "quality_score",
        "quality_grade",
        "blocking_reasons",
        "auto_apply_eligible",
    }
)


class _ApplyLockUnavailable(RuntimeError):
    pass


@contextmanager
def _exclusive_outbox_lock(path: Any):
    """Hold a cross-process one-byte lock adjacent to the shared outbox.

    The lock file is intentionally persistent; the operating system releases the
    byte-range lock when a process exits, including crashes.  This serializes the
    local proxy and post-commit worker across the lookup-to-POST critical section.
    """

    lock_path = path.with_name(f"{path.name}.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
    except OSError as exc:
        raise _ApplyLockUnavailable(f"cannot open Jira apply lock: {exc}") from exc
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise _ApplyLockUnavailable(
                "another local Jira apply process holds the shared outbox lock"
            ) from exc
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


class JiraApplyError(RuntimeError):
    """Base error with enough state for an API response and operator review."""

    def __init__(
        self,
        message: str,
        *,
        operation_id: str = "",
        state: OutboxState | None = None,
        uncertain: bool = False,
        created_key: str = "",
    ) -> None:
        super().__init__(message)
        self.operation_id = operation_id
        self.state = state
        self.uncertain = uncertain
        self.created_key = created_key


class InvalidCreateTaskProposal(JiraApplyError):
    """The stored proposal violates the create-task contract."""


class JiraApplyBlocked(JiraApplyError):
    """No Jira write was attempted because a safe precondition was unavailable."""


class JiraApplyFailed(JiraApplyError):
    """Jira definitively rejected the request."""


class JiraApplyUncertain(JiraApplyError):
    """Jira may have applied the request; reconciliation is required."""


@dataclass(frozen=True)
class CreateTaskApplyResult:
    operation_id: str
    key: str
    marker: str
    state: OutboxState
    created: bool
    already_applied: bool

    @property
    def label(self) -> str:
        """Compatibility alias for callers created before summary-marker dedupe."""

        return self.marker

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "key": self.key,
            "proposal_marker": self.marker,
            "outbox_state": self.state.value,
            "created": self.created,
            "already_applied": self.already_applied,
        }


def proposal_marker(proposal_id: str) -> str:
    """Return a stable alphanumeric token safe for Jira's summary field."""

    clean = str(proposal_id or "").strip()
    if not clean or not _PROPOSAL_ID_RE.fullmatch(clean) or len(clean) > 120:
        raise InvalidCreateTaskProposal("stored proposal id is missing or invalid")
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:20].upper()
    return f"ARID{digest}"


def proposal_revision(proposal: Mapping[str, Any]) -> str:
    """Fingerprint the exact stored revision shown to an approver.

    The proxy binds a submitted approval to this digest before merging reviewed
    editable fields.  This prevents a same-day report regeneration from swapping
    in different content under the same stable proposal id.
    """

    if not isinstance(proposal, Mapping):
        raise InvalidCreateTaskProposal("stored proposal must be an object")
    material = {
        "id": str(proposal.get("id") or "").strip(),
        "type": str(proposal.get("type") or proposal.get("action") or "").strip(),
        "task_key": str(
            proposal.get("task_key") or proposal.get("related_jira_key") or ""
        ).strip(),
        "project_key": str(proposal.get("project_key") or "").strip(),
        "epic_key": str(proposal.get("epic_key") or "").strip(),
        "dedupe_marker": str(proposal.get("dedupe_marker") or "").strip(),
        "summary": str(
            proposal.get("suggested_text") or proposal.get("summary") or ""
        ),
        "description": str(
            proposal.get("suggested_description") or proposal.get("description") or ""
        ),
        "start": str(proposal.get("start") or "").strip(),
        "end": str(proposal.get("end") or "").strip(),
        "report_required": str(proposal.get("report_required") or "").strip().lower(),
    }
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"jrev_{hashlib.sha256(encoded).hexdigest()[:24]}"


def _date_value(value: Any, field_name: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    try:
        return date.fromisoformat(clean).isoformat()
    except ValueError as exc:
        raise InvalidCreateTaskProposal(
            f"stored proposal {field_name} must use YYYY-MM-DD"
        ) from exc


def normalize_create_task_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and reduce a stored suggestion to the only permitted Jira payload."""

    if not isinstance(proposal, Mapping):
        raise InvalidCreateTaskProposal("stored proposal must be an object")
    stored_type = str(proposal.get("type") or proposal.get("action") or "").strip()
    if stored_type != "create_task":
        raise InvalidCreateTaskProposal(
            f"stored proposal type must be create_task, got {stored_type!r}"
        )
    declared_issue_type = str(
        proposal.get("issue_type") or proposal.get("issuetype") or ""
    ).strip()
    if declared_issue_type and declared_issue_type.lower() != "task":
        raise InvalidCreateTaskProposal("create_task proposal issue_type must be Task")
    # A create proposal targets a project/Epic, never an existing issue.  This
    # guard prevents a malformed card from being reinterpreted as a transition.
    if str(proposal.get("task_key") or proposal.get("related_jira_key") or "").strip():
        raise InvalidCreateTaskProposal("create_task proposal must not carry task_key")

    proposal_id = str(proposal.get("id") or "").strip()
    marker = proposal_marker(proposal_id)
    project_key = str(proposal.get("project_key") or "").strip().upper()
    if not _PROJECT_KEY_RE.fullmatch(project_key):
        raise InvalidCreateTaskProposal("stored proposal project_key is missing or invalid")
    epic_key = str(proposal.get("epic_key") or "").strip().upper()
    if epic_key and not _ISSUE_KEY_RE.fullmatch(epic_key):
        raise InvalidCreateTaskProposal("stored proposal epic_key is invalid")
    source_summary = str(
        proposal.get("suggested_text") or proposal.get("summary") or ""
    ).strip()
    if not source_summary:
        raise InvalidCreateTaskProposal("stored proposal summary is required")
    suffix = f" [{marker}]"
    if source_summary.endswith(suffix):
        summary = source_summary
    else:
        summary = f"{source_summary[:255 - len(suffix)].rstrip()}{suffix}"
    description = str(
        proposal.get("suggested_description") or proposal.get("description") or ""
    )
    start = _date_value(proposal.get("start"), "start")
    end = _date_value(proposal.get("end"), "end")
    if not start or not end:
        raise InvalidCreateTaskProposal("stored proposal start and end are required")
    if start and end and start > end:
        raise InvalidCreateTaskProposal("stored proposal start cannot be after end")
    report_required = str(proposal.get("report_required") or "").strip().lower()
    if report_required not in ("yes", "no"):
        raise InvalidCreateTaskProposal("stored proposal report_required must be yes or no")

    return {
        "proposal_id": proposal_id,
        "proposal_marker": marker,
        "issuetype": "Task",
        "project_key": project_key,
        "epic_key": epic_key,
        "summary": summary,
        "description": description,
        "start": start,
        "end": end,
        "report_required": report_required,
    }


def _require_auto_apply_quality(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute the authoritative proposal's quality and fail closed.

    Stored quality fields are derived display data, not authorization.  Removing
    them from the assessor input ensures a caller cannot turn a draft into an
    auto-apply candidate by submitting a forged score or eligibility flag.
    This check intentionally runs before the cross-process lock, outbox prepare,
    provider reconciliation, or Jira mutation.
    """

    source = {
        key: value
        for key, value in proposal.items()
        if key not in _DERIVED_QUALITY_FIELDS
    }
    try:
        assessment = assess_proposal_quality(source)
    except Exception as exc:
        raise JiraApplyBlocked(
            "Jira proposal quality validation failed closed"
        ) from exc
    if not isinstance(assessment, Mapping):
        raise JiraApplyBlocked(
            "Jira proposal quality validation returned an invalid assessment"
        )

    score = assessment.get("quality_score")
    grade = assessment.get("quality_grade")
    raw_reasons = assessment.get("blocking_reasons")
    eligible = assessment.get("auto_apply_eligible")
    if (
        isinstance(score, bool)
        or not isinstance(score, int)
        or not 0 <= score <= 100
        or grade not in {"high", "manual", "draft"}
        or not isinstance(raw_reasons, list)
        or not isinstance(eligible, bool)
    ):
        raise JiraApplyBlocked(
            "Jira proposal quality validation returned an invalid assessment contract"
        )
    reasons = [str(reason).strip() for reason in raw_reasons if str(reason).strip()]

    if eligible is not True or reasons:
        details = "; ".join(reasons[:5]) or "auto_apply_eligible is false"
        raise JiraApplyBlocked(
            f"Jira proposal blocked by server-side quality gate: {details}"
        )
    return dict(assessment)


def _rollout_create_permission(proposal: Mapping[str, Any]) -> tuple[bool, str]:
    """Return whether the configured rollout stage permits a new Jira POST."""

    stage = os.environ.get("JIRA_QUALITY_ROLLOUT_STAGE", "B").strip().upper() or "B"
    if stage not in {"A", "B", "C", "D"}:
        return False, f"unknown Jira quality rollout stage {stage!r}"
    if stage in {"A", "B"}:
        return False, f"Jira quality rollout stage {stage} does not permit Jira create POSTs"
    if stage == "C":
        channel = str(proposal.get("approval_channel") or "").strip().lower()
        if channel != "manual_review":
            return False, (
                "Jira quality rollout stage C requires "
                "approval_channel='manual_review'"
            )
    return True, ""


def _create_operation_payload(normalized: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: normalized[key]
        for key in (
            "issuetype",
            "project_key",
            "epic_key",
            "summary",
            "description",
            "start",
            "end",
            "report_required",
        )
    }


def _is_definite_rejection(error: str) -> bool:
    """Classify errors only when Jira definitely did not accept the mutation."""

    text = str(error or "").strip()
    match = _HTTP_STATUS_RE.search(text)
    if match:
        code = int(match.group(1))
        # Timeout/rate-limit responses are conservatively reconciled by marker.
        return 400 <= code < 500 and code not in (408, 425, 429)
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "unsupported issuetype",
            "project_key 누락",
            "invalid project key",
            "summary is required",
        )
    )


class JiraApplyService:
    """Apply reviewed create-task proposals with local and Jira-side dedupe."""

    def __init__(
        self,
        provider: Any,
        outbox: JiraOutbox,
        *,
        recover_in_flight: bool = True,
    ) -> None:
        self.provider = provider
        self.outbox = outbox
        self._recover_in_flight = recover_in_flight

    def _lookup(self, normalized: Mapping[str, Any], operation_id: str) -> list[dict[str, Any]]:
        finder = getattr(self.provider, "find_issues_by_marker", None)
        if not callable(finder):
            raise JiraApplyBlocked(
                "Jira provider cannot perform proposal-marker reconciliation",
                operation_id=operation_id,
                state=self.outbox.require(operation_id).state,
            )
        try:
            found = finder(normalized["proposal_marker"], normalized["project_key"])
        except Exception as exc:
            raise JiraApplyBlocked(
                f"Jira proposal-marker lookup failed: {exc}",
                operation_id=operation_id,
                state=self.outbox.require(operation_id).state,
            ) from exc
        provider_error = str(getattr(self.provider, "last_error", "") or "").strip()
        if provider_error:
            raise JiraApplyBlocked(
                f"Jira proposal-marker lookup failed: {provider_error}",
                operation_id=operation_id,
                state=self.outbox.require(operation_id).state,
            )
        candidates = [dict(item) for item in (found or []) if isinstance(item, Mapping)]
        marker = str(normalized["proposal_marker"])
        verified = [
            item
            for item in candidates
            if _ISSUE_KEY_RE.fullmatch(str(item.get("key") or "").strip().upper())
            and marker in str(item.get("summary") or "").upper()
        ]
        if candidates and not verified:
            raise JiraApplyBlocked(
                "Jira marker lookup returned results without the exact proposal marker",
                operation_id=operation_id,
                state=self.outbox.require(operation_id).state,
            )
        return verified

    @staticmethod
    def _result_from_record(record: Any, marker: str) -> CreateTaskApplyResult:
        result = record.result if isinstance(record.result, Mapping) else {}
        return CreateTaskApplyResult(
            operation_id=record.operation_id,
            key=str(result.get("key") or ""),
            marker=marker,
            state=record.state,
            created=bool(result.get("created", False)),
            already_applied=True,
        )

    def _record_existing(
        self,
        operation_id: str,
        normalized: Mapping[str, Any],
        found: list[dict[str, Any]],
        *,
        reconcile: bool,
    ) -> CreateTaskApplyResult:
        key = str(found[0].get("key") or "")
        result = {
            "key": key,
            "created": False,
            "matched_keys": [str(item.get("key") or "") for item in found],
            "proposal_marker": normalized["proposal_marker"],
        }
        try:
            if reconcile:
                record = self.outbox.reconcile(
                    operation_id,
                    ReconciliationDecision.APPLIED,
                    result=result,
                    note="proposal marker already exists in Jira",
                )
            else:
                self.outbox.begin(operation_id)
                record = self.outbox.mark_applied(operation_id, result)
        except Exception as exc:
            raise JiraApplyUncertain(
                f"Jira marker match found but outbox persistence failed: {exc}",
                operation_id=operation_id,
                state=self.outbox.require(operation_id).state,
                uncertain=True,
                created_key=key,
            ) from exc
        return CreateTaskApplyResult(
            operation_id=operation_id,
            key=key,
            marker=str(normalized["proposal_marker"]),
            state=record.state,
            created=False,
            already_applied=True,
        )

    def apply_create_task(self, stored_proposal: Mapping[str, Any]) -> CreateTaskApplyResult:
        """Safely satisfy one authoritative stored ``create_task`` proposal."""

        normalized = normalize_create_task_proposal(stored_proposal)
        _require_auto_apply_quality(stored_proposal)
        allow_create, rollout_block_reason = _rollout_create_permission(stored_proposal)
        operation_payload = _create_operation_payload(normalized)
        operation_id = stable_operation_id(
            "create_task",
            "",
            operation_payload,
            idempotency_key=str(normalized["proposal_id"]),
        )
        expected_fingerprint = operation_fingerprint(
            "create_task", "", operation_payload
        )

        # Rollout stages A/B and non-manual stage C must not create even a pending
        # outbox record.  A read-only reload lets an idempotent caller still obtain
        # an already-applied result, or safely reconcile an operation whose Jira
        # outcome was uncertain before the rollout gate closed.
        self.outbox.reload()
        existing = self.outbox.get(operation_id)
        if existing is not None and existing.fingerprint != expected_fingerprint:
            raise InvalidCreateTaskProposal(
                f"operation_id {operation_id!r} already identifies different Jira content"
            )
        if not allow_create:
            if existing is None or existing.state is OutboxState.PENDING:
                raise JiraApplyBlocked(
                    rollout_block_reason,
                    operation_id=operation_id if existing is not None else "",
                    state=existing.state if existing is not None else None,
                )
            if existing.state is OutboxState.APPLIED:
                return self._result_from_record(
                    existing, str(normalized["proposal_marker"])
                )
            if existing.state is OutboxState.FAILED:
                raise JiraApplyFailed(
                    existing.last_error or "previous Jira create attempt failed",
                    operation_id=existing.operation_id,
                    state=existing.state,
                )
        try:
            with _exclusive_outbox_lock(self.outbox.path):
                # Another local process may have committed after this JiraOutbox
                # instance was constructed. Reload only while holding the same lock.
                self.outbox.reload()
                if self._recover_in_flight:
                    self.outbox.recover_in_flight(
                        "apply service restarted before Jira outcome was persisted"
                    )
                return self._apply_create_task_locked(
                    normalized,
                    allow_create=allow_create,
                    rollout_block_reason=rollout_block_reason,
                )
        except _ApplyLockUnavailable as exc:
            raise JiraApplyBlocked(str(exc)) from exc

    def _apply_create_task_locked(
        self,
        normalized: Mapping[str, Any],
        *,
        allow_create: bool,
        rollout_block_reason: str,
    ) -> CreateTaskApplyResult:
        """Apply a normalized proposal while the cross-process lock is held."""

        operation_payload = _create_operation_payload(normalized)
        try:
            record = self.outbox.prepare(
                "create_task",
                "",
                operation_payload,
                idempotency_key=str(normalized["proposal_id"]),
                metadata={
                    "proposal_id": normalized["proposal_id"],
                    "proposal_marker": normalized["proposal_marker"],
                },
            )
        except OperationConflictError as exc:
            raise InvalidCreateTaskProposal(str(exc)) from exc

        directive = self.outbox.directive(record.operation_id)
        if directive is OutboxDirective.SKIP:
            return self._result_from_record(record, str(normalized["proposal_marker"]))
        if directive is OutboxDirective.REVIEW_FAILURE:
            raise JiraApplyFailed(
                record.last_error or "previous Jira create attempt failed",
                operation_id=record.operation_id,
                state=record.state,
            )

        if directive is OutboxDirective.RECONCILE:
            # Constructor recovery normally converted in_flight to uncertain.  This
            # extra guard keeps a service created with recover_in_flight=False safe.
            if record.state is OutboxState.IN_FLIGHT:
                self.outbox.recover_in_flight(
                    "create_task apply encountered an unresolved in-flight operation"
                )
                record = self.outbox.require(record.operation_id)
            found = self._lookup(normalized, record.operation_id)
            if found:
                return self._record_existing(
                    record.operation_id, normalized, found, reconcile=True
                )
            # Jira search is eventually consistent.  One empty lookup after a
            # timeout cannot prove the POST was not applied, so keep this operation
            # uncertain and let later reconciliation discover the marker.  An
            # operator may explicitly use JiraOutbox.reconcile(NOT_APPLIED) only
            # after obtaining stronger evidence that a retry is safe.
            record = self.outbox.reconcile(
                record.operation_id,
                ReconciliationDecision.UNKNOWN,
                note=(
                    "proposal marker is not visible in Jira yet; search may be "
                    "eventually consistent, so automatic retry remains blocked"
                ),
            )
            raise JiraApplyUncertain(
                "Jira create_task outcome remains uncertain; proposal marker is "
                "not visible yet, so no automatic retry was attempted",
                operation_id=record.operation_id,
                state=record.state,
                uncertain=True,
            )

        # A pending operation still performs a Jira-side lookup before POST.  This
        # covers outbox loss/restoration and a task created by another workstation.
        found = self._lookup(normalized, record.operation_id)
        if found:
            return self._record_existing(record.operation_id, normalized, found, reconcile=False)

        # Recheck after acquiring the lock and reconciling marker state.  A record
        # that was uncertain during preflight may have been explicitly returned to
        # pending by another process; closed rollout stages still must never POST.
        if not allow_create:
            raise JiraApplyBlocked(
                rollout_block_reason,
                operation_id=record.operation_id,
                state=record.state,
            )

        self.outbox.begin(record.operation_id)
        creator = getattr(self.provider, "create_issue", None)
        if not callable(creator):
            failed = self.outbox.mark_failed(
                record.operation_id, "Jira provider cannot create issues"
            )
            raise JiraApplyFailed(
                failed.last_error,
                operation_id=record.operation_id,
                state=failed.state,
            )
        try:
            new_key = creator(
                "Task",
                str(normalized["summary"]),
                str(normalized["description"]),
                str(normalized["start"]),
                str(normalized["end"]),
                str(normalized["epic_key"]),
                project_key=str(normalized["project_key"]),
                report_required=str(normalized["report_required"]),
            )
        except Exception as exc:
            if _is_definite_rejection(str(exc)):
                failed = self.outbox.mark_failed(record.operation_id, str(exc))
                raise JiraApplyFailed(
                    failed.last_error,
                    operation_id=record.operation_id,
                    state=failed.state,
                ) from exc
            uncertain = self.outbox.mark_uncertain(record.operation_id, str(exc))
            raise JiraApplyUncertain(
                f"Jira create_task outcome is uncertain: {exc}",
                operation_id=record.operation_id,
                state=uncertain.state,
                uncertain=True,
            ) from exc

        if not new_key:
            error = str(getattr(self.provider, "last_error", "") or "").strip()
            if _is_definite_rejection(error):
                failed = self.outbox.mark_failed(
                    record.operation_id, error or "Jira definitively rejected create_task"
                )
                raise JiraApplyFailed(
                    failed.last_error,
                    operation_id=record.operation_id,
                    state=failed.state,
                )
            uncertain = self.outbox.mark_uncertain(
                record.operation_id,
                error or "Jira returned no issue key; outcome is uncertain",
            )
            raise JiraApplyUncertain(
                uncertain.last_error,
                operation_id=record.operation_id,
                state=uncertain.state,
                uncertain=True,
            )

        result = {
            "key": str(new_key),
            "created": True,
            "proposal_marker": normalized["proposal_marker"],
        }
        try:
            applied = self.outbox.mark_applied(record.operation_id, result)
        except Exception as exc:
            # Jira returned a concrete key, but the durable terminal write failed.
            # Never invite a blind retry; the marker will reconcile on the next call.
            raise JiraApplyUncertain(
                f"Jira created {new_key}, but outbox confirmation failed: {exc}",
                operation_id=record.operation_id,
                state=self.outbox.require(record.operation_id).state,
                uncertain=True,
                created_key=str(new_key),
            ) from exc
        return CreateTaskApplyResult(
            operation_id=record.operation_id,
            key=str(new_key),
            marker=str(normalized["proposal_marker"]),
            state=applied.state,
            created=True,
            already_applied=False,
        )


__all__ = [
    "CreateTaskApplyResult",
    "InvalidCreateTaskProposal",
    "JiraApplyBlocked",
    "JiraApplyError",
    "JiraApplyFailed",
    "JiraApplyService",
    "JiraApplyUncertain",
    "normalize_create_task_proposal",
    "proposal_marker",
    "proposal_revision",
]
