from pathlib import Path

from scripts.generate_multi_project_reports import merge_task_plan, parse_next_plan


def test_parse_next_plan_uses_future_work_sections(tmp_path: Path):
    path = tmp_path / "next-plan.md"
    path.write_text(
        """# Plan

## 계획 요약

- 커밋 기반 Jira 자동화 안정화
- 중복 없이 계획을 이어서 수행

## 우선 작업

- outbox 통합
- hook 검증

## 중기 작업

- 운영 모니터링

## 리스크

- Jira 응답 불확실성
""",
        encoding="utf-8",
    )

    parsed = parse_next_plan(path)

    assert parsed["task_name"] == "커밋 기반 Jira 자동화 안정화"
    assert parsed["task_goal"] == "중복 없이 계획을 이어서 수행"
    assert parsed["remaining"] == ["outbox 통합", "hook 검증", "운영 모니터링"]
    assert parsed["risks"] == ["Jira 응답 불확실성"]


def test_merge_task_plan_keeps_jira_execution_but_uses_next_plan_work():
    jira = {
        "task_name": "stale Jira title",
        "remaining": ["stale Jira remaining"],
        "completed": ["APPL-1 완료"],
        "validation": ["pytest passed"],
    }
    next_plan = {
        "task_name": "current next plan",
        "task_goal": "ship safely",
        "task_scope": ["workflow"],
        "remaining": ["apply service"],
    }

    merged = merge_task_plan(next_plan, jira)

    assert merged["task_name"] == "current next plan"
    assert merged["remaining"] == ["apply service"]
    assert merged["completed"] == ["APPL-1 완료"]
    assert merged["validation"] == ["pytest passed"]
