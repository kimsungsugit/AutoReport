"""Focused contract tests for the pure Jira planning layer."""

from __future__ import annotations

import json

import pytest

from workflow.jira_planning import (
    assess_proposal_quality,
    build_create_task_proposals,
    enrich_proposal_quality,
    extract_jira_key,
    parse_validation_evidence,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[appl-123] harden approval", "APPL-123"),
        ("APPL-124: harden approval", "APPL-124"),
        ("fix(appl-125): harden approval", "APPL-125"),
        ("fix(proxy): [appl-126] harden approval", "APPL-126"),
        ("feat(queue): appl-131 retain existing convention", "APPL-131"),
        ("fix: harden approval\n\nJira: appl-127", "APPL-127"),
        ("fix: harden approval\nIssue: APPL-128", "APPL-128"),
        ("fix: harden approval\nRefs: APPL-129, APPL-130", "APPL-129"),
    ],
)
def test_extract_jira_key_accepts_explicit_references(text, expected):
    assert extract_jira_key(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "work for APPL-123, not ticket 456",
        "Security example: fetch('/issue/APPL-123/complete')",
        "The code must reject APPL-123 in attacker prose.",
        "fix: harden approval\nThis sentence says Jira: APPL-123 as an example.",
        "no issue here",
    ],
)
def test_extract_jira_key_ignores_free_text_and_code_examples(text):
    assert extract_jira_key(text) == ""


def test_commit_body_requires_explicit_trailer_to_bind_jira_key():
    commits = [
        {
            "sha": "attack1",
            "subject": "docs(security): record hostile input",
            "body": "Example payload includes APPL-123 and must remain unbound.",
        },
        {
            "sha": "linked2",
            "subject": "fix(proxy): preserve uncertain response",
            "body": "Avoid a blind retry after timeout.\n\nRefs: appl-456",
        },
    ]

    proposals = build_create_task_proposals(commits, {})
    by_sha = {
        proposal["source_commits"][0]["sha"]: proposal
        for proposal in proposals
    }

    assert by_sha["attack1"]["related_jira_key"] == ""
    assert by_sha["attack1"]["binding_source"] == "none"
    assert by_sha["linked2"]["related_jira_key"] == "APPL-456"
    assert by_sha["linked2"]["binding_source"] == "commit.text"


