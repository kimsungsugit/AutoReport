from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import workflow.jira_apply as jira_apply_module
from workflow.jira_apply import (
    CreateTaskApplyResult,
    InvalidCreateTaskProposal,
    JiraApplyBlocked,
    JiraApplyFailed,
    JiraApplyService,
    JiraApplyUncertain,
    proposal_marker,
    proposal_revision,
)
from workflow.jira_outbox import JiraOutbox, OutboxState


@pytest.fixture(autouse=True)
def _eligible_server_side_quality_assessment(monkeypatch):
    """Keep apply tests focused on mutation safety, not score heuristics."""

    monkeypatch.setenv("JIRA_QUALITY_ROLLOUT_STAGE", "D")
    monkeypatch.setattr(
        jira_apply_module,
        "assess_proposal_quality",
        lambda _proposal: {
            "quality_score": 100,
            "quality_grade": "high",
            "blocking_reasons": [],
            "auto_apply_eligible": True,
        },
    )


def _proposal(**updates):
    proposal = {
        "id": "jtp-AbC123",
        "task_key": "",
        "type": "create_task",
        "project_key": "APPL",
        "epic_key": "APPL-10",
        "suggested_text": "Persist reviewed Jira task",
        "suggested_description": "Canonical stored description",
        "start": "2026-08-11",
        "end": "2026-08-18",
        "report_required": "yes",
        "dedupe_marker": "autoreport:jira-create-task:v1:test",
        "labels": ["autoreport", "planning"],
        "status": "pending",
    }
    proposal.update(updates)
    return proposal


def _quality_eligible_proposal(**updates):
    proposal = _proposal(
        proposal_type="commit_backed",
        evidence_type="commit_backed",
        problem="Jira 품질 미달 제안이 적용 경로에 도달할 수 있다.",
        outcome="서버 품질 게이트가 부적합 제안을 Jira 쓰기 전에 차단한다.",
        scope=["workflow/jira_apply.py"],
        source_files=["workflow/jira_apply.py"],
        source_commits=[{
            "sha": "a" * 40,
            "subject": "fix(jira): enforce server quality gate",
            "url": "https://example.invalid/commit/" + "a" * 40,
        }],
        grouping_rationale="단일 jira 컴포넌트의 품질 적용 경계 변경 1건이다.",
        acceptance_criteria=[
            "부적합 제안 1건 적용 시 Jira 생성 요청이 0건인지 확인한다.",
            "적합 제안 1건 적용 시 outbox 상태가 applied인지 확인한다.",
        ],
        executed_validation=[{
            "command": "pytest tests/test_jira_apply.py -q",
            "environment": "Windows, Python 3.12",
            "actual_result": "32 passed, exit code 0",
            "source": "local:test_jira_apply",
        }],
        risks=["jira 적용 경계 누락 시 품질 미달 Task가 생성될 위험"],
        task_specific_risks=[
            "jira 적용 경계 누락 시 품질 미달 Task가 생성될 위험"
        ],
        schedule_rationale="커밋 1건과 변경 파일 1개를 기준으로 1일을 배정한다.",
        epic_rationale="APPL-10은 Jira 자동화 안전성 범위이므로 연결한다.",
    )
    proposal.update(updates)
    return proposal


class _FakeProvider:
    def __init__(self):
        self.last_error = ""
        self.lookup_results = []
        self.lookup_calls = []
        self.create_calls = []
        self.create_result = "APPL-501"
        self.create_error = None

    def find_issues_by_marker(self, marker, project_key=""):
        self.lookup_calls.append((marker, project_key))
        self.last_error = ""
        return list(self.lookup_results)

    def create_issue(self, *args, **kwargs):
        self.create_calls.append((args, kwargs))
        if "labels" in kwargs:
            raise AssertionError("APPL Task create screen does not expose labels")
        if self.create_error:
            raise self.create_error
        return self.create_result

    def complete_issue(self, *_args, **_kwargs):
        raise AssertionError("create_task must never complete an issue")

    def transition_issue(self, *_args, **_kwargs):
        raise AssertionError("create_task must never transition an issue")


