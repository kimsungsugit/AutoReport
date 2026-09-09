"""Durable local write-ahead outbox for Jira mutations.

The outbox deliberately does not call Jira.  A caller first persists a
``pending`` operation, marks it ``in_flight`` immediately before the HTTP
request, and then records a definite outcome.  If the process stops while an
operation is ``in_flight``, startup recovery moves it to ``uncertain`` so the
caller must query Jira before deciding whether a retry is safe.

Typical integration::

    record = outbox.prepare(
        "add_subtask",
        parent_key,
        jira_payload,
        idempotency_key=suggestion_id,
    )
    directive = outbox.directive(record.operation_id)
    if directive is OutboxDirective.APPLY:
        outbox.begin(record.operation_id)  # durable before the Jira request
        try:
            result = jira_create_subtask(jira_payload)
        except DefiniteJiraRejection as exc:
            outbox.mark_failed(record.operation_id, str(exc))
        except Exception as exc:
            outbox.mark_uncertain(record.operation_id, str(exc))
        else:
            outbox.mark_applied(record.operation_id, result)

``uncertain`` operations must be resolved with :meth:`JiraOutbox.reconcile`;
``begin`` refuses to retry them.  The file is safe for one writer process at a
time.  Persistence uses a same-directory temporary file, fsync, and
``os.replace`` so readers see either the old complete document or the new one.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping


SCHEMA_VERSION = 1


class OutboxState(str, Enum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    APPLIED = "applied"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class ReconciliationDecision(str, Enum):
    """Outcome returned by a Jira-side reconciliation probe."""

    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"


class OutboxDirective(str, Enum):
    """What a mutation caller should do for the current persisted state."""

    APPLY = "apply"
    SKIP = "skip"
    RECONCILE = "reconcile"
    REVIEW_FAILURE = "review_failure"


class JiraOutboxError(RuntimeError):
    """Base class for outbox failures."""


class OutboxCorruptionError(JiraOutboxError):
    """Raised when an existing outbox cannot be trusted or decoded."""


class OperationConflictError(JiraOutboxError):
    """Raised when a stable operation id is reused for different content."""


class InvalidStateTransitionError(JiraOutboxError):
    """Raised when an operation cannot move between the requested states."""


class UnsafeRetryError(InvalidStateTransitionError):
    """Raised when retrying could duplicate an already/possibly applied write."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value must be JSON serializable: {exc}") from exc


def _json_copy(value: Any) -> Any:
    """Validate JSON compatibility and detach the stored value from its caller."""

    return json.loads(_canonical_json(value))