def test_builds_traceable_create_task_from_structured_plan_and_commit():
    commits = [{
        "sha": "abc1234",
        "subject": "fix(jira): APPL-42 preserve approval state",
        "changed_files": ["scripts/jira_proxy.py"],
        "validation": ["pytest tests/test_sprint_tasks.py passed"],
    }]
    plan = {"tasks": [{
        "summary": "Harden Jira approval persistence",
        "jira_key": "APPL-42",
        "project_key": "APPL",
        "epic_key": "APPL-10",
        "problem": "Regeneration can resurrect an applied suggestion.",
        "outcome": "Approval state remains durable across regeneration.",
        "scope": ["suggestion persistence", "approval API"],
        "acceptance_criteria": ["Approved cards do not reappear."],
        "remaining_work": ["Run a browser approval round trip."],
        "risks": ["A stale tab can submit twice."],
    }]}

    proposals = build_create_task_proposals(
        commits,
        plan,
        repository_url="https://github.com/acme/autoreport.git",
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["action"] == "create_task"
    assert proposal["issue_type"] == "Task"
    assert proposal["project_key"] == "APPL"
    assert proposal["epic_key"] == "APPL-10"
    assert proposal["related_jira_key"] == "APPL-42"
    assert proposal["binding_source"] == "plan.jira_key"
    assert proposal["problem"] == "Regeneration can resurrect an applied suggestion."
    assert proposal["outcome"] == "Approval state remains durable across regeneration."
    assert "scripts/jira_proxy.py" in proposal["scope"]
    assert proposal["acceptance_criteria"][0] == "Approved cards do not reappear."
    assert len(proposal["acceptance_criteria"]) == 3
    assert sum("0건" in item for item in proposal["acceptance_criteria"]) == 2
    assert proposal["validation"] == ["pytest tests/test_sprint_tasks.py"]
    assert proposal["executed_validation"] == []
    assert proposal["verification_plan"][0]["status"] == "not_run"
    assert proposal["remaining_work"] == ["Run a browser approval round trip."]
    assert proposal["risks"][0] == "A stale tab can submit twice."
    assert len(proposal["task_specific_risks"]) == 1
    assert proposal["source_commits"] == [{
        "sha": "abc1234",
        "subject": "fix(jira): APPL-42 preserve approval state",
        "url": "https://github.com/acme/autoreport/commit/abc1234",
    }]
    assert proposal["id"].startswith("jtp-")
    assert proposal["dedupe_marker"].startswith("autoreport:jira-create-task:v1:")


def test_related_commits_are_grouped_and_same_subject_shas_are_not_collapsed():
    commits = [
        {"sha": "111aaaa", "subject": "fix(jira): CSRF token gate"},
        {"sha": "222bbbb", "subject": "fix(jira): CSRF token gate"},
        {"sha": "333cccc", "subject": "test(jira): CSRF token regression"},
    ]
    proposals = build_create_task_proposals(commits, {})

    assert len(proposals) == 1
    assert [item["sha"] for item in proposals[0]["source_commits"]] == [
        "111aaaa", "222bbbb", "333cccc",
    ]


def test_deterministic_ids_markers_and_output_ignore_input_order():
    commits = [
        {"sha": "b222222", "subject": "test(proxy): approval persistence regression"},
        {"sha": "a111111", "subject": "fix(proxy): approval persistence state"},
    ]
    plan_a = {
        "priority_actions": ["Verify proxy approval persistence"],
        "risks": ["Duplicate writes"],
        "validation": ["Focused pytest"],
    }
    plan_b = {
        "validation": ["Focused pytest"],
        "risks": ["Duplicate writes"],
        "priority_actions": ["Verify proxy approval persistence"],
    }

    first = build_create_task_proposals(commits, plan_a, default_project_key="APPL")
    second = build_create_task_proposals(list(reversed(commits)), plan_b, default_project_key="APPL")

    assert first == second
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["dedupe_marker"] == second[0]["dedupe_marker"]


def test_explicit_jira_fields_outrank_keys_parsed_from_free_text():
    commits = [{
        "sha": "abc7777",
        "subject": "fix(binding): APPL-888 correct key precedence",
        "jira_key": "APPL-123",
    }]
    plan = {"tasks": [{
        "summary": "APPL-999 verify explicit binding",
        "jira_key": "appl-123",
    }]}

    proposal = build_create_task_proposals(commits, plan)[0]

    assert proposal["related_jira_key"] == "APPL-123"
    assert proposal["binding_source"] == "plan.jira_key"
    assert proposal["project_key"] == "APPL"


def test_conflicting_explicit_jira_keys_never_group_related_commits():
    commits = [
        {"sha": "aaa0001", "subject": "fix(jira): approval state", "jira_key": "APPL-101"},
        {"sha": "bbb0002", "subject": "test(jira): approval state", "jira_key": "APPL-202"},
    ]

    proposals = build_create_task_proposals(commits, {})

    assert len(proposals) == 2
    assert {proposal["related_jira_key"] for proposal in proposals} == {"APPL-101", "APPL-202"}
    assert all(len(proposal["source_commits"]) == 1 for proposal in proposals)


def test_commit_sha_binding_matches_the_intended_plan_before_token_inference():
    commits = [
        {"sha": "aaa1111", "subject": "fix(alpha): shared wording"},
        {"sha": "bbb2222", "subject": "fix(beta): shared wording"},
    ]
    plan = {"tasks": [
        {"summary": "Beta follow-up", "commit_shas": ["bbb2222"]},
        {"summary": "Alpha follow-up", "commit_shas": ["aaa1111"]},
    ]}

    proposals = build_create_task_proposals(commits, plan)
    by_summary = {proposal["summary"]: proposal for proposal in proposals}

    assert [c["sha"] for c in by_summary["Alpha follow-up"]["source_commits"]] == ["aaa1111"]
    assert [c["sha"] for c in by_summary["Beta follow-up"]["source_commits"]] == ["bbb2222"]


def test_report_style_plan_sections_create_plan_only_task_with_required_fields():
    proposals = build_create_task_proposals(
        [],
        {
            "priority_actions": ["Add a Jira proposal review gate"],
            "problem": ["Automated suggestions can be overconfident."],
            "acceptance_criteria": ["Every proposal requires human review."],
            "validation": ["Unit tests for confidence handling"],
            "remaining_work": ["Implement the review UI"],
            "risks": ["Bulk approval may bypass review"],
        },
        default_project_key="APPL",
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["source_commits"] == []
    assert proposal["problem"] == "Automated suggestions can be overconfident."
    assert proposal["outcome"]
    assert proposal["scope"]
    assert proposal["acceptance_criteria"][0] == "Every proposal requires human review."
    assert len(proposal["acceptance_criteria"]) == 3
    assert proposal["executed_validation"] == []
    assert proposal["verification_plan"][0]["source"] == "generated:verification_plan"
    assert proposal["remaining_work"] == ["Implement the review UI"]
    assert proposal["risks"][0] == "Bulk approval may bypass review"
    assert proposal["proposal_type"] == "plan_draft"
    assert proposal["quality_grade"] == "draft"
    assert proposal["auto_apply_eligible"] is False


def test_unstructured_meaningful_commit_gets_grounded_quality_fallbacks():
    sha = "0123456789abcdef0123456789abcdef01234567"
    commits = [{
        "sha": sha,
        "subject": "feat(cache): invalidate stale report entries",
        "changed_files": ["workflow/cache.py", "tests/test_cache.py"],
    }]

    first = build_create_task_proposals(commits, {}, default_project_key="APPL")
    second = build_create_task_proposals(commits, {}, default_project_key="APPL")

    assert first == second
    assert len(first) == 1
    proposal = first[0]
    # Prose carries the short SHA; the ticket body repeats this context four-plus
    # times, so full 40-char hashes there were pure noise.
    context = f"커밋 {sha[:12]}의 변경 파일 2개"
    assert context in proposal["problem"]
    assert any(context in item for item in proposal["acceptance_criteria"])
    assert proposal["validation"] == [
        f"검증 근거 미기록, 관련 테스트 실행 필요 ({context})."
    ]
    assert any(context in item for item in proposal["remaining_work"])
    assert any(context in item for item in proposal["risks"])
    # ...but the full hash must survive in source_commits: the quality gate's
    # full_shas check and the 근거 커밋 links both read that field.
    assert sha not in proposal["problem"]
    assert proposal["source_commits"] == [{
        "sha": sha,
        "subject": "feat(cache): invalidate stale report entries",
        "url": "",
    }]


def test_raw_command_without_environment_is_a_not_run_plan_not_pass_evidence():
    proposal = build_create_task_proposals(
        [{
            "sha": "abcdef0123456789",
            "subject": "fix(api): reject stale proposal revisions",
            "changed_files": ["workflow/jira_apply.py"],
            "validation": ["pytest tests/test_jira_apply.py -q: 23 passed"],
        }],
        {},
        default_project_key="APPL",
    )[0]

    assert proposal["executed_validation"] == []
    assert proposal["validation"] == ["pytest tests/test_jira_apply.py -q"]
    assert proposal["verification_plan"][0]["status"] == "not_run"
    assert proposal["verification_plan"][0]["environment"] == (
        "프로젝트 저장소의 현재 Python 테스트 환경"
    )
    assert "actual_result" not in proposal["verification_plan"][0]
    assert "missing_actionable_validation" not in proposal["blocking_reasons"]
    assert any("실행 근거가 없어" in risk for risk in proposal["risks"])


def test_completion_like_plan_input_can_only_produce_create_task_action():
    proposals = build_create_task_proposals(
        [{"sha": "deadbee", "subject": "fix: final cleanup"}],
        {"tasks": [{"summary": "Close finished cleanup", "action": "complete", "status": "done"}]},
    )

    assert proposals
    assert all(proposal["action"] == "create_task" for proposal in proposals)
    assert all(proposal["issue_type"] == "Task" for proposal in proposals)
    assert all("status" not in proposal and "transition" not in proposal for proposal in proposals)


def test_missing_commit_sha_is_rejected_instead_of_inventing_evidence():
    with pytest.raises(ValueError, match="sha/hash"):
        build_create_task_proposals([{"subject": "fix: untraceable"}], {})


def test_public_result_is_json_serializable():
    result = build_create_task_proposals(
        [{"hash": "abc9999", "message": "feat(api): proposal export"}],
        {"next_actions": ["Publish proposal export"]},
    )
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result


def test_parse_validation_evidence_requires_command_environment_result_and_source():
    executed, planned = parse_validation_evidence(
        [
            "[Windows 11 / Python 3.13] pytest tests/test_jira.py -q -> 2 passed",
            "pytest tests/test_other.py -q: 3 passed",
        ],
        source="commit:0123456789abcdef0123456789abcdef01234567",
    )

    assert executed == [{
        "command": "pytest tests/test_jira.py -q",
        "environment": "Windows 11 / Python 3.13",
        "actual_result": "2 passed",
        "source": "commit:0123456789abcdef0123456789abcdef01234567",
    }]
    assert len(planned) == 1
    assert planned[0]["command"] == "pytest tests/test_other.py -q"
    assert planned[0]["environment"] == "프로젝트 저장소의 현재 Python 테스트 환경"
    assert planned[0]["status"] == "not_run"
    assert "actual_result" not in planned[0]


def test_parse_validation_evidence_accepts_zero_exit_code_without_falsy_loss():
    executed, planned = parse_validation_evidence([{
        "command": "pytest tests/test_jira.py -q",
        "environment": "Windows 11 / Python 3.13",
        "exit_code": 0,
        "source": "ci:run-42",
    }])

    assert planned == []
    assert executed[0]["actual_result"] == "0"


def test_parse_validation_evidence_discards_narrative_titles_and_questions():
    executed, planned = parse_validation_evidence(
        [
            "검증",
            "빌드",
            "표기 변형 오탐 가능성 분석",
            "검증 결과가 맞나요?",
            "pytest 테스트 결과를 확인했습니까?",
        ],
        source="commit:" + "a" * 40,
    )

    assert executed == []
    assert planned == []


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/test_jira_planning.py -q",
        "python -m pytest tests/test_jira_planning.py -q",
        "python -m compileall workflow",
        "npm.cmd run build",
        "dotnet test",
        "node --check scripts/jira_proxy.js",
        "ruff check workflow",
        "mypy workflow",
        "cargo test",
        "go test ./...",
    ],
)
def test_parse_validation_evidence_keeps_executable_raw_commands(command):
    executed, planned = parse_validation_evidence(
        [command],
        source="commit:" + "b" * 40,
    )

    assert executed == []
    assert len(planned) == 1
    assert planned[0]["command"] == command
    assert planned[0]["environment"].startswith("프로젝트 저장소의 현재 ")
    assert planned[0]["environment"] != "실행 전 확정 필요"
    assert planned[0]["status"] == "not_run"