def test_server_quality_gate_recomputes_and_blocks_before_any_mutation(
    tmp_path, monkeypatch
):
    seen = None

    def reject_quality(proposal):
        nonlocal seen
        seen = dict(proposal)
        return {
            "quality_score": 42,
            "quality_grade": "draft",
            "blocking_reasons": ["source commits are missing"],
            "auto_apply_eligible": False,
        }

    monkeypatch.setattr(
        jira_apply_module, "assess_proposal_quality", reject_quality
    )
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")
    forged = _proposal(
        quality_score=100,
        quality_grade="high",
        blocking_reasons=[],
        auto_apply_eligible=True,
    )

    with pytest.raises(JiraApplyBlocked, match="server-side quality gate") as caught:
        JiraApplyService(provider, outbox).apply_create_task(forged)

    assert "source commits are missing" in str(caught.value)
    assert seen is not None
    derived = {
        "quality_score",
        "quality_grade",
        "blocking_reasons",
        "auto_apply_eligible",
    }
    assert not derived & set(seen)
    assert provider.lookup_calls == []
    assert provider.create_calls == []
    assert outbox.list_records() == []
    assert not outbox.path.exists()
    assert not outbox.path.with_name(f"{outbox.path.name}.lock").exists()


def test_default_rollout_stage_b_blocks_before_outbox_or_provider_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("JIRA_QUALITY_ROLLOUT_STAGE", raising=False)
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")

    with pytest.raises(JiraApplyBlocked, match="stage B"):
        JiraApplyService(provider, outbox).apply_create_task(_proposal())

    assert provider.lookup_calls == []
    assert provider.create_calls == []
    assert outbox.list_records() == []
    assert not outbox.path.exists()
    assert not outbox.path.with_name(f"{outbox.path.name}.lock").exists()


def test_unknown_rollout_stage_fails_closed_before_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_QUALITY_ROLLOUT_STAGE", "unexpected")
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")

    with pytest.raises(JiraApplyBlocked, match="unknown Jira quality rollout stage"):
        JiraApplyService(provider, outbox).apply_create_task(_proposal())

    assert provider.lookup_calls == []
    assert provider.create_calls == []
    assert outbox.list_records() == []
    assert not outbox.path.exists()


def test_rollout_stage_c_requires_manual_review_channel_before_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JIRA_QUALITY_ROLLOUT_STAGE", "C")
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")

    with pytest.raises(JiraApplyBlocked, match="manual_review"):
        JiraApplyService(provider, outbox).apply_create_task(
            _proposal(approval_channel="hook")
        )

    assert provider.lookup_calls == []
    assert provider.create_calls == []
    assert outbox.list_records() == []
    assert not outbox.path.exists()