def operation_fingerprint(action: str, issue_key: str, payload: Mapping[str, Any]) -> str:
    """Return a full content fingerprint for collision/conflict detection."""

    action_clean = str(action or "").strip()
    if not action_clean:
        raise ValueError("action is required")
    material = {
        "action": action_clean,
        "issue_key": str(issue_key or "").strip(),
        "payload": _json_copy(dict(payload)),
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def stable_operation_id(
    action: str,
    issue_key: str,
    payload: Mapping[str, Any],
    *,
    idempotency_key: str | None = None,
    namespace: str = "jira",
) -> str:
    """Build a deterministic operation id.

    With no explicit ``idempotency_key`` the id is content-addressed.  A caller
    that already has a stable suggestion/proposal id should pass it as the key;
    the record fingerprint still prevents that key from being reused for a
    different Jira request.
    """

    namespace_clean = str(namespace or "jira").strip() or "jira"
    if idempotency_key is None:
        identity = operation_fingerprint(action, issue_key, payload)
    else:
        key_clean = str(idempotency_key).strip()
        if not key_clean:
            raise ValueError("idempotency_key cannot be empty")
        identity = _canonical_json({"namespace": namespace_clean, "key": key_clean})
    digest = hashlib.sha256(
        _canonical_json({"namespace": namespace_clean, "identity": identity}).encode("utf-8")
    ).hexdigest()
    return f"jop_{digest[:32]}"


@dataclass(frozen=True)
class OutboxRecord:
    operation_id: str
    fingerprint: str
    action: str
    issue_key: str
    payload: dict[str, Any]
    state: OutboxState
    created_at: str
    updated_at: str
    attempt_count: int = 0
    last_started_at: str = ""
    applied_at: str = ""
    last_error: str = ""
    result: Any = None
    reconciliation_note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OutboxRecord":
        try:
            state = OutboxState(str(raw["state"]))
            payload = raw.get("payload", {})
            metadata = raw.get("metadata", {})
            if not isinstance(payload, dict):
                raise TypeError("payload must be an object")
            if not isinstance(metadata, dict):
                raise TypeError("metadata must be an object")
            attempt_count = int(raw.get("attempt_count", 0))
            if attempt_count < 0:
                raise ValueError("attempt_count cannot be negative")
            return cls(
                operation_id=str(raw["operation_id"]),
                fingerprint=str(raw["fingerprint"]),
                action=str(raw["action"]),
                issue_key=str(raw.get("issue_key", "")),
                payload=_json_copy(payload),
                state=state,
                created_at=str(raw["created_at"]),
                updated_at=str(raw["updated_at"]),
                attempt_count=attempt_count,
                last_started_at=str(raw.get("last_started_at", "")),
                applied_at=str(raw.get("applied_at", "")),
                last_error=str(raw.get("last_error", "")),
                result=_json_copy(raw.get("result")),
                reconciliation_note=str(raw.get("reconciliation_note", "")),
                metadata=_json_copy(metadata),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OutboxCorruptionError(f"invalid outbox record: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "fingerprint": self.fingerprint,
            "action": self.action,
            "issue_key": self.issue_key,
            "payload": _json_copy(self.payload),
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempt_count": self.attempt_count,
            "last_started_at": self.last_started_at,
            "applied_at": self.applied_at,
            "last_error": self.last_error,
            "result": _json_copy(self.result),
            "reconciliation_note": self.reconciliation_note,
            "metadata": _json_copy(self.metadata),
        }


class JiraOutbox:
    """One-writer durable outbox stored in a local JSON document."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._records = self._load()

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OutboxCorruptionError(f"cannot read outbox {self.path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise OutboxCorruptionError(
                f"unsupported or missing outbox schema in {self.path}"
            )
        records = raw.get("records")
        if not isinstance(records, dict):
            raise OutboxCorruptionError("outbox records must be an object")
        validated: dict[str, dict[str, Any]] = {}
        for operation_id, value in records.items():
            if not isinstance(value, dict):
                raise OutboxCorruptionError(f"record {operation_id!r} must be an object")
            record = OutboxRecord.from_dict(value)
            if operation_id != record.operation_id:
                raise OutboxCorruptionError(
                    f"record key {operation_id!r} does not match operation_id {record.operation_id!r}"
                )
            validated[operation_id] = record.to_dict()
        return validated

    def reload(self) -> None:
        """Reload the file, failing closed if it is corrupt."""

        with self._lock:
            self._records = self._load()

    def _persist(self, records: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": self._now(),
            "records": records,
        }
        fd, temp_name = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _commit(self, candidate: dict[str, dict[str, Any]]) -> None:
        # Publish in-memory state only after the atomic disk replacement succeeds.
        self._persist(candidate)
        self._records = candidate

    @staticmethod
    def _validate_operation_id(operation_id: str) -> str:
        clean = str(operation_id or "").strip()
        if not clean:
            raise ValueError("operation_id is required")
        if len(clean) > 200 or any(ord(char) < 32 for char in clean):
            raise ValueError("operation_id is invalid")
        return clean

    def get(self, operation_id: str) -> OutboxRecord | None:
        with self._lock:
            raw = self._records.get(str(operation_id))
            return OutboxRecord.from_dict(raw) if raw is not None else None

    def require(self, operation_id: str) -> OutboxRecord:
        record = self.get(operation_id)
        if record is None:
            raise KeyError(f"unknown Jira outbox operation: {operation_id}")
        return record

    def list_records(self, *states: OutboxState | str) -> list[OutboxRecord]:
        with self._lock:
            wanted = {OutboxState(state).value for state in states} if states else None
            records = [OutboxRecord.from_dict(raw) for raw in self._records.values()]
            if wanted is not None:
                records = [record for record in records if record.state.value in wanted]
            return sorted(records, key=lambda record: (record.created_at, record.operation_id))

    def prepare(
        self,
        action: str,
        issue_key: str,
        payload: Mapping[str, Any],
        *,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> OutboxRecord:
        """Persist a pending operation before any Jira mutation is attempted.

        Re-preparing identical content returns the existing record, including an
        existing terminal state.  Reusing the id for different content fails
        closed with :class:`OperationConflictError`.
        """

        if operation_id is not None and idempotency_key is not None:
            raise ValueError("use operation_id or idempotency_key, not both")
        action_clean = str(action or "").strip()
        issue_key_clean = str(issue_key or "").strip()
        payload_copy = _json_copy(dict(payload))
        metadata_copy = _json_copy(dict(metadata or {}))
        fingerprint = operation_fingerprint(action_clean, issue_key_clean, payload_copy)
        generated_id = stable_operation_id(
            action_clean,
            issue_key_clean,
            payload_copy,
            idempotency_key=idempotency_key,
        )
        chosen_id = self._validate_operation_id(operation_id or generated_id)

        with self._lock:
            existing = self._records.get(chosen_id)
            if existing is not None:
                record = OutboxRecord.from_dict(existing)
                if record.fingerprint != fingerprint:
                    raise OperationConflictError(
                        f"operation_id {chosen_id!r} already identifies different Jira content"
                    )
                return record

            now = self._now()
            record = OutboxRecord(
                operation_id=chosen_id,
                fingerprint=fingerprint,
                action=action_clean,
                issue_key=issue_key_clean,
                payload=payload_copy,
                state=OutboxState.PENDING,
                created_at=now,
                updated_at=now,
                metadata=metadata_copy,
            )
            candidate = copy.deepcopy(self._records)
            candidate[chosen_id] = record.to_dict()
            self._commit(candidate)
            return record

    def directive(self, operation_id: str) -> OutboxDirective:
        state = self.require(operation_id).state
        if state is OutboxState.PENDING:
            return OutboxDirective.APPLY
        if state is OutboxState.APPLIED:
            return OutboxDirective.SKIP
        if state in (OutboxState.IN_FLIGHT, OutboxState.UNCERTAIN):
            return OutboxDirective.RECONCILE
        return OutboxDirective.REVIEW_FAILURE

    def _replace_record(self, operation_id: str, record: OutboxRecord) -> OutboxRecord:
        candidate = copy.deepcopy(self._records)
        candidate[operation_id] = record.to_dict()
        self._commit(candidate)
        return record

    def begin(self, operation_id: str) -> OutboxRecord:
        """Durably mark an operation in-flight immediately before the HTTP call."""

        with self._lock:
            record = self.require(operation_id)
            if record.state in (
                OutboxState.IN_FLIGHT,
                OutboxState.UNCERTAIN,
                OutboxState.APPLIED,
            ):
                raise UnsafeRetryError(
                    f"operation {operation_id} is {record.state.value}; reconcile or skip it"
                )
            if record.state is OutboxState.FAILED:
                raise InvalidStateTransitionError(
                    f"operation {operation_id} is failed; explicitly requeue it before retry"
                )
            now = self._now()
            updated = OutboxRecord(
                **{
                    **record.to_dict(),
                    "state": OutboxState.IN_FLIGHT,
                    "updated_at": now,
                    "attempt_count": record.attempt_count + 1,
                    "last_started_at": now,
                    "last_error": "",
                }
            )
            return self._replace_record(operation_id, updated)

    def mark_applied(self, operation_id: str, result: Any = None) -> OutboxRecord:
        with self._lock:
            record = self.require(operation_id)
            if record.state is not OutboxState.IN_FLIGHT:
                raise InvalidStateTransitionError(
                    f"only in_flight operations can be marked applied, got {record.state.value}"
                )
            now = self._now()
            updated = OutboxRecord(
                **{
                    **record.to_dict(),
                    "state": OutboxState.APPLIED,
                    "updated_at": now,
                    "applied_at": now,
                    "last_error": "",
                    "result": _json_copy(result),
                }
            )
            return self._replace_record(operation_id, updated)

    def mark_failed(self, operation_id: str, error: str) -> OutboxRecord:
        """Record a definite negative Jira response (known not applied)."""

        with self._lock:
            record = self.require(operation_id)
            if record.state is not OutboxState.IN_FLIGHT:
                raise InvalidStateTransitionError(
                    f"only in_flight operations can fail, got {record.state.value}"
                )
            updated = OutboxRecord(
                **{
                    **record.to_dict(),
                    "state": OutboxState.FAILED,
                    "updated_at": self._now(),
                    "last_error": str(error or "definite Jira failure"),
                }
            )
            return self._replace_record(operation_id, updated)

    def mark_uncertain(self, operation_id: str, error: str) -> OutboxRecord:
        """Record a timeout/disconnect where Jira may already have applied the write."""

        with self._lock:
            record = self.require(operation_id)
            if record.state is not OutboxState.IN_FLIGHT:
                raise InvalidStateTransitionError(
                    f"only in_flight operations can become uncertain, got {record.state.value}"
                )
            updated = OutboxRecord(
                **{
                    **record.to_dict(),
                    "state": OutboxState.UNCERTAIN,
                    "updated_at": self._now(),
                    "last_error": str(error or "Jira outcome is uncertain"),
                }
            )
            return self._replace_record(operation_id, updated)

    def recover_in_flight(self, reason: str = "process restarted before outcome persisted") -> list[OutboxRecord]:
        """Move crash-left ``in_flight`` records to ``uncertain`` in one write."""

        with self._lock:
            candidate = copy.deepcopy(self._records)
            recovered: list[OutboxRecord] = []
            now = self._now()
            for operation_id, raw in list(candidate.items()):
                record = OutboxRecord.from_dict(raw)
                if record.state is not OutboxState.IN_FLIGHT:
                    continue
                updated = OutboxRecord(
                    **{
                        **record.to_dict(),
                        "state": OutboxState.UNCERTAIN,
                        "updated_at": now,
                        "last_error": str(reason),
                        "reconciliation_note": str(reason),
                    }
                )
                candidate[operation_id] = updated.to_dict()
                recovered.append(updated)
            if recovered:
                self._commit(candidate)
            return recovered

    def reconciliation_candidates(self) -> list[OutboxRecord]:
        """Return records that must be checked against Jira before any retry."""

        return self.list_records(OutboxState.IN_FLIGHT, OutboxState.UNCERTAIN)

    def reconcile(
        self,
        operation_id: str,
        decision: ReconciliationDecision | str,
        *,
        result: Any = None,
        note: str = "",
    ) -> OutboxRecord:
        """Persist the result of an external Jira reconciliation query.

        ``not_applied`` safely returns the operation to ``pending``. ``unknown``
        keeps it blocked in ``uncertain``.  No Jira request is made here.
        """

        resolved = ReconciliationDecision(decision)
        with self._lock:
            record = self.require(operation_id)
            if record.state is not OutboxState.UNCERTAIN:
                raise InvalidStateTransitionError(
                    f"only uncertain operations can be reconciled, got {record.state.value}"
                )
            now = self._now()
            values = record.to_dict()
            values["updated_at"] = now
            values["reconciliation_note"] = str(note)
            if resolved is ReconciliationDecision.APPLIED:
                values.update(
                    state=OutboxState.APPLIED,
                    applied_at=now,
                    last_error="",
                    result=_json_copy(result),
                )
            elif resolved is ReconciliationDecision.NOT_APPLIED:
                values.update(
                    state=OutboxState.PENDING,
                    last_error="",
                    result=None,
                )
            else:
                values.update(state=OutboxState.UNCERTAIN)
            updated = OutboxRecord(**values)
            return self._replace_record(operation_id, updated)

    def requeue_failed(self, operation_id: str, note: str = "explicit retry approved") -> OutboxRecord:
        """Explicitly requeue a known failure; failed writes never retry implicitly."""

        with self._lock:
            record = self.require(operation_id)
            if record.state is not OutboxState.FAILED:
                raise InvalidStateTransitionError(
                    f"only failed operations can be requeued, got {record.state.value}"
                )
            updated = OutboxRecord(
                **{
                    **record.to_dict(),
                    "state": OutboxState.PENDING,
                    "updated_at": self._now(),
                    "last_error": "",
                    "reconciliation_note": str(note),
                }
            )
            return self._replace_record(operation_id, updated)


__all__ = [
    "InvalidStateTransitionError",
    "JiraOutbox",
    "JiraOutboxError",
    "OperationConflictError",
    "OutboxCorruptionError",
    "OutboxDirective",
    "OutboxRecord",
    "OutboxState",
    "ReconciliationDecision",
    "SCHEMA_VERSION",
    "UnsafeRetryError",
    "operation_fingerprint",
    "stable_operation_id",
]