def test_explicit_structured_custom_command_is_preserved():
    executed, planned = parse_validation_evidence([{
        "command": ".\\scripts\\verify_jira_contract.ps1",
        "environment": "Windows PowerShell 7",
        "expected_result": "계약 검사가 완료된다.",
        "pass_criteria": "실패 0건",
        "status": "not_run",
        "source": "plan:tasks",
    }])

    assert executed == []
    assert planned[0]["command"] == ".\\scripts\\verify_jira_contract.ps1"


def test_structured_result_without_environment_is_not_execution_evidence():
    executed, planned = parse_validation_evidence([{
        "command": "pytest tests/test_jira_planning.py -q",
        "actual_result": "59 passed",
        "source": "ci:run-with-missing-environment",
    }])

    assert executed == []
    assert planned[0]["status"] == "not_run"
    assert planned[0]["environment"] == "실행 전 확정 필요"
    assert "actual_result" not in planned[0]


def test_noise_only_commit_validation_uses_generated_actionable_fallback():
    sha = "1234567890abcdef1234567890abcdef12345678"
    proposal = build_create_task_proposals(
        [{
            "sha": sha,
            "subject": "fix(jira): validation evidence classification",
            "changed_files": [
                "workflow/jira_planning.py",
                "tests/test_jira_planning.py",
            ],
            "validation": [
                "검증",
                "빌드",
                "표기 변형 오탐 가능성 분석",
                "이 결과가 맞나요?",
            ],
        }],
        {},
        default_project_key="APPL",
        default_epic_key="APPL-10",
    )[0]

    assert proposal["executed_validation"] == []
    assert proposal["verification_plan"] == [{
        "command": "pytest tests/test_jira_planning.py -q",
        "environment": "프로젝트 CI 기본 환경",
        "expected_result": "명령이 오류 없이 완료되고 회귀가 발견되지 않는다.",
        "pass_criteria": "실패 0건",
        "status": "not_run",
        "source": "generated:verification_plan",
    }]
    assert "missing_actionable_validation" not in proposal["blocking_reasons"]
    assert all(
        noise not in proposal["validation"]
        for noise in ("검증", "빌드", "표기 변형 오탐 가능성 분석")
    )