def test_rollout_stage_c_allows_quality_eligible_manual_review(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_QUALITY_ROLLOUT_STAGE", "C")
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")

    result = JiraApplyService(provider, outbox).apply_create_task(
        _proposal(approval_channel="manual_review")
    )

    assert result.state is OutboxState.APPLIED
    assert len(provider.lookup_calls) == 1
    assert len(provider.create_calls) == 1


def test_closed_rollout_returns_identical_applied_record_without_jira_access(
    tmp_path, monkeypatch
):
    path = tmp_path / "jira-outbox.json"
    first_provider = _FakeProvider()
    first = JiraApplyService(first_provider, JiraOutbox(path)).apply_create_task(
        _proposal()
    )
    persisted = path.read_bytes()

    monkeypatch.setenv("JIRA_QUALITY_ROLLOUT_STAGE", "B")
    second_provider = _FakeProvider()
    second = JiraApplyService(second_provider, JiraOutbox(path)).apply_create_task(
        _proposal()
    )

    assert second.operation_id == first.operation_id
    assert second.key == first.key
    assert second.already_applied is True
    assert second_provider.lookup_calls == []
    assert second_provider.create_calls == []
    assert path.read_bytes() == persisted


def test_closed_rollout_can_reconcile_uncertain_without_retrying_create(
    tmp_path, monkeypatch
):
    path = tmp_path / "jira-outbox.json"
    provider = _FakeProvider()
    provider.create_error = TimeoutError("connection closed after request body")
    with pytest.raises(JiraApplyUncertain):
        JiraApplyService(provider, JiraOutbox(path)).apply_create_task(_proposal())
    assert len(provider.create_calls) == 1

    monkeypatch.setenv("JIRA_QUALITY_ROLLOUT_STAGE", "B")
    provider.create_error = None
    marker = proposal_marker("jtp-AbC123")
    provider.lookup_results = [
        {"key": "APPL-777", "summary": f"created before timeout [{marker}]"}
    ]
    reconciled = JiraApplyService(provider, JiraOutbox(path)).apply_create_task(
        _proposal()
    )

    assert reconciled.key == "APPL-777"
    assert reconciled.already_applied is True
    assert len(provider.create_calls) == 1
    assert JiraOutbox(path).require(reconciled.operation_id).state is OutboxState.APPLIED


@pytest.mark.parametrize(
    "assessment",
    [
        None,
        {},
        {
            "quality_score": 100,
            "quality_grade": "high",
            "blocking_reasons": "not-a-list",
            "auto_apply_eligible": True,
        },
    ],
)
def test_server_quality_gate_malformed_result_fails_closed_before_mutation(
    tmp_path, monkeypatch, assessment
):
    monkeypatch.setattr(
        jira_apply_module,
        "assess_proposal_quality",
        lambda _proposal: assessment,
    )
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")

    with pytest.raises(JiraApplyBlocked, match="quality validation|quality gate"):
        JiraApplyService(provider, outbox).apply_create_task(_proposal())

    assert provider.lookup_calls == []
    assert provider.create_calls == []
    assert outbox.list_records() == []
    assert not outbox.path.exists()


def test_server_quality_gate_exception_fails_closed_before_mutation(
    tmp_path, monkeypatch
):
    def broken_quality(_proposal):
        raise RuntimeError("quality policy unavailable")

    monkeypatch.setattr(
        jira_apply_module, "assess_proposal_quality", broken_quality
    )
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")

    with pytest.raises(JiraApplyBlocked, match="failed closed") as caught:
        JiraApplyService(provider, outbox).apply_create_task(_proposal())

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert provider.lookup_calls == []
    assert provider.create_calls == []
    assert outbox.list_records() == []
    assert not outbox.path.exists()


def test_real_quality_engine_blocks_unsubstantiated_direct_call(
    tmp_path, monkeypatch
):
    from workflow.jira_planning import assess_proposal_quality as real_assessor

    monkeypatch.setattr(
        jira_apply_module, "assess_proposal_quality", real_assessor
    )
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")
    forged = _proposal(
        quality_score=100,
        quality_grade="high",
        blocking_reasons=[],
        auto_apply_eligible=True,
    )

    with pytest.raises(JiraApplyBlocked, match="server-side quality gate"):
        JiraApplyService(provider, outbox).apply_create_task(forged)

    assert provider.lookup_calls == []
    assert provider.create_calls == []
    assert outbox.list_records() == []
    assert not outbox.path.exists()


def test_real_quality_engine_allows_complete_proposal_in_stage_d(
    tmp_path, monkeypatch
):
    from workflow.jira_planning import assess_proposal_quality as real_assessor

    monkeypatch.setattr(
        jira_apply_module, "assess_proposal_quality", real_assessor
    )
    proposal = _quality_eligible_proposal()
    assessment = real_assessor(proposal)
    assert assessment["auto_apply_eligible"] is True
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")

    result = JiraApplyService(provider, outbox).apply_create_task(proposal)

    assert result.state is OutboxState.APPLIED
    assert len(provider.lookup_calls) == 1
    assert len(provider.create_calls) == 1


def test_create_task_uses_summary_marker_and_omits_unsupported_labels(tmp_path):
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")
    service = JiraApplyService(provider, outbox)

    result = service.apply_create_task(_proposal())

    assert result.key == "APPL-501"
    assert result.state is OutboxState.APPLIED
    assert result.created is True
    marker = proposal_marker("jtp-AbC123")
    assert provider.lookup_calls == [(marker, "APPL")]
    assert len(provider.create_calls) == 1
    args, kwargs = provider.create_calls[0]
    assert args == (
        "Task",
        f"Persist reviewed Jira task [{marker}]",
        "Canonical stored description",
        "2026-08-11",
        "2026-08-18",
        "APPL-10",
    )
    assert kwargs["project_key"] == "APPL"
    assert kwargs["report_required"] == "yes"
    assert "labels" not in kwargs
    record = outbox.require(result.operation_id)
    assert record.state is OutboxState.APPLIED
    assert record.attempt_count == 1
    assert record.result["key"] == "APPL-501"


def test_second_apply_skips_without_lookup_or_duplicate_create(tmp_path):
    provider = _FakeProvider()
    service = JiraApplyService(provider, JiraOutbox(tmp_path / "jira-outbox.json"))

    first = service.apply_create_task(_proposal())
    second = service.apply_create_task(_proposal())

    assert second.operation_id == first.operation_id
    assert second.key == first.key
    assert second.already_applied is True
    assert len(provider.lookup_calls) == 1
    assert len(provider.create_calls) == 1


def test_shared_outbox_cross_process_lock_blocks_concurrent_create(tmp_path):
    path = tmp_path / "jira-outbox.json"
    provider = _FakeProvider()
    service = JiraApplyService(provider, JiraOutbox(path))
    script = """
import sys
from pathlib import Path
from workflow.jira_apply import _exclusive_outbox_lock
with _exclusive_outbox_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(JiraApplyBlocked, match="shared outbox lock"):
            service.apply_create_task(_proposal())
        assert provider.lookup_calls == []
        assert provider.create_calls == []
        assert service.outbox.list_records() == []
    finally:
        if child.stdin is not None:
            child.stdin.write("\n")
            child.stdin.flush()
        child.wait(timeout=5)

    result = service.apply_create_task(_proposal())
    assert result.state is OutboxState.APPLIED
    assert len(provider.create_calls) == 1


def test_existing_jira_summary_marker_is_recorded_applied_without_create(tmp_path):
    provider = _FakeProvider()
    marker = proposal_marker("jtp-AbC123")
    provider.lookup_results = [
        {"key": "APPL-400", "summary": f"existing [{marker}]"}
    ]
    service = JiraApplyService(provider, JiraOutbox(tmp_path / "jira-outbox.json"))

    result = service.apply_create_task(_proposal())

    assert result.key == "APPL-400"
    assert result.created is False
    assert result.already_applied is True
    assert provider.create_calls == []
    assert service.outbox.require(result.operation_id).state is OutboxState.APPLIED


def test_uncertain_create_waits_for_summary_marker_and_never_blindly_retries(tmp_path):
    path = tmp_path / "jira-outbox.json"
    provider = _FakeProvider()
    provider.create_error = TimeoutError("connection closed after request body")
    first_service = JiraApplyService(provider, JiraOutbox(path))

    with pytest.raises(JiraApplyUncertain) as caught:
        first_service.apply_create_task(_proposal())

    assert caught.value.state is OutboxState.UNCERTAIN
    assert first_service.outbox.require(caught.value.operation_id).state is OutboxState.UNCERTAIN
    assert len(provider.create_calls) == 1

    provider.create_error = None
    still_empty = JiraApplyService(provider, JiraOutbox(path))
    with pytest.raises(JiraApplyUncertain) as still_uncertain:
        still_empty.apply_create_task(_proposal())
    assert still_uncertain.value.state is OutboxState.UNCERTAIN
    assert len(provider.create_calls) == 1

    marker = proposal_marker("jtp-AbC123")
    provider.lookup_results = [
        {"key": "APPL-777", "summary": f"created before timeout [{marker}]"}
    ]
    final_service = JiraApplyService(provider, JiraOutbox(path))
    reconciled = final_service.apply_create_task(_proposal())

    assert reconciled.key == "APPL-777"
    assert reconciled.already_applied is True
    assert len(provider.create_calls) == 1
    assert provider.lookup_calls[-1] == (marker, "APPL")
    assert marker in provider.create_calls[0][0][1]
    assert final_service.outbox.require(reconciled.operation_id).state is OutboxState.APPLIED


def test_definite_rejection_is_failed_and_not_blindly_retried(tmp_path):
    provider = _FakeProvider()
    provider.create_result = ""
    service = JiraApplyService(provider, JiraOutbox(tmp_path / "jira-outbox.json"))

    # create_issue implementations report definite Jira validation failures via
    # last_error while returning an empty key.
    original_create = provider.create_issue

    def rejected(*args, **kwargs):
        result = original_create(*args, **kwargs)
        provider.last_error = "HTTP 400 Bad Request: summary rejected"
        return result

    provider.create_issue = rejected
    with pytest.raises(JiraApplyFailed) as caught:
        service.apply_create_task(_proposal())
    assert caught.value.state is OutboxState.FAILED
    assert len(provider.create_calls) == 1

    with pytest.raises(JiraApplyFailed):
        service.apply_create_task(_proposal())
    assert len(provider.create_calls) == 1


def test_raised_http_400_is_also_a_definite_failure(tmp_path):
    provider = _FakeProvider()
    provider.create_error = RuntimeError("HTTP 400 Bad Request: invalid fields")
    service = JiraApplyService(provider, JiraOutbox(tmp_path / "jira-outbox.json"))

    with pytest.raises(JiraApplyFailed) as caught:
        service.apply_create_task(_proposal())

    assert caught.value.state is OutboxState.FAILED
    assert len(provider.create_calls) == 1


@pytest.mark.parametrize(
    "updates",
    [
        {"type": "complete", "task_key": "APPL-9"},
        {"task_key": "APPL-9"},
        {"project_key": ""},
    ],
)
def test_invalid_stored_proposal_is_rejected_before_outbox_or_jira(tmp_path, updates):
    provider = _FakeProvider()
    outbox = JiraOutbox(tmp_path / "jira-outbox.json")

    with pytest.raises(InvalidCreateTaskProposal):
        JiraApplyService(provider, outbox).apply_create_task(_proposal(**updates))

    assert outbox.list_records() == []
    assert provider.lookup_calls == []
    assert provider.create_calls == []


def test_proposal_marker_is_stable_alphanumeric_and_rejects_untrusted_characters():
    marker = proposal_marker("jtp-abc123")
    assert len(marker) == 24
    assert marker.startswith("ARID")
    assert marker.isalnum()
    assert proposal_marker("jtp-abc123") == marker
    with pytest.raises(InvalidCreateTaskProposal):
        proposal_marker('jtp-abc" OR summary is not EMPTY')


def test_proposal_revision_changes_with_reviewed_content_but_not_status_metadata():
    stored = _proposal()
    revision = proposal_revision(stored)
    assert proposal_revision({**stored, "status": "approved"}) == revision
    assert proposal_revision({**stored, "jira_operation_id": "jop-x"}) == revision
    assert proposal_revision({**stored, "suggested_description": "changed"}) != revision
    assert proposal_revision({**stored, "end": "2026-08-19"}) != revision


def _post_json(port, path, payload, token):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-Proxy-Token": token},
    )
    try:
        response = urllib.request.urlopen(request, timeout=5)
        return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _serve_proxy(jp):
    server = jp._ReusableHTTPServer(("127.0.0.1", 0), jp.ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _write_suggestion(tmp_path, suggestion, file_date="2026-08-11"):
    path = (
        tmp_path
        / "reports"
        / "projects"
        / "Proj"
        / "reports"
        / "jira"
        / f"{file_date}-jira-suggestions.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"date": file_date, "suggestions": [suggestion]}),
        encoding="utf-8",
    )
    return path


def test_proxy_binds_base_revision_and_applies_validated_reviewed_edits(
    tmp_path, monkeypatch
):
    from scripts import jira_proxy as jp

    stored = _proposal(id="jtp-abc123")
    path = _write_suggestion(tmp_path, stored)

    class FakeApplyService:
        seen = None

        def apply_create_task(self, proposal):
            self.seen = dict(proposal)
            return CreateTaskApplyResult(
                operation_id="jop-test",
                key="APPL-888",
                marker=proposal_marker("jtp-abc123"),
                state=OutboxState.APPLIED,
                created=True,
                already_applied=False,
            )

    fake = FakeApplyService()
    monkeypatch.setattr(jp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(jp, "_get_jira_apply_service", lambda: fake)
    server, port = _serve_proxy(jp)
    try:
        code, response = _post_json(
            port,
            "/api/suggestions/Proj-jtp-abc123/approve",
            {
                "date": "2026-08-11",
                "type": "create_task",
                "task_key": "",
                "text": "CLIENT TAMPERED SUMMARY",
                "description": "CLIENT TAMPERED DESCRIPTION",
                "start": "2026-08-12",
                "end": "2026-08-19",
                "report_required": "no",
                "project_key": "APPL",
                "epic_key": "APPL-10",
                "dedupe_marker": stored["dedupe_marker"],
                "proposal_revision": proposal_revision(stored),
            },
            jp.PROXY_TOKEN,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert code == 200
    assert response["ok"] is True
    assert response["key"] == "APPL-888"
    assert response["outbox_state"] == "applied"
    assert response["proposal_marker"] == proposal_marker("jtp-abc123")
    assert fake.seen["suggested_text"] == "CLIENT TAMPERED SUMMARY"
    assert fake.seen["suggested_description"] == "CLIENT TAMPERED DESCRIPTION"
    assert fake.seen["project_key"] == "APPL"
    assert fake.seen["approval_channel"] == "manual_review"
    saved = json.loads(path.read_text(encoding="utf-8"))["suggestions"][0]
    assert saved["status"] == "approved"
    assert saved["suggested_text"] == "CLIENT TAMPERED SUMMARY"
    assert saved["suggested_description"] == "CLIENT TAMPERED DESCRIPTION"
    assert saved["start"] == "2026-08-12"
    assert saved["report_required"] == "no"
    assert saved["created_task_key"] == "APPL-888"
    assert saved["jira_operation_id"] == "jop-test"
    assert saved["jira_proposal_marker"] == proposal_marker("jtp-abc123")
    assert saved["jira_review_revision"] == proposal_revision(stored)
    assert saved["jira_applied_revision"] == proposal_revision(fake.seen)


@pytest.mark.parametrize(
    "submitted",
    [
        {"type": "complete", "task_key": ""},
        {"type": "create_task", "task_key": "APPL-9"},
    ],
)
def test_proxy_rejects_submitted_identity_mismatch_before_apply(
    tmp_path, monkeypatch, submitted
):
    from scripts import jira_proxy as jp

    _write_suggestion(tmp_path, _proposal(id="jtp-abc123"))

    class MustNotApply:
        def apply_create_task(self, _proposal):
            raise AssertionError("mismatched request must not reach apply service")

    monkeypatch.setattr(jp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(jp, "_get_jira_apply_service", lambda: MustNotApply())
    server, port = _serve_proxy(jp)
    try:
        code, response = _post_json(
            port,
            "/api/suggestions/Proj-jtp-abc123/approve",
            {"date": "2026-08-11", **submitted},
            jp.PROXY_TOKEN,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert code == 409
    assert response["ok"] is False
    assert "does not match" in response["error"]


def test_proxy_explicit_stale_date_does_not_fall_forward_to_newer_proposal(
    tmp_path, monkeypatch
):
    from scripts import jira_proxy as jp

    _write_suggestion(tmp_path, _proposal(id="jtp-abc123"), file_date="2026-08-12")

    class MustNotApply:
        def apply_create_task(self, _proposal):
            raise AssertionError("stale-date request must not reach apply service")

    monkeypatch.setattr(jp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(jp, "_get_jira_apply_service", lambda: MustNotApply())
    server, port = _serve_proxy(jp)
    try:
        code, response = _post_json(
            port,
            "/api/suggestions/Proj-jtp-abc123/approve",
            {"date": "2026-08-11", "type": "create_task", "task_key": ""},
            jp.PROXY_TOKEN,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert code == 404
    assert response["ok"] is False
    assert "stored suggestion" in response["error"]


def test_proxy_rejects_stale_same_day_proposal_revision_before_apply(
    tmp_path, monkeypatch
):
    from scripts import jira_proxy as jp

    stored = _proposal(id="jtp-abc123")
    _write_suggestion(tmp_path, stored)

    class MustNotApply:
        def apply_create_task(self, _proposal):
            raise AssertionError("stale revision must not reach apply service")

    monkeypatch.setattr(jp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(jp, "_get_jira_apply_service", lambda: MustNotApply())
    stale = {**stored, "suggested_description": "older content"}
    server, port = _serve_proxy(jp)
    try:
        code, response = _post_json(
            port,
            "/api/suggestions/Proj-jtp-abc123/approve",
            {
                "date": "2026-08-11",
                "type": "create_task",
                "task_key": "",
                "project_key": "APPL",
                "epic_key": "APPL-10",
                "dedupe_marker": stored["dedupe_marker"],
                "text": stored["suggested_text"],
                "description": stale["suggested_description"],
                "start": stored["start"],
                "end": stored["end"],
                "report_required": stored["report_required"],
                "proposal_revision": proposal_revision(stale),
            },
            jp.PROXY_TOKEN,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert code == 409
    assert response["ok"] is False
    assert "reload" in response["error"]


def test_proxy_does_not_mark_replaced_revision_approved_after_jira_apply(
    tmp_path, monkeypatch
):
    from scripts import jira_proxy as jp

    stored = _proposal(id="jtp-abc123")
    path = _write_suggestion(tmp_path, stored)

    class ReplacingApplyService:
        def apply_create_task(self, _proposal):
            replacement = {**stored, "suggested_description": "regenerated content"}
            _write_suggestion(tmp_path, replacement)
            return CreateTaskApplyResult(
                operation_id="jop-race",
                key="APPL-889",
                marker=proposal_marker("jtp-abc123"),
                state=OutboxState.APPLIED,
                created=True,
                already_applied=False,
            )

    monkeypatch.setattr(jp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(jp, "_get_jira_apply_service", lambda: ReplacingApplyService())
    server, port = _serve_proxy(jp)
    try:
        code, response = _post_json(
            port,
            "/api/suggestions/Proj-jtp-abc123/approve",
            {
                "date": "2026-08-11",
                "type": "create_task",
                "task_key": "",
                "project_key": "APPL",
                "epic_key": "APPL-10",
                "dedupe_marker": stored["dedupe_marker"],
                "text": stored["suggested_text"],
                "description": stored["suggested_description"],
                "start": stored["start"],
                "end": stored["end"],
                "report_required": stored["report_required"],
                "proposal_revision": proposal_revision(stored),
            },
            jp.PROXY_TOKEN,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert code == 200
    assert response["ok"] is True
    assert response["status_persisted"] is False
    assert "changed while Jira was applying" in response["persistence_error"]
    current = json.loads(path.read_text(encoding="utf-8"))["suggestions"][0]
    assert current["status"] == "pending"
    assert current["suggested_description"] == "regenerated content"


def test_split_namespaced_create_proposal_id():
    from scripts import jira_proxy as jp

    assert jp._split_namespaced_id("Proj-jtp-abc123") == ("Proj", "jtp-abc123")
    assert jp._split_namespaced_id("my-proj-jtp-abc123") == ("my-proj", "jtp-abc123")
    assert jp._split_namespaced_id("jtp-abc123") == ("", "jtp-abc123")