def test_plan_validation_with_pass_word_is_never_claimed_as_executed():
    proposal = build_create_task_proposals(
        [],
        {
            "tasks": [{
                "summary": "검증 게이트 구현",
                "validation": ["pytest tests/test_gate.py -q: 4 passed"],
            }],
        },
        default_project_key="APPL",
        default_epic_key="APPL-10",
    )[0]

    assert proposal["proposal_type"] == "plan_draft"
    assert proposal["executed_validation"] == []
    assert proposal["verification_plan"][0]["status"] == "not_run"
    assert "actual_result" not in proposal["verification_plan"][0]
    assert proposal["auto_apply_eligible"] is False


def test_commit_validation_is_executed_only_with_environment_and_provenance():
    sha = "0123456789abcdef0123456789abcdef01234567"
    proposal = build_create_task_proposals(
        [{
            "sha": sha,
            "subject": "fix(jira): approval revision gate",
            "changed_files": ["workflow/jira_apply.py", "tests/test_jira_apply.py"],
            "validation": ["pytest tests/test_jira_apply.py -q: 29 passed"],
            "validation_environment": "Windows 11 / Python 3.13",
        }],
        {},
        default_project_key="APPL",
        default_epic_key="APPL-10",
    )[0]

    assert proposal["executed_validation"] == [{
        "command": "pytest tests/test_jira_apply.py -q",
        "environment": "Windows 11 / Python 3.13",
        "actual_result": "29 passed",
        "source": f"commit:{sha}",
    }]
    assert proposal["verification_plan"][0]["status"] == "not_run"


def test_structured_commit_validation_round_trips_without_being_discarded():
    sha = "fedcba9876543210fedcba9876543210fedcba98"
    proposal = build_create_task_proposals(
        [{
            "sha": sha,
            "subject": "test(jira): structured validation evidence",
            "executed_validation": [{
                "command": "pytest tests/test_jira_planning.py -q",
                "environment": "Windows 11 / Python 3.13",
                "actual_result": "34 passed",
            }],
        }],
        {},
        default_project_key="APPL",
        default_epic_key="APPL-10",
    )[0]

    assert proposal["executed_validation"] == [{
        "command": "pytest tests/test_jira_planning.py -q",
        "environment": "Windows 11 / Python 3.13",
        "actual_result": "34 passed",
        "source": f"commit:{sha}",
    }]
    assert proposal["quality_dimensions"]["validation_evidence"]["score"] == 15


def test_structured_plan_validation_is_preserved_but_plan_draft_stays_blocked():
    proposal = build_create_task_proposals(
        [],
        {"tasks": [{
            "summary": "승인 검증 계획",
            "executed_validation": [{
                "command": "pytest tests/test_jira_apply.py -q",
                "environment": "Windows 11 / Python 3.13",
                "actual_result": "41 passed",
                "source": "ci:run-100",
            }],
        }]},
        default_project_key="APPL",
        default_epic_key="APPL-10",
    )[0]

    assert proposal["executed_validation"][0]["actual_result"] == "41 passed"
    assert proposal["proposal_type"] == "plan_draft"
    assert proposal["auto_apply_eligible"] is False


def test_unrelated_scopes_split_and_related_components_are_capped_at_three_commits():
    unrelated = build_create_task_proposals(
        [
            {"sha": "a" * 40, "subject": "fix(api): shared approval state"},
            {"sha": "b" * 40, "subject": "fix(ui): shared approval state"},
        ],
        {},
    )
    assert len(unrelated) == 2

    related = build_create_task_proposals(
        [
            {"sha": char * 40, "subject": f"fix(jira): approval gate part {index}"}
            for index, char in enumerate("cdef", start=1)
        ],
        {},
    )
    assert sorted(len(item["source_commits"]) for item in related) == [1, 3]
    assert all(len(item["source_commits"]) <= 3 for item in related)


def _approval_ready_proposal() -> dict:
    return {
        "action": "create_task",
        "proposal_type": "commit_backed",
        "project_key": "APPL",
        "epic_key": "APPL-10",
        "related_jira_key": "",
        "create_suppressed": False,
        "summary": "Jira 승인 게이트 강화",
        "problem": "중복 적용이 발생할 수 있다.",
        "outcome": "승인된 제안만 한 번 적용된다.",
        "scope": ["workflow/jira_apply.py"],
        "source_files": ["workflow/jira_apply.py"],
        "source_commits": [{
            "sha": "0123456789abcdef0123456789abcdef01234567",
            "subject": "fix(jira): approval revision gate",
            "url": "",
        }],
        "grouping_rationale": "단일 커밋 기능 작업으로 분리했다.",
        "acceptance_criteria": [
            "승인되지 않은 제안의 Jira 생성이 0건임을 확인한다.",
            "동일 revision 중복 요청의 Jira 생성이 0건임을 확인한다.",
        ],
        "executed_validation": [],
        "verification_plan": [{
            "command": "pytest tests/test_jira_apply.py -q",
            "environment": "Windows 11 / Python 3.13",
            "expected_result": "모든 승인 게이트 테스트가 완료된다.",
            "pass_criteria": "실패 0건",
            "status": "not_run",
            "source": "generated:verification_plan",
        }],
        "risks": ["승인 revision 불일치 시 중복 Jira가 생성될 수 있다."],
        "task_specific_risks": ["승인 revision 불일치 시 중복 Jira가 생성될 수 있다."],
        "schedule_rationale": "커밋 1건과 테스트 1건 기준으로 1영업일을 산정했다.",
        "epic_rationale": "APPL-10 승인 자동화 목표에 직접 연결된다.",
    }


def test_assessment_allows_complete_not_run_plan_and_is_exactly_100_points():
    assessment = assess_proposal_quality(_approval_ready_proposal())

    assert assessment["quality_score"] == 100
    assert assessment["quality_grade"] == "high"
    assert assessment["blocking_reasons"] == []
    assert assessment["auto_apply_eligible"] is True
    assert sum(
        dimension["score"]
        for dimension in assessment["quality_dimensions"].values()
    ) == 100


def test_commit_backed_proposal_without_changed_files_is_hard_blocked():
    proposal = _approval_ready_proposal()
    proposal["source_files"] = []

    assessment = assess_proposal_quality(proposal)

    assert assessment["auto_apply_eligible"] is False
    assert "missing_changed_file_evidence" in assessment["blocking_reasons"]
    traceability = assessment["quality_dimensions"]["traceability"]
    assert traceability["checks"]["changed_file_evidence"] is False
    assert assessment["quality_score"] < 100


def test_builder_without_file_evidence_avoids_zero_file_claim_and_blocks_auto_apply():
    proposal = build_create_task_proposals(
        [{
            "sha": "a" * 40,
            "subject": "fix(jira): approval revision handling",
        }],
        {},
        default_project_key="APPL",
        default_epic_key="APPL-10",
    )[0]

    assert proposal["source_files"] == []
    assert all("변경 파일 0개" not in item for item in proposal["acceptance_criteria"])
    assert any("파일 근거 누락 0건" in item for item in proposal["acceptance_criteria"])
    assert "missing_changed_file_evidence" in proposal["blocking_reasons"]
    assert proposal["auto_apply_eligible"] is False
    assert any(
        "Task 종료 전에 해당 검증 계획을 실행해 실패 0건" in risk
        for risk in proposal["task_specific_risks"]
    )


@pytest.mark.parametrize(
    ("subject", "parent_count"),
    [
        ("chore(auto): end-of-day snapshot 2026-08-11", 1),
        ("docs: nightly snapshot", 1),
        ("WIP parser changes", 1),
        ("chore: tmp", 1),
        ("chore: auto commit 2026-08-11", 1),
        ("Merge branch 'main'", 2),
        ("feat: combine release histories", 2),
    ],
)
def test_public_assessor_blocks_generic_or_non_actionable_commits(
    subject,
    parent_count,
):
    proposal = _approval_ready_proposal()
    proposal["source_commits"][0]["subject"] = subject
    proposal["source_commits"][0]["parent_count"] = parent_count

    assessment = assess_proposal_quality(proposal)

    assert assessment["auto_apply_eligible"] is False
    assert "generic_or_non_actionable_commit" in assessment["blocking_reasons"]
    assert (
        assessment["quality_dimensions"]["task_coherence"]["checks"]
        ["actionable_commit_subjects"]
        is False
    )


def test_snapshot_feature_subject_is_not_overblocked():
    proposal = _approval_ready_proposal()
    proposal["source_commits"][0]["subject"] = "feat: add snapshot export endpoint"

    assessment = assess_proposal_quality(proposal)

    assert "generic_or_non_actionable_commit" not in assessment["blocking_reasons"]
    assert assessment["auto_apply_eligible"] is True


def test_plan_draft_and_existing_jira_binding_can_never_auto_apply():
    plan_draft = _approval_ready_proposal()
    plan_draft.update({"proposal_type": "plan_draft", "source_commits": []})
    draft_assessment = assess_proposal_quality(plan_draft)
    assert draft_assessment["quality_grade"] == "draft"
    assert draft_assessment["auto_apply_eligible"] is False
    assert "plan_draft_requires_human_planning" in draft_assessment["blocking_reasons"]

    bound = _approval_ready_proposal()
    bound["related_jira_key"] = "APPL-42"
    bound_assessment = assess_proposal_quality(bound)
    assert bound_assessment["quality_grade"] == "manual"
    assert bound_assessment["auto_apply_eligible"] is False
    assert (
        "existing_jira_binding_suppresses_create"
        in bound_assessment["blocking_reasons"]
    )


def test_enrich_quality_returns_copy_and_is_deterministic():
    original = _approval_ready_proposal()
    first = enrich_proposal_quality(original)
    second = enrich_proposal_quality(original)

    assert first == second
    assert first is not original
    assert "quality_score" not in original
    first["scope"].append("mutated")
    assert original["scope"] == ["workflow/jira_apply.py"]


def test_every_evidence_sentence_uses_short_shas_not_full_hashes():
    """The context string is reused across 4+ sentences — it must stay short."""
    shas = [f"{index:040x}" for index in range(1, 4)]
    commits = [
        {
            "sha": sha,
            "subject": f"feat(cache): invalidate stale entries step {index}",
            "changed_files": ["workflow/cache.py"],
        }
        for index, sha in enumerate(shas)
    ]

    proposal = build_create_task_proposals(commits, {}, default_project_key="APPL")[0]
    assert len(proposal["source_commits"]) == 3

    reused = [
        proposal["problem"],
        *proposal["acceptance_criteria"],
        *proposal["remaining_work"],
        *proposal["risks"],
    ]
    assert not any(sha in text for sha in shas for text in reused)
    assert all(sha[:12] in proposal["problem"] for sha in shas)
    # The full hashes survive where they are actually needed.
    assert {item["sha"] for item in proposal["source_commits"]} == set(shas)


def test_prose_wording_does_not_move_the_proposal_identity():
    """id/dedupe_marker hash summary+shas only — changed_files alters the prose
    (변경 파일 N개) but must never reshuffle an already-applied proposal."""
    base = {
        "sha": "0123456789abcdef0123456789abcdef01234567",
        "subject": "feat(cache): invalidate stale report entries",
    }
    one_file = build_create_task_proposals(
        [{**base, "changed_files": ["workflow/cache.py"]}], {}, default_project_key="APPL"
    )[0]
    three_files = build_create_task_proposals(
        [{**base, "changed_files": ["workflow/cache.py", "a.py", "b.py"]}],
        {},
        default_project_key="APPL",
    )[0]

    assert one_file["problem"] != three_files["problem"]
    assert one_file["id"] == three_files["id"]
    assert one_file["dedupe_marker"] == three_files["dedupe_marker"]


def test_rendered_description_is_a_plain_work_list_without_machine_tokens():
    """The reviewer of record is a manager, so the body is the work and nothing else.

    The earlier layout opened with 목적/문제 headings and closed with sha links;
    that reads as a diff summary, not as work somebody can approve.
    """
    from scripts.generate_periodic_reports import _jira_plan_description

    body = _jira_plan_description({
        "problem": "파서가 헤더를 조기 종료한다.",
        "outcome": "전체 매크로를 파싱한다.",
        "scope": ["소스 매크로 파싱 범위 확장"],
        "acceptance_criteria": ["매크로 수가 소스 기준 100%와 일치한다."],
        "executed_validation": [],
        "verification_plan": [],
        "remaining_work": [],
        "risks": [],
        "source_commits": [],
        "dedupe_marker": "autoreport:jira-create-task:v1:deadbeefdeadbeefdead",
    })

    assert "deadbeef" not in body
    assert "h2." not in body
    for absent in ("목적", "문제 / 배경", "완료 조건", "리스크", "근거 커밋"):
        assert absent not in body
    assert body.startswith("* 소스 매크로 파싱 범위 확장")
    # The one audit line survives: without it a plan reads as a finished result.
    assert body.endswith("* 검증 미실행")


def test_rendered_description_omits_commit_links_and_the_verification_line_when_run():
    from scripts.generate_periodic_reports import _jira_plan_description

    sha = "f9590adc3613a0ed8f27176b17574bc8f24783cc"
    body = _jira_plan_description({
        "problem": "p",
        "outcome": "헤더 캡 제거",
        "scope": ["헤더 캡 제거로 전체 파싱"],
        "acceptance_criteria": ["c"],
        "executed_validation": [{"command": "pytest -q", "actual_result": "24 passed"}],
        "risks": ["r"],
        "source_commits": [{
            "sha": sha,
            "subject": "fix(parser): 헤더 캡 제거",
            "url": f"https://example.invalid/commit/{sha}",
        }],
        "dedupe_marker": "autoreport:jira-create-task:v1:abc",
    })

    assert sha[:12] not in body and "example.invalid" not in body
    assert "pytest" not in body and "리스크" not in body
    # Something did run, so the "검증 미실행" flag must not appear.
    assert "검증 미실행" not in body
    assert body == "* 헤더 캡 제거로 전체 파싱"


def test_scope_is_capped_for_reading_and_code_paths_are_dropped():
    """A 20-file commit must not open the ticket with a 20-line wall of paths.

    File paths name code, not work, so they are removed outright; what remains is
    capped so the card stays scannable.
    """
    from scripts.generate_periodic_reports import (
        _SCOPE_RENDER_LIMIT,
        _jira_plan_description,
    )

    paths = [f"src/module_{index:02d}.py" for index in range(20)]
    body = _jira_plan_description({
        "problem": "p", "outcome": "o", "scope": paths,
        "acceptance_criteria": ["c"], "source_commits": [],
    })
    assert ".py" not in body and "src/" not in body

    readable = [f"{index}번 화면 정비" for index in range(20)]
    body = _jira_plan_description({
        "problem": "p", "outcome": "o", "scope": readable,
        "acceptance_criteria": ["c"], "source_commits": [],
    })
    bullets = [line for line in body.splitlines() if line != "* 검증 미실행"]
    assert len(bullets) == _SCOPE_RENDER_LIMIT


def test_schedule_and_epic_rationale_are_scored_but_not_rendered():
    """Generator meta-commentary stays off the ticket, on the proposal."""
    from scripts.generate_periodic_reports import _jira_plan_description
    from workflow.jira_planning import assess_proposal_quality

    proposal = {
        "problem": "p", "outcome": "o", "scope": ["a.py"],
        "acceptance_criteria": ["관련 없는 변경이 0건임을 확인한다."],
        "source_commits": [],
        "schedule_rationale": "검토·검증에 3영업일을 산정했다.",
        "epic_rationale": "명시적으로 지정된 Epic APPL-418의 목표 범위에 연결한다.",
    }
    body = _jira_plan_description(proposal)

    assert "일정 / Epic 근거" not in body
    assert "3영업일" not in body
    assert "APPL-418" not in body
    # The fields still reach the scorer, so removing them from the body is free:
    # the same proposal without them scores strictly lower.
    stripped = {**proposal, "schedule_rationale": "", "epic_rationale": ""}
    assert (
        assess_proposal_quality(proposal)["quality_score"]
        > assess_proposal_quality(stripped)["quality_score"]
    )


def test_structured_task_carries_purpose_exclusions_and_a_work_breakdown():
    """A plan-authored ticket reaches the reviewer intact: why, what is out, how.

    These three fields are the difference between a card someone can approve and
    a card someone has to rewrite, so they travel from the plan document to the
    proposal without the generator paraphrasing them.
    """
    commits = [{
        "sha": "c0ffee1",
        "subject": "feat(tara): clause 15 coverage matrix",
        "changed_files": ["GUI/tara_simulator.py"],
    }]
    plan = {"tasks": [{
        "summary": "ISO 21434 규범 요구사항 커버리지 매트릭스 최신화",
        "purpose": "심사 시점에 조항별 갭을 즉시 확인할 수 있게 한다.",
        "problem": "현행 매트릭스는 Clause 15 계열 7단계만 판정한다.",
        "scope": ["Clause 5~15 RQ/WP 원장 재정비"],
        "out_of_scope": ["도출된 갭의 해소(신규 문서 작성)"],
        "acceptance_criteria": ["전 항목이 상태값과 근거 링크를 갖는다."],
        "subtasks": [
            {
                "summary": "규범 요구사항 기준 원장 정비",
                "description": "Clause 5~15의 RQ/WP 식별자를 단일 원장으로 정리한다.",
                "acceptance_criteria": ["현행 매트릭스 대비 차이 목록 확정"],
            },
            {"summary": "산출물·근거 인벤토리 매핑"},
        ],
    }]}

    proposal = build_create_task_proposals(commits, plan, default_project_key="APPL")[0]

    assert proposal["purpose"] == "심사 시점에 조항별 갭을 즉시 확인할 수 있게 한다."
    assert proposal["out_of_scope"] == ["도출된 갭의 해소(신규 문서 작성)"]
    assert [item["summary"] for item in proposal["subtasks"]] == [
        "규범 요구사항 기준 원장 정비",
        "산출물·근거 인벤토리 매핑",
    ]
    # A bare-string step is a valid step; it just has nothing more to say.
    assert proposal["subtasks"][1] == {
        "summary": "산출물·근거 인벤토리 매핑",
        "description": "",
        "acceptance_criteria": [],
    }


def test_structured_task_supersedes_the_same_action_listed_as_a_bare_string():
    """A report names the same work twice; the reviewer must see one card.

    The plan document keeps rendering flat ``priority_actions``, so the LLM is
    asked to repeat each action verbatim in ``tasks``.  Keying plan identity on
    the section would turn that duplication into two proposals — one rich, one
    empty — for a single piece of work.
    """
    action = "ISO 21434 규범 요구사항 커버리지 매트릭스 최신화"
    plan = {
        "priority_actions": [action],
        "tasks": [{
            "summary": action,
            "purpose": "심사 대응 시 갭을 즉시 확인한다.",
            "subtasks": [{"summary": "기준 원장 정비"}],
        }],
    }

    proposals = build_create_task_proposals([], plan, default_project_key="APPL")

    assert len(proposals) == 1
    assert proposals[0]["purpose"] == "심사 대응 시 갭을 즉시 확인한다."
    assert [item["summary"] for item in proposals[0]["subtasks"]] == ["기준 원장 정비"]


def test_commit_only_proposal_derives_a_breakdown_from_its_own_evidence():
    """No plan author, so every derived step names evidence already on the card."""
    commits = [{
        "sha": "d1ff00d",
        "subject": "fix(parser): 헤더 캡 제거",
        "changed_files": ["report_gen/source_parser.py", "tests/test_parser.py"],
    }]

    proposal = build_create_task_proposals(commits, {}, default_project_key="APPL")[0]

    summaries = [item["summary"] for item in proposal["subtasks"]]
    assert summaries[0] == "변경 범위 확인"
    # The verification step quotes the command the proposal actually plans to run.
    command = proposal["verification_plan"][0]["command"]
    assert f"검증 실행 — {command}" in summaries
    assert "tests/test_parser.py" in command
    assert all(item["acceptance_criteria"] for item in proposal["subtasks"])


def test_plan_draft_without_an_author_breakdown_shows_no_invented_steps():
    """Nothing was executed and nobody wrote a breakdown: say nothing."""
    from scripts.generate_periodic_reports import _jira_plan_description

    proposal = build_create_task_proposals(
        [], {"priority_actions": ["검증 커버리지 확대"]}, default_project_key="APPL"
    )[0]

    assert proposal["subtasks"] == []
    assert "h2. 서브작업" not in _jira_plan_description(proposal)


def test_rendered_description_prefers_the_work_breakdown_over_scope():
    """When the author wrote a breakdown, that is the work list a reviewer reads."""
    from scripts.generate_periodic_reports import _jira_plan_description

    body = _jira_plan_description({
        "purpose": "심사 시점에 조항별 상태를 답할 수 있어야 한다.",
        "problem": "현행 매트릭스는 일부 단계만 판정한다.",
        "outcome": "전 조항 커버리지가 산출된다.",
        "scope": ["Clause 5~15 RQ/WP 원장"],
        "out_of_scope": ["갭 해소용 신규 문서 작성"],
        "subtasks": [
            {
                "summary": "기준 원장 정비",
                "description": "RQ/WP 식별자를 단일 원장으로 정리한다.",
                "acceptance_criteria": ["차이 목록 확정"],
            },
            {"summary": "근거 인벤토리 매핑"},
        ],
        "acceptance_criteria": ["전 항목이 상태값을 갖는다."],
        "source_commits": [],
    })

    assert body.startswith("* 기준 원장 정비\n* 근거 인벤토리 매핑")
    # Scope, exclusions and per-subtask criteria stay on the proposal, off the card.
    assert "Clause" not in body
    assert "갭 해소용" not in body
    assert "차이 목록 확정" not in body


def test_rendered_breakdown_is_capped_so_the_card_stays_scannable():
    from scripts.generate_periodic_reports import (
        _SCOPE_RENDER_LIMIT,
        _jira_plan_description,
    )

    body = _jira_plan_description({
        "problem": "p", "outcome": "o", "scope": ["a"],
        "acceptance_criteria": ["c"], "source_commits": [],
        "subtasks": [{"summary": f"{index}단계 정비"} for index in range(1, 13)],
    })

    bullets = [line for line in body.splitlines() if line != "* 검증 미실행"]
    assert len(bullets) == _SCOPE_RENDER_LIMIT
    assert bullets[0] == "* 1단계 정비"


def test_the_same_action_keeps_one_id_whichever_plan_section_carries_it():
    """Identity must survive a report moving an action into the structured list.

    A new id would drop the carried approve/reject status (merge_suggestion_status
    keys on id) and mint a new Jira-side ARID marker, so re-approving the same
    work would create a second issue instead of matching the first.
    """
    action = "Harden Jira approval persistence"
    commits = [{"sha": "e5e5e5e", "subject": "fix(proxy): approval persistence"}]

    flat = build_create_task_proposals(
        commits, {"priority_actions": [action]}, default_project_key="APPL"
    )[0]
    structured = build_create_task_proposals(
        commits,
        {"tasks": [{"summary": action, "purpose": "승인 상태를 영속화한다."}]},
        default_project_key="APPL",
    )[0]

    assert flat["id"] == structured["id"]
    assert flat["dedupe_marker"] == structured["dedupe_marker"]
    # The section itself is still reported, just not part of the identity.
    assert flat["plan_source"] == "priority_actions"
    assert structured["plan_source"] == "tasks"
