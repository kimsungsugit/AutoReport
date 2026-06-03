"""Tests for sprint_tasks loading, matching, and fallback Jira doc generation."""
from __future__ import annotations

import json
import textwrap
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.generate_periodic_reports import (
    load_sprint_tasks,
    match_commits_to_tasks,
    build_fallback_jira_doc,
    build_fallback_sections,
    _build_sprint_summary,
    _render_sprint_summary,
    _keyword_pattern,
    _scope_tasks_to_epic,
    _log_swallowed,
    generate_jira_suggestions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_SPRINT_DATA = {
    "sprint": {"name": "Test Sprint", "start": "2026-04-01", "end": "2026-04-30"},
    "tasks": [
        {
            "key": "APPL-100",
            "title": "CI Pipeline",
            "start": "2026-04-01",
            "end": "2026-04-10",
            "subtasks": [{"title": "Setup CI", "description": "Configure pipeline"}],
            "keywords": ["ci", "pipeline", "github-actions"],
        },
        {
            "key": "APPL-101",
            "title": "Dashboard UI",
            "start": "2026-04-05",
            "end": "2026-04-15",
            "subtasks": [{"title": "Layout", "description": "Build layout"}],
            "keywords": ["dashboard", "html", "css", "UI"],
        },
        {
            "key": "APPL-102",
            "title": "Future Task",
            "start": "2026-04-20",
            "end": "2026-04-30",
            "subtasks": [],
            "keywords": ["deploy", "release"],
        },
    ],
}


def make_commits(*subjects: str) -> list[dict[str, str]]:
    return [{"subject": s} for s in subjects]


# ---------------------------------------------------------------------------
# load_sprint_tasks
# ---------------------------------------------------------------------------

class TestLoadSprintTasks:
    def test_loads_existing_file(self):
        """The real sprint_tasks.json should load successfully."""
        result = load_sprint_tasks()
        assert isinstance(result, dict)
        # If the file exists, it should have tasks
        if result:
            assert "tasks" in result

    def test_returns_empty_on_provider_failure(self, tmp_path):
        """When both TaskProvider and direct fallback fail, returns empty dict."""
        import scripts.generate_periodic_reports as mod

        # Point both REPO_ROOT (for TaskProvider) and __file__ (for direct fallback)
        # to nonexistent paths so neither can load sprint_tasks.json
        with patch.object(mod, "REPO_ROOT", Path("/nonexistent")), \
             patch.object(mod, "__file__", str(tmp_path / "fake.py")):
            result = mod.load_sprint_tasks()
        assert result == {}


# ---------------------------------------------------------------------------
# _keyword_pattern (word-boundary matching)
# ---------------------------------------------------------------------------

class TestKeywordPattern:
    def test_short_keyword_requires_boundary(self):
        pat = _keyword_pattern("ci")
        assert pat.search("setup ci pipeline")
        assert pat.search("CI/CD works")
        assert not pat.search("specification")  # 'ci' inside a word
        assert not pat.search("ancient code")

    def test_hyphenated_keyword(self):
        pat = _keyword_pattern("github-actions")
        assert pat.search("use github-actions for CI")
        assert pat.search("use github actions for CI")
        assert pat.search("github_actions config")

    def test_underscore_keyword(self):
        pat = _keyword_pattern("design_system")
        assert pat.search("update design_system.py")
        assert pat.search("update design system module")

    def test_longer_keyword_no_false_positive(self):
        pat = _keyword_pattern("test")
        assert pat.search("add test file")
        assert not pat.search("attestation doc")  # 'test' inside word
        assert pat.search("test_runner.py")

    def test_fix_keyword_boundary(self):
        pat = _keyword_pattern("fix")
        assert pat.search("fix: resolve bug")
        assert not pat.search("prefix something")


# ---------------------------------------------------------------------------
# match_commits_to_tasks
# ---------------------------------------------------------------------------

class TestMatchCommitsToTasks:
    def test_empty_tasks(self):
        result = match_commits_to_tasks([], [], {}, date(2026, 4, 5))
        assert result == []

    def test_empty_sprint_data(self):
        result = match_commits_to_tasks(
            make_commits("fix ci"), ["pipeline.yml"], {}, date(2026, 4, 5)
        )
        assert result == []

    def test_status_in_progress(self):
        result = match_commits_to_tasks(
            make_commits("setup ci pipeline"),
            [],
            SAMPLE_SPRINT_DATA,
            date(2026, 4, 5),
        )
        ci_task = next(t for t in result if t["key"] == "APPL-100")
        assert ci_task["status"] == "진행 중"

    def test_status_upcoming(self):
        result = match_commits_to_tasks(
            make_commits("nothing relevant"),
            [],
            SAMPLE_SPRINT_DATA,
            date(2026, 4, 5),
        )
        future = next(t for t in result if t["key"] == "APPL-102")
        assert future["status"] == "예정"

    def test_status_completed(self):
        result = match_commits_to_tasks(
            [],
            [],
            SAMPLE_SPRINT_DATA,
            date(2026, 4, 25),
        )
        ci_task = next(t for t in result if t["key"] == "APPL-100")
        assert ci_task["status"] == "완료"

    def test_keyword_matching_counts(self):
        result = match_commits_to_tasks(
            make_commits("setup ci pipeline"),
            ["ci.yml"],
            SAMPLE_SPRINT_DATA,
            date(2026, 4, 5),
        )
        ci_task = next(t for t in result if t["key"] == "APPL-100")
        assert ci_task["hit_count"] >= 2  # 'ci' and 'pipeline'

    def test_related_commits_populated(self):
        result = match_commits_to_tasks(
            make_commits("update dashboard layout", "fix css issue"),
            [],
            SAMPLE_SPRINT_DATA,
            date(2026, 4, 8),
        )
        ui_task = next(t for t in result if t["key"] == "APPL-101")
        assert "update dashboard layout" in ui_task["related_commits"]
        assert "fix css issue" in ui_task["related_commits"]

    def test_sorted_by_hit_count_descending(self):
        result = match_commits_to_tasks(
            make_commits("ci pipeline github-actions"),
            [],
            SAMPLE_SPRINT_DATA,
            date(2026, 4, 5),
        )
        assert result[0]["key"] == "APPL-100"

    def test_bad_date_skips_task(self):
        bad_data = {
            "tasks": [
                {"key": "BAD-1", "title": "Bad", "start": "not-a-date", "end": "2026-04-10", "keywords": ["x"]},
                {"key": "GOOD-1", "title": "Good", "start": "2026-04-01", "end": "2026-04-10", "keywords": ["y"]},
            ]
        }
        result = match_commits_to_tasks([], [], bad_data, date(2026, 4, 5))
        assert len(result) == 1
        assert result[0]["key"] == "GOOD-1"

    def test_all_subtasks_done_marks_completed(self):
        """When all subtasks are done, status should be 완료 even within date range."""
        data = {
            "tasks": [
                {
                    "key": "APPL-200",
                    "title": "All Done",
                    "start": "2026-04-01",
                    "end": "2026-04-30",
                    "subtasks": [
                        {"title": "S1", "description": "d", "status": "done"},
                        {"title": "S2", "description": "d", "status": "done"},
                    ],
                    "keywords": ["test"],
                },
            ],
        }
        result = match_commits_to_tasks([], [], data, date(2026, 4, 10))
        assert result[0]["status"] == "완료"

    def test_partial_subtasks_done_stays_in_progress(self):
        """When some subtasks are done but not all, status stays 진행 중."""
        data = {
            "tasks": [
                {
                    "key": "APPL-201",
                    "title": "Partial",
                    "start": "2026-04-01",
                    "end": "2026-04-30",
                    "subtasks": [
                        {"title": "S1", "description": "d", "status": "done"},
                        {"title": "S2", "description": "d", "status": "in_progress"},
                    ],
                    "keywords": ["test"],
                },
            ],
        }
        result = match_commits_to_tasks([], [], data, date(2026, 4, 10))
        assert result[0]["status"] == "진행 중"

    def test_word_boundary_prevents_false_match(self):
        """'ci' keyword should NOT match 'specification' in file paths."""
        result = match_commits_to_tasks(
            make_commits("update specification doc"),
            ["specification.md"],
            SAMPLE_SPRINT_DATA,
            date(2026, 4, 5),
        )
        ci_task = next(t for t in result if t["key"] == "APPL-100")
        assert ci_task["hit_count"] == 0
        assert ci_task["related_commits"] == []

    # Regression: Iteration-1 fix — Jira 실제 status 우선 사용. 이전 동작은
    # 날짜만으로 재할당해서 end_date < today 인 in_progress 작업이 강제 "완료"
    # 가 되어 Rule 2 (기한 초과 완료 제안) 가 사실상 미발동.
    def test_jira_status_overrides_date_when_overdue(self):
        """task['status']='in_progress' + end_date < today → 진행 중 유지 (날짜 fallback 무시)."""
        data = {
            "tasks": [
                {
                    "key": "APPL-300",
                    "title": "Overdue WIP",
                    "start": "2026-04-01",
                    "end": "2026-04-05",
                    "status": "in_progress",
                    "subtasks": [],
                    "keywords": [],
                },
            ],
        }
        result = match_commits_to_tasks([], [], data, date(2026, 4, 22))
        assert result[0]["status"] == "진행 중"

    def test_jira_status_korean_mapping(self):
        """task['status'] 영문 값이 한국어로 매핑되는지."""
        data = {
            "tasks": [
                {"key": "K-1", "title": "T1", "start": "2026-04-01", "end": "2026-04-30",
                 "status": "done", "subtasks": [], "keywords": []},
                {"key": "K-2", "title": "T2", "start": "2026-04-01", "end": "2026-04-30",
                 "status": "in_progress", "subtasks": [], "keywords": []},
                {"key": "K-3", "title": "T3", "start": "2026-04-01", "end": "2026-04-30",
                 "status": "pending", "subtasks": [], "keywords": []},
            ],
        }
        result = match_commits_to_tasks([], [], data, date(2026, 4, 15))
        by_key = {t["key"]: t for t in result}
        assert by_key["K-1"]["status"] == "완료"
        assert by_key["K-2"]["status"] == "진행 중"
        assert by_key["K-3"]["status"] == "예정"

    def test_no_status_falls_back_to_date_logic(self):
        """status 필드 없으면 기존 날짜 기반 fallback 그대로 동작."""
        data = {
            "tasks": [
                {"key": "F-1", "title": "Future", "start": "2026-05-01", "end": "2026-05-30",
                 "subtasks": [], "keywords": []},
            ],
        }
        result = match_commits_to_tasks([], [], data, date(2026, 4, 15))
        assert result[0]["status"] == "예정"

    def test_preserves_epic_key(self):
        """epic_key/epic_summary 가 출력에 보존돼야 한다 (보드 그룹핑 + epic 격리 의존).

        이전엔 match 출력에서 누락 → payload sprint_tasks 가 전부 'No Epic' 으로
        뭉치고 공유 스프린트 격리가 불가능했다.
        """
        data = {
            "tasks": [
                {"key": "E-1", "title": "Task", "start": "2026-05-01", "end": "2026-05-30",
                 "subtasks": [], "keywords": [], "epic_key": "APPL-418",
                 "epic_summary": "사이버보안"},
            ],
        }
        result = match_commits_to_tasks([], [], data, date(2026, 5, 15))
        assert result[0]["epic_key"] == "APPL-418"
        assert result[0]["epic_summary"] == "사이버보안"

    def test_missing_epic_key_defaults_empty(self):
        """source 에 epic 정보 없으면 빈 문자열로 채워야 한다 (KeyError 금지)."""
        data = {"tasks": [{"key": "E-2", "title": "T", "start": "2026-05-01",
                           "end": "2026-05-30", "subtasks": [], "keywords": []}]}
        result = match_commits_to_tasks([], [], data, date(2026, 5, 15))
        assert result[0]["epic_key"] == ""
        assert result[0]["epic_summary"] == ""


# ---------------------------------------------------------------------------
# _scope_tasks_to_epic — 공유 스프린트 epic 격리 헬퍼
# ---------------------------------------------------------------------------

class TestLogSwallowed:
    """침묵 실패 가시화 헬퍼 — 삼켜진 예외를 stderr 로 흘려 scheduler.log 가 잡게."""

    def test_writes_context_and_exception_to_stderr(self, capsys):
        _log_swallowed("load_sprint_tasks/provider", ValueError("boom"))
        err = capsys.readouterr().err
        assert "load_sprint_tasks/provider" in err
        assert "ValueError" in err and "boom" in err

    def test_does_not_raise_on_weird_exception(self):
        # 헬퍼 자체가 절대 예외를 던지면 안 된다 (안전망이 안전망을 깨면 곤란).
        _log_swallowed("ctx", RuntimeError(""))


class TestScopeTasksToEpic:
    _TASKS = [
        {"key": "A-1", "epic_key": "APPL-373"},
        {"key": "A-2", "epic_key": "APPL-418"},
        {"key": "A-3", "epic_key": "APPL-373"},
    ]

    def test_filters_to_configured_epic(self):
        out = _scope_tasks_to_epic(self._TASKS, "APPL-418")
        assert [t["key"] for t in out] == ["A-2"]

    def test_empty_scope_is_noop(self):
        out = _scope_tasks_to_epic(self._TASKS, "")
        assert [t["key"] for t in out] == ["A-1", "A-2", "A-3"]

    def test_no_epic_data_is_noop(self):
        """epic_key 가 전혀 없는 데이터(로컬 fallback)는 wipe 하지 않고 그대로 둔다."""
        tasks = [{"key": "X-1"}, {"key": "X-2", "epic_key": ""}]
        out = _scope_tasks_to_epic(tasks, "APPL-418")
        assert [t["key"] for t in out] == ["X-1", "X-2"]

    def test_no_match_returns_empty(self):
        """epic_key 데이터는 있으나 scope 와 하나도 안 맞으면 빈 리스트."""
        out = _scope_tasks_to_epic(self._TASKS, "APPL-999")
        assert out == []


# ---------------------------------------------------------------------------
# build_fallback_jira_doc
# ---------------------------------------------------------------------------

class TestBuildFallbackJiraDoc:
    def _make_payload(self, sprint_tasks=None):
        return {
            "top_areas": [{"area": "scripts", "count": 5}],
            "recent_commits": [{"subject": "fix pipeline"}],
            "work_type": "feature",
            "source_insights": ["Insight 1"],
            "sprint_tasks": sprint_tasks or [],
            "today": "2026-04-05",
            "repository": "TestRepo",
            "uncommitted_count": 0,
            "github": {"commits": []},
        }

    def test_empty_sprint_tasks(self):
        doc = build_fallback_jira_doc("jira", self._make_payload())
        assert doc["completed"]
        assert doc["in_progress"]
        assert doc["remaining"]

    def test_with_tasks_classifies_correctly(self):
        tasks = [
            {"key": "A-1", "title": "Done task", "status": "완료", "hit_count": 2,
             "start": "2026-04-01", "end": "2026-04-03",
             "subtasks": [{"title": "Sub", "description": "Desc"}],
             "related_commits": ["commit 1"]},
            {"key": "A-2", "title": "Active task", "status": "진행 중", "hit_count": 1,
             "start": "2026-04-01", "end": "2026-04-10",
             "subtasks": [], "related_commits": []},
            {"key": "A-3", "title": "Future task", "status": "예정", "hit_count": 0,
             "start": "2026-04-20", "end": "2026-04-30",
             "subtasks": [], "related_commits": []},
        ]
        doc = build_fallback_jira_doc("jira", self._make_payload(tasks))
        assert any("A-1" in c for c in doc["completed"])
        assert any("A-2" in c for c in doc["in_progress"])
        assert any("A-3" in c for c in doc["remaining"])

    def test_task_board_structure(self):
        tasks = [
            {"key": "A-1", "title": "Task", "status": "진행 중", "hit_count": 1,
             "start": "2026-04-01", "end": "2026-04-10",
             "subtasks": [{"title": "Sub1", "description": "Desc1"}],
             "related_commits": ["commit msg"]},
        ]
        doc = build_fallback_jira_doc("jira", self._make_payload(tasks))
        board = doc["task_board"]
        assert len(board) == 1
        assert board[0]["key"] == "A-1"
        assert board[0]["status"] == "진행 중"
        assert "2026-04-01" in board[0]["period"]
        assert len(board[0]["subtasks"]) == 1

    def test_status_summary_counts(self):
        tasks = [
            {"key": "A-1", "title": "T1", "status": "완료", "hit_count": 1,
             "start": "2026-04-01", "end": "2026-04-03",
             "subtasks": [], "related_commits": ["c1"]},
            {"key": "A-2", "title": "T2", "status": "진행 중", "hit_count": 1,
             "start": "2026-04-01", "end": "2026-04-10",
             "subtasks": [], "related_commits": []},
        ]
        doc = build_fallback_jira_doc("jira", self._make_payload(tasks))
        assert doc["status_summary"]["completed_count"] == 1
        assert doc["status_summary"]["in_progress_count"] == 1

    def test_fallback_when_no_completed_or_in_progress(self):
        """When all tasks are '예정' with 0 hits, completed falls back to commits."""
        tasks = [
            {"key": "A-1", "title": "Future", "status": "예정", "hit_count": 0,
             "start": "2026-04-20", "end": "2026-04-30",
             "subtasks": [], "related_commits": []},
        ]
        doc = build_fallback_jira_doc("jira", self._make_payload(tasks))
        # Should fall back to commit subjects
        assert "fix pipeline" in doc["completed"]

    def test_completed_task_with_done_subtasks(self):
        """Completed tasks should include done subtask details."""
        tasks = [
            {"key": "A-1", "title": "Done task", "status": "완료", "hit_count": 1,
             "start": "2026-04-01", "end": "2026-04-03",
             "subtasks": [
                 {"title": "Sub1", "description": "Desc1", "status": "done"},
                 {"title": "Sub2", "description": "Desc2", "status": "done"},
             ],
             "related_commits": ["commit 1"]},
        ]
        doc = build_fallback_jira_doc("jira", self._make_payload(tasks))
        # Should have the main entry plus subtask detail lines
        assert any("A-1" in c for c in doc["completed"])
        assert any("Sub1" in c for c in doc["completed"])
        assert any("Sub2" in c for c in doc["completed"])

    def test_in_progress_task_with_zero_hits_goes_to_remaining(self):
        """진행 중 task with 0 hit_count should be classified as remaining."""
        tasks = [
            {"key": "A-1", "title": "No hits", "status": "진행 중", "hit_count": 0,
             "start": "2026-04-01", "end": "2026-04-10",
             "subtasks": [], "related_commits": []},
        ]
        doc = build_fallback_jira_doc("jira", self._make_payload(tasks))
        assert any("A-1" in c for c in doc["remaining"])
        assert not any("A-1" in c for c in doc["in_progress"])


# ---------------------------------------------------------------------------
# _build_sprint_summary
# ---------------------------------------------------------------------------

class TestBuildSprintSummary:
    def _make_payload(self, sprint_tasks=None):
        return {
            "sprint_tasks": sprint_tasks or [],
        }

    def test_empty_sprint_tasks(self):
        result = _build_sprint_summary(self._make_payload())
        assert result == []

    def test_done_subtasks_in_completion_details(self):
        tasks = [
            {"key": "A-1", "title": "Task1", "status": "완료",
             "subtask_progress": "2/2",
             "subtasks": [
                 {"title": "S1", "description": "D1", "status": "done"},
                 {"title": "S2", "description": "D2", "status": "done"},
             ],
             "related_commits": ["c1"]},
        ]
        result = _build_sprint_summary(self._make_payload(tasks))
        assert len(result) == 1
        assert result[0]["status"] == "완료"
        assert len(result[0]["completion_details"]) == 2
        assert "S1: D1" in result[0]["completion_details"]

    def test_in_progress_subtasks_tracked(self):
        tasks = [
            {"key": "A-2", "title": "Task2", "status": "진행 중",
             "subtask_progress": "1/2",
             "subtasks": [
                 {"title": "S1", "description": "D1", "status": "done"},
                 {"title": "S2", "description": "D2", "status": "in_progress"},
             ],
             "related_commits": []},
        ]
        result = _build_sprint_summary(self._make_payload(tasks))
        assert result[0]["in_progress_details"] == ["S2"]

    def test_subtask_without_status_ignored(self):
        """Subtasks missing 'status' key should not crash and not count as done."""
        tasks = [
            {"key": "A-3", "title": "Task3", "status": "진행 중",
             "subtask_progress": "0/1",
             "subtasks": [{"title": "S1", "description": "D1"}],
             "related_commits": []},
        ]
        result = _build_sprint_summary(self._make_payload(tasks))
        assert result[0]["completion_details"] == []
        assert result[0]["in_progress_details"] == []


# ---------------------------------------------------------------------------
# _render_sprint_summary
# ---------------------------------------------------------------------------

class TestRenderSprintSummary:
    def test_empty_sprint_summary_no_output(self):
        lines = []
        _render_sprint_summary(lines, {}, "주간")
        assert lines == []

    def test_completed_tasks_rendered(self):
        sections = {
            "sprint_summary": [
                {"key": "A-1", "title": "Done", "status": "완료",
                 "subtask_progress": "2/2",
                 "completion_details": ["S1: D1"],
                 "in_progress_details": [],
                 "related_commits": ["c1"]},
            ],
        }
        lines = []
        _render_sprint_summary(lines, sections, "주간")
        text = "\n".join(lines)
        assert "주간 스프린트 작업 현황" in text
        assert "완료된 작업 (1건)" in text
        assert "[A-1] Done" in text
        assert "S1: D1" in text

    def test_in_progress_tasks_rendered(self):
        sections = {
            "sprint_summary": [
                {"key": "A-2", "title": "Active", "status": "진행 중",
                 "subtask_progress": "1/2",
                 "completion_details": ["S1: D1"],
                 "in_progress_details": ["S2"],
                 "related_commits": []},
            ],
        }
        lines = []
        _render_sprint_summary(lines, sections, "월간")
        text = "\n".join(lines)
        assert "진행 중인 작업 (1건)" in text
        assert "완료된 하위작업" in text
        assert "S2" in text


# ---------------------------------------------------------------------------
# build_fallback_sections (weekly/monthly sprint_summary)
# ---------------------------------------------------------------------------

class TestBuildFallbackSectionsSprintSummary:
    def _make_payload(self, sprint_tasks=None):
        return {
            "today": "2026-04-05",
            "window_start": "2026-03-30",
            "window_end": "2026-04-05",
            "recent_commits": [{"subject": "fix ci", "author": "dev", "time": "2026-04-05"}],
            "top_areas": [{"area": "scripts", "count": 3}],
            "uncommitted_count": 0,
            "work_type": "feature",
            "commit_count": 1,
            "changed_file_count": 3,
            "diff_summary": {"total_added": 10, "total_deleted": 2, "top_files": []},
            "source_insights": [],
            "sprint_tasks": sprint_tasks or [],
            "repository": "TestRepo",
            "branch": "main",
            "remote_url": "https://example.com",
            "domain_profile_name": "test",
            "github": {},
        }

    def test_weekly_includes_sprint_summary(self):
        tasks = [
            {"key": "A-1", "title": "Task", "status": "완료",
             "subtask_progress": "1/1",
             "subtasks": [{"title": "S1", "description": "D1", "status": "done"}],
             "related_commits": []},
        ]
        result = build_fallback_sections("weekly", self._make_payload(tasks))
        assert "sprint_summary" in result
        assert len(result["sprint_summary"]) == 1
        assert result["sprint_summary"][0]["key"] == "A-1"

    def test_monthly_includes_sprint_summary(self):
        result = build_fallback_sections("monthly", self._make_payload())
        assert "sprint_summary" in result
        assert result["sprint_summary"] == []

    def test_daily_no_sprint_summary(self):
        result = build_fallback_sections("daily", self._make_payload())
        assert "sprint_summary" not in result


# ---------------------------------------------------------------------------
# generate_jira_suggestions — Iteration 1-4 회귀 방지
# ---------------------------------------------------------------------------

def _suggestion_payload(sprint_tasks, commits=None):
    """Minimal payload for generate_jira_suggestions / generate_document tests."""
    return {
        "today": "2026-05-22",
        "report_type": "jira",
        "window_start": "2026-05-22",
        "window_end": "2026-05-22",
        "repository": "test",
        "repo_root": "C:/nonexistent",  # so subprocess git log fails silently
        "domain_profile": "desktop_app",
        "domain_profile_name": "데스크톱",
        "domain_focus": [],
        "jira_enabled": True,
        "sprint_tasks": sprint_tasks,
        "recent_commits": [{"hash": "h", "subject": s, "author": "", "time": ""}
                           for s in (commits or [])],
        "changed_files": [], "uncommitted": [], "uncommitted_count": 0,
        # Fields render_jira_markdown / render_report_markdown read directly
        "branch": "main", "remote_url": "", "upstream": "",
        "sync_status": {"ahead": 0, "behind": 0},
        "commit_count": 0, "changed_file_count": 0,
        "work_type": "feature", "source_insights": [],
        "diff_summary": {}, "github": {}, "top_areas": [],
        "primary_change_facets": [], "supporting_change_facets": [],
        "change_facets": [], "auto_commit_status": {},
        "changed_docs": [],
    }


class TestGenerateJiraSuggestions:
    def test_empty_sprint_tasks_returns_empty(self):
        payload = _suggestion_payload([])
        # jira_enabled=False to skip live fetch fallback (which would also empty)
        payload["jira_enabled"] = False
        assert generate_jira_suggestions(payload, None) == []

    def test_rule2_end_date_today_message(self):
        """end_date == today → "종료일 도래" (days_over=0 분기).

        커밋 증거도 완료 부작업도 없으면 confidence 는 'low' (날짜만으로 완료를 high 로
        단정하지 않는다 — evidence-gate). 증거가 있을 때 high 인지는 아래 sibling 테스트.
        """
        sprint = [{
            "key": "T-1", "title": "Wraps today",
            "start": "2026-05-01", "end": "2026-05-22",
            "status": "in_progress", "subtasks": [],
        }]
        result = generate_jira_suggestions(_suggestion_payload(sprint), None)
        complete_for_t1 = [s for s in result if s["task_key"] == "T-1" and s["type"] == "complete"]
        assert complete_for_t1, "Rule 2 종료일 도래 제안이 발동해야 함"
        s = complete_for_t1[0]
        assert "종료일 도래" in s["title"]
        assert "0일" not in s["title"]
        assert s["confidence"] == "low"

    def test_rule2_confidence_high_with_commit_evidence(self):
        """Rule 2 는 커밋 증거가 있을 때만 high — 마감 도래 + 제목과 겹치는 커밋 → high."""
        sprint = [{
            "key": "T-1", "title": "Wraps today",
            "start": "2026-05-01", "end": "2026-05-22",
            "status": "in_progress", "subtasks": [],
        }]
        # "wraps"/"today" 가 제목과 겹쳐 _match_commits_for 가 증거를 찾는다
        commits = ["feat: Wraps today final fix"]
        result = generate_jira_suggestions(_suggestion_payload(sprint, commits), None)
        complete_for_t1 = [s for s in result if s["task_key"] == "T-1" and s["type"] == "complete"]
        assert complete_for_t1, "Rule 2 제안이 발동해야 함"
        assert complete_for_t1[0]["confidence"] == "high"
        assert "종료 요청합니다." in complete_for_t1[0]["suggested_text"]

    def test_rule2_overdue_message(self):
        """end_date < today → "기한 초과 N일" 분기."""
        sprint = [{
            "key": "T-2", "title": "Overdue",
            "start": "2026-05-01", "end": "2026-05-15",
            "status": "in_progress", "subtasks": [],
        }]
        result = generate_jira_suggestions(_suggestion_payload(sprint), None)
        complete_for_t2 = [s for s in result if s["task_key"] == "T-2" and s["type"] == "complete"]
        assert complete_for_t2
        assert "기한 초과" in complete_for_t2[0]["title"]
        assert "7일" in complete_for_t2[0]["title"]  # 2026-05-22 - 2026-05-15

    def test_rule2_skipped_for_pending(self):
        """pending 상태 task 는 end_date 와 무관하게 Rule 2 안 탐."""
        sprint = [{
            "key": "T-3", "title": "Not started",
            "start": "2026-05-01", "end": "2026-05-15",
            "status": "pending", "subtasks": [],
        }]
        result = generate_jira_suggestions(_suggestion_payload(sprint), None)
        completes = [s for s in result if s["task_key"] == "T-3" and s["type"] == "complete"]
        assert completes == []

    def test_noise_chore_auto_filtered(self):
        """chore(auto): snapshot 은 noise → add_subtask 제안 후보 아님."""
        sprint = [{
            "key": "T-4", "title": "Active work",
            "start": "2026-05-25", "end": "2026-05-30",  # future to skip Rule 2/3
            "status": "in_progress", "subtasks": [],
        }]
        commits = [
            "chore(auto): end-of-day snapshot 2026-05-21",
            "chore(auto): end-of-day snapshot 2026-05-20",
        ]
        result = generate_jira_suggestions(_suggestion_payload(sprint, commits), None)
        adds = [s for s in result if s["type"] == "add_subtask"]
        assert adds == [], "chore(auto) 는 noise 로 모두 차단되어야 함"

    def test_noise_chore_refinement_present(self):
        """_NOISE_PREFIXES 의 chore: 광범위 차단을 _NOISE_CHORE_BODY 로 정교화한 fix 가
        살아 있는지 source-level 회귀 표식 검증.
        """
        src = (Path(__file__).resolve().parents[1] / "scripts" / "generate_periodic_reports.py").read_text(encoding="utf-8")
        # Iteration 4 의 핵심: _NOISE_CHORE_BODY 변수가 정의되어 있어야 한다
        assert "_NOISE_CHORE_BODY" in src, "chore noise 정교화 변수가 존재해야 함"
        # 그 안의 noise 변종 키워드 확인
        assert '"bump "' in src and '"deps"' in src, \
            "_NOISE_CHORE_BODY 의 bump/deps 차단이 살아 있어야 함"

    def test_noise_chore_bump_filtered(self):
        """chore: bump version 은 _NOISE_CHORE_BODY 로 차단."""
        sprint = [{
            "key": "T-6", "title": "Active work",
            "start": "2026-05-25", "end": "2026-05-30",
            "status": "in_progress", "subtasks": [],
        }]
        commits = ["chore: bump version to 1.2.0"]
        result = generate_jira_suggestions(_suggestion_payload(sprint, commits), None)
        adds = [s for s in result if s["type"] == "add_subtask"]
        assert adds == [], "chore: bump 은 noise 변종으로 차단되어야 함"

    def test_best_parent_word_overlap(self):
        """unmatched commit 의 부모 선택은 commit subject 와 task title 단어 overlap 기반."""
        sprint = [
            {"key": "T-A", "title": "documentation cleanup",
             "start": "2026-05-25", "end": "2026-05-30",
             "status": "in_progress", "subtasks": []},
            {"key": "T-B", "title": "replay analysis system",
             "start": "2026-05-25", "end": "2026-05-30",
             "status": "in_progress", "subtasks": []},
        ]
        # "replay" 가 T-B 의 title 과 겹친다 → T-B 가 best_parent 여야 함
        commits = ["feat: replay analysis panel"]
        result = generate_jira_suggestions(_suggestion_payload(sprint, commits), None)
        adds = [s for s in result if s["type"] == "add_subtask"]
        assert adds, "add_subtask 제안이 있어야 함"
        # 첫 add_subtask 가 T-B 로 향해야 함 (단어 overlap: replay/analysis)
        assert adds[0]["task_key"] == "T-B"

    def test_dedup_identical_add_subtask(self):
        """동일 (task_key, type, suggested_text) add_subtask 카드는 1건으로 dedup.

        같은 커밋이 미매칭 루프 + 부모-title 매칭 루프 양쪽에서, 또는 동일 subject
        커밋이 중복 입력될 때 같은 카드가 여러 장 생기던 것을 막는다.
        """
        sprint = [{
            "key": "DUP-1", "title": "alpha",  # 커밋 토큰과 안 겹침
            "start": "2026-05-25", "end": "2099-12-31",  # 미래 → Rule 2 안 탐
            "status": "in_progress", "subtasks": [],
        }]
        # 동일 subject 커밋 2개 (무의미 토큰 → 어떤 keyword 와도 매칭 안 됨)
        commits = ["feat: zzqqxx wibwob", "feat: zzqqxx wibwob"]
        result = generate_jira_suggestions(_suggestion_payload(sprint, commits), None)
        adds = [s for s in result if s["type"] == "add_subtask" and s["task_key"] == "DUP-1"]
        sigs = {(s["task_key"], s["type"], s["suggested_text"]) for s in adds}
        assert len(adds) == len(sigs), f"중복 카드 발생: {[s['suggested_text'] for s in adds]}"
        assert len(adds) == 1, "동일 커밋 2개 → add_subtask 1건이어야 함"

    def test_confidence_sort_preserves_high_under_cap(self):
        """저신뢰 카드가 많아도 high-confidence 제안이 max_suggestions 컷에서 살아남아야 한다.

        per-subtask 루프는 cap 체크 없이 medium 카드를 쌓아서, 정렬 없이 자르면 뒤
        task 의 high Rule 2 제안이 잘려나갔다. confidence 정렬로 high 가 앞으로 온다.
        """
        big = {
            "key": "BIG", "title": "big task",
            "start": "2026-05-01", "end": "2099-12-31",  # 미래
            "status": "in_progress",
            "subtasks": [
                {"key": f"BIG-{i}", "title": f"sub {i}", "status": "in_progress"}
                for i in range(12)  # 12 medium 카드 → cap(10) 초과
            ],
        }
        deadline = {
            "key": "DEADLINE", "title": "overdue task",
            "start": "2026-05-01", "end": "2026-05-01",  # today 이전 → Rule 2
            "status": "in_progress", "subtasks": [],
        }
        # "overdue" 가 DEADLINE 제목과만 겹치는 커밋 → Rule 2 가 증거 기반으로 high.
        # (BIG 제목 "big task" 와는 안 겹쳐 BIG 으로는 안 샌다)
        payload = _suggestion_payload([big, deadline], ["feat: overdue cleanup"])  # 순서: BIG 먼저
        payload["today"] = "2026-06-01"
        result = generate_jira_suggestions(payload, None)
        assert len(result) == 10  # max_suggestions 기본값
        assert any(s["confidence"] == "high" and s["task_key"] == "DEADLINE"
                   for s in result), "high-confidence Rule 2 제안이 컷에서 살아남아야 함"
        # 정렬 결과: high 가 맨 앞
        assert result[0]["confidence"] == "high"

    def test_honors_payload_today_not_wall_clock(self):
        """제안은 payload['today'](리포트 날짜) 기준으로 종료일 도래를 판단해야 한다.

        end=2099-12-31 은 실제 오늘 기준 한참 미래라 Rule 2 가 안 터져야 정상인데,
        payload['today']=2100-01-01 로 백데이트(여기선 forward-date)하면 Rule 2 가
        발동한다 — wall-clock(date.today())이 아니라 payload 날짜를 쓴다는 증거.
        """
        sprint = [{
            "key": "FUT-1", "title": "Far future",
            "start": "2099-01-01", "end": "2099-12-31",
            "status": "in_progress", "subtasks": [],
        }]
        payload = _suggestion_payload(sprint)
        payload["today"] = "2100-01-01"
        result = generate_jira_suggestions(payload, None)
        completes = [s for s in result if s["task_key"] == "FUT-1" and s["type"] == "complete"]
        assert completes, "payload['today'] 기준 종료일 초과 → Rule 2 발동해야 함"
        assert "기한 초과" in completes[0]["title"]

    def test_shared_sprint_no_leak_without_epic_scope(self):
        """공유 스프린트(여러 에픽) + epic_scope 미설정 시: 단어 overlap 0 인 커밋은
        '다른 에픽' 작업에 새지 않고 억제된다 (예전엔 _active_parents[0]=OTH-1 로 누수).

        에픽이 모호할 땐 무작위 부모에 붙이느니 카드를 내지 않는 게 정밀도상 낫다.
        epic_scope 가 설정되면 자기 에픽 작업에 분류용으로 붙는다(아래 scoped 테스트).
        """
        sprint = [
            {"key": "OTH-1", "title": "사용자 피드백 및 개선",  # 다른 프로젝트(에픽 E-OTHER)
             "start": "2026-05-01", "end": "2026-05-30",
             "status": "in_progress", "subtasks": [], "epic_key": "E-OTHER"},
            {"key": "MINE-1", "title": "프로그램 통신 확장",     # 이 프로젝트(에픽 E-MINE)
             "start": "2026-05-01", "end": "2026-05-30",
             "status": "in_progress", "subtasks": [], "epic_key": "E-MINE"},
        ]
        commits = ["feat(tara): ISO 26262 HARA 데이터모델"]  # 두 title 과 단어 겹침 0
        result = generate_jira_suggestions(_suggestion_payload(sprint, commits), None)
        assert not any(s["task_key"] == "OTH-1" for s in result), "남의 에픽으로 누수 금지"
        adds = [s for s in result if s["type"] == "add_subtask"]
        assert adds == [], "에픽 모호 + 단어 겹침 0 커밋은 억제돼야 함"

    def test_shared_sprint_scoped_to_configured_epic(self):
        """공유 스프린트 격리: epic_scope 설정 시 제안이 그 에픽 작업으로만 한정되고
        다른 에픽으로 새지 않아야 한다 (CyberSecurity↔Release_claude APPL 공유 버그).
        """
        sprint = [
            {"key": "OTH-1", "title": "사용자 피드백 및 개선",
             "start": "2026-05-01", "end": "2026-05-30",
             "status": "in_progress", "subtasks": [], "epic_key": "E-OTHER"},
            {"key": "MINE-1", "title": "프로그램 통신 확장",
             "start": "2026-05-01", "end": "2026-05-30",
             "status": "in_progress", "subtasks": [], "epic_key": "E-MINE"},
        ]
        commits = ["feat(tara): ISO 26262 HARA 데이터모델"]
        payload = _suggestion_payload(sprint, commits)
        payload["epic_scope"] = "E-MINE"
        result = generate_jira_suggestions(payload, None)
        assert result, "에픽 내 제안이 있어야 함"
        # 모든 제안이 내 에픽(E-MINE)의 작업으로만 향해야 한다
        assert all(s["task_key"] == "MINE-1" for s in result), \
            f"제안이 다른 에픽으로 새면 안 됨: {[s['task_key'] for s in result]}"
        assert all(s.get("epic_key") == "E-MINE" for s in result)
        assert not any(s["task_key"] == "OTH-1" for s in result), "남의 에픽 작업 제안 금지"

    def test_shared_jira_projects_have_distinct_epic_key(self):
        """공유 APPL 스프린트의 프로젝트들은 startup_projects.json 에서 서로 다른
        epic_key 를 가져 격리돼야 한다 (config 누락 시 누수 재발).
        """
        sp = Path(__file__).resolve().parents[1] / "scripts" / "startup_projects.json"
        projects = json.loads(sp.read_text(encoding="utf-8")).get("projects", [])
        jira_projects = [p for p in projects if isinstance(p.get("jira"), dict)]
        # 같은 (project_key, sprint_id) 를 공유하는 프로젝트는 epic_key 가 모두 채워져야 함
        from collections import defaultdict
        groups = defaultdict(list)
        for p in jira_projects:
            j = p["jira"]
            groups[(j.get("project_key"), j.get("sprint_id"))].append(j.get("epic_key"))
        for sig, epics in groups.items():
            if len(epics) > 1:  # 공유 스프린트
                assert all(epics), f"{sig} 공유 프로젝트는 모두 epic_key 필요: {epics}"
                assert len(set(epics)) == len(epics), f"{sig} epic_key 중복: {epics}"

    def test_live_vocabulary_covers_matching_commit(self):
        """all_keywords 가 LIVE 태스크 제목 토큰을 포함 → 그 토큰을 가진 커밋은 '미매칭'
        low 카드를 만들면 안 된다 (이전엔 만료 캐시 어휘만 써서 라이브 매칭이 새로 noise).
        """
        sprint = [{
            "key": "LIVE-1", "title": "Zephyr telemetry buffer",
            "start": "2026-05-25", "end": "2099-12-31",  # 미래 → Rule 2 안 탐
            "status": "in_progress", "subtasks": [],
        }]
        # "zephyr"/"telemetry" 가 라이브 제목 토큰 → 커밋이 커버된 것으로 분류돼야 함
        commits = ["feat: zephyr telemetry flush"]
        result = generate_jira_suggestions(_suggestion_payload(sprint, commits), None)
        adds = [s for s in result if s["type"] == "add_subtask"]
        assert all(s["confidence"] != "low" for s in adds), \
            f"라이브 제목과 겹치는 커밋은 미매칭 low 카드를 만들면 안 됨: {[s['confidence'] for s in adds]}"

    def test_dedup_keys_on_source_commit_not_truncated_text(self):
        """서로 다른 커밋이 60자 prefix 를 공유해 suggested_text 가 같아도, 원본 커밋이
        다르면 2건으로 유지된다 (dedup 키가 truncate 텍스트가 아니라 _src_commit 기반).
        """
        prefix = "x" * 70  # _strip_cc_prefix 후 [:60] 이 동일해지는 긴 prefix
        sprint = [{
            "key": "SRC-1", "title": "alpha",  # 커밋 토큰과 안 겹침 → 둘 다 미매칭
            "start": "2026-05-25", "end": "2099-12-31",
            "status": "in_progress", "subtasks": [],
        }]
        commits = [f"feat: {prefix} AAA", f"feat: {prefix} BBB"]
        result = generate_jira_suggestions(_suggestion_payload(sprint, commits), None)
        adds = [s for s in result if s["type"] == "add_subtask" and s["task_key"] == "SRC-1"]
        texts = {s["suggested_text"] for s in adds}
        assert len(texts) == 1, "전제: 두 커밋의 suggested_text 가 60자 컷에서 동일해야 함"
        assert len(adds) == 2, "원본 커밋이 다르면 2건 유지돼야 함 (truncate 텍스트로 잘못 병합 금지)"

    def test_internal_src_commit_not_leaked_to_output(self):
        """dedup 내부용 _src_commit 필드는 반환 카드에 남으면 안 된다."""
        sprint = [{
            "key": "LEAK-1", "title": "alpha",
            "start": "2026-05-25", "end": "2099-12-31",
            "status": "in_progress", "subtasks": [],
        }]
        result = generate_jira_suggestions(_suggestion_payload(sprint, ["feat: zzqqxx wibwob"]), None)
        assert all("_src_commit" not in s for s in result), "_src_commit 은 내부 필드 — 노출 금지"

    def test_zero_overlap_attaches_when_single_epic(self):
        """에픽 모호성이 없으면(단일/무 에픽) 단어 겹침 0 커밋도 부모에 분류용으로 붙되
        confidence 는 low, reason 은 '부모 검토 필요' — 억제는 다중 에픽 미설정 한정.
        """
        sprint = [{
            "key": "SOLO-1", "title": "alpha",
            "start": "2026-05-01", "end": "2099-12-31",
            "status": "in_progress", "subtasks": [],
        }]
        commits = ["feat(tara): ISO 26262 위협분석"]  # title 'alpha' 와 겹침 0
        result = generate_jira_suggestions(_suggestion_payload(sprint, commits), None)
        adds = [s for s in result if s["type"] == "add_subtask"]
        assert adds, "단일 에픽이면 겹침 0 도 분류용으로 붙어야 함"
        assert adds[0]["task_key"] == "SOLO-1"
        assert adds[0]["confidence"] == "low"
        assert "부모 검토" in adds[0]["reason"]

    def test_evidence_score_tiebreaks_within_confidence_tier(self):
        """같은 medium tier 안에서 evidence_score 높은 카드가 먼저 정렬된다(삽입순서 무관).

        입력 순서는 B 먼저지만 A 커밋의 신규 단어가 더 많아 A 가 앞서야 한다.
        """
        sprint = [
            {"key": "TASK-B", "title": "xray yankee",
             "start": "2026-05-01", "end": "2099-12-31",
             "status": "in_progress", "subtasks": []},
            {"key": "TASK-A", "title": "alpha bravo",
             "start": "2026-05-01", "end": "2099-12-31",
             "status": "in_progress", "subtasks": []},
        ]
        commits = [
            "feat: xray yankee zulu",                        # B: 신규 단어 적음
            "feat: alpha bravo charlie delta echo foxtrot",  # A: 신규 단어 많음
        ]
        result = generate_jira_suggestions(_suggestion_payload(sprint, commits), None)
        adds = [s for s in result if s["type"] == "add_subtask"]
        a = next(s for s in adds if s["task_key"] == "TASK-A")
        b = next(s for s in adds if s["task_key"] == "TASK-B")
        assert a["evidence_score"] > b["evidence_score"]
        assert adds.index(a) < adds.index(b), "evidence_score 높은 카드가 먼저 와야 함"


# ---------------------------------------------------------------------------
# generate_document 의 jira fact_field 후처리 (환각 차단) — Iteration 1/2
# ---------------------------------------------------------------------------

class TestGenerateDocumentFactOverride:
    def test_jira_gemini_overrides_hallucinated_task_board(self):
        """jira+gemini 모드에서 LLM 환각 task_board 가 sprint_tasks 기반으로 덮어쓰여야 한다."""
        from unittest.mock import patch as _patch
        from scripts import generate_periodic_reports as g

        # 실제 sprint_tasks 는 APPL-373
        sprint = [{
            "key": "APPL-373", "title": "Real epic task",
            "start": "2026-05-01", "end": "2026-05-30",
            "status": "in_progress", "subtasks": [],
        }]
        payload = _suggestion_payload(sprint, [])
        payload["report_type"] = "jira"

        # Gemini 가 환각 task_board (APPL-101) 를 반환한다고 가정
        hallucinated = {
            "title": "Test", "summary": "x", "task_name": "n", "task_goal": "g",
            "scope": ["[APPL-101] hallucinated"],
            "completed": [], "in_progress": [], "remaining": [],
            "task_board": [{"key": "APPL-101", "title": "FAKE", "status": "진행 중",
                            "period": "2026-05-01 ~ 2026-05-30", "subtasks": [],
                            "related_commits": []}],
            "validation": [], "risks": [], "links": [],
            "status_summary": {"completed_count": 0, "in_progress_count": 1, "remaining_count": 0},
        }
        with _patch.object(g, "ask_gemini_for_sections", return_value=hallucinated), \
             _patch.object(g, "ask_gemini_for_team_analysis", return_value={}):
            _md, mode, sections = g.generate_document("jira", payload)

        assert mode == "gemini"
        # 환각 키 APPL-101 이 task_board 에서 제거되고 실제 APPL-373 으로 덮어써져야 함
        keys = [t.get("key") for t in sections.get("task_board") or []]
        assert "APPL-101" not in keys, "환각 키는 차단되어야 함"
        assert "APPL-373" in keys, "실제 sprint_tasks 의 키로 덮어써져야 함"

    def test_jira_gemini_empty_sprint_forces_placeholder(self):
        """sprint_tasks 비어 있고 mode=gemini 면 task_board/scope 가 placeholder 로 강제됨."""
        from unittest.mock import patch as _patch
        from scripts import generate_periodic_reports as g

        payload = _suggestion_payload([], [])
        payload["report_type"] = "jira"
        payload["jira_enabled"] = False  # sprint_tasks 빈 경로 강제

        hallucinated = {
            "title": "Test", "summary": "x", "task_name": "n", "task_goal": "g",
            "scope": ["[APPL-001] invented"],
            "completed": ["[APPL-001] invented complete"],
            "in_progress": [], "remaining": [],
            "task_board": [{"key": "APPL-001", "title": "FAKE", "status": "진행 중",
                            "period": "2026-05-01 ~ 2026-05-30", "subtasks": [],
                            "related_commits": []}],
            "validation": [], "risks": [], "links": [],
            "status_summary": {"completed_count": 1, "in_progress_count": 0, "remaining_count": 0},
        }
        with _patch.object(g, "ask_gemini_for_sections", return_value=hallucinated), \
             _patch.object(g, "ask_gemini_for_team_analysis", return_value={}):
            _md, mode, sections = g.generate_document("jira", payload)

        assert mode == "gemini"
        assert sections.get("task_board") == []
        assert "Jira 스프린트 미연동" in (sections.get("scope") or [""])[0]
        assert "Jira 스프린트 미연동" in (sections.get("completed") or [""])[0]
        # status_summary 도 0 으로 강제
        assert sections["status_summary"]["completed_count"] == 0


# ---------------------------------------------------------------------------
# Multi-project dashboard dedup — source-level 회귀 표식 (Iteration 2/3)
# ---------------------------------------------------------------------------

class TestDashboardDedupRegression:
    """렌더링 결과를 직접 만들지 않고 dedup 로직이 코드에 살아 있는지 검증."""

    def test_render_html_dashboard_has_seen_boards(self):
        src = (Path(__file__).resolve().parents[1] / "scripts" / "generate_periodic_reports.py").read_text(encoding="utf-8")
        # render_html_dashboard 안에 (project_key, sprint_id, board_id) seen set 이 있어야 함
        assert "seen_boards" in src
        assert "board_key in seen_boards" in src

    def test_multi_project_has_dedup_and_merged_suggestions(self):
        src = (Path(__file__).resolve().parents[1] / "scripts" / "generate_multi_project_reports.py").read_text(encoding="utf-8")
        # 보드 dedup + suggestion 통합 둘 다 살아 있어야 함
        assert "seen_boards" in src, "보드 dedup set 이 있어야 함"
        assert "merged_suggestions" in src, "suggestion 통합 변수가 있어야 함"


# ---------------------------------------------------------------------------
# GeminiAdapter timeout wiring (Iteration 5)
# ---------------------------------------------------------------------------

class TestGeminiAdapterTimeoutWiring:
    """GeminiAdapter.generate 의 timeout 인자가 SDK Client 로 전파되는지."""

    def test_timeout_passed_via_http_options(self):
        src = (Path(__file__).resolve().parents[1] / "workflow" / "llm_adapters.py").read_text(encoding="utf-8")
        # HttpOptions(timeout=...) 가 Client(...) 에 전달되어야 함
        assert "HttpOptions" in src
        assert "http_options=" in src
        # 초→ms 변환 코멘트 표식
        assert "timeout * 1000" in src or "int(timeout * 1000)" in src


# ---------------------------------------------------------------------------
# JiraApiTaskProvider keyword 자동 추출 (Iteration 3)
# ---------------------------------------------------------------------------

class TestDefaultKeywordsFromTitle:
    """workflow/task_provider.py 의 _default_keywords_from_title 회귀 방지."""

    def _kws(self, title):
        from workflow.task_provider import _default_keywords_from_title
        return [k["word"] for k in _default_keywords_from_title(title)]

    def test_empty_title_returns_empty(self):
        assert self._kws("") == []

    def test_korean_short_words_filtered(self):
        """한국어 2자 이하는 제외 (UI/연동 등도 짧으면 빠짐)."""
        kws = self._kws("UI 연동 분석")
        # UI 영문 2자 제외, 연동 한글 2자 제외, 분석 skip-list 에 있음
        assert kws == []

    def test_korean_long_words_extracted(self):
        kws = self._kws("대시보드 컴포넌트 구현")
        assert "대시보드" in kws
        assert "컴포넌트" in kws
        # "구현" skip list 로 제외
        assert "구현" not in kws

    def test_english_short_words_filtered(self):
        """영문 3자 이하 + ci/qa 류 제외."""
        kws = self._kws("ci qa for the app")
        # ci, qa, for, the 모두 영문 4자 미만 / skip list → 제외
        assert kws == []

    def test_english_long_words_extracted(self):
        kws = self._kws("GitLab pipeline orchestration")
        # 모두 영문 4자 이상이고 skip list 에 없음
        assert "gitlab" in kws
        assert "pipeline" in kws
        assert "orchestration" in kws

    def test_skip_list_filters_generic(self):
        kws = self._kws("프로젝트 시스템 기능 관리")
        # 모두 _DEFAULT_KEYWORD_SKIP 에 등재된 generic 한국어 → 모두 제외
        assert kws == []

    def test_dedup_within_title(self):
        kws = self._kws("대시보드 대시보드 컴포넌트")
        # 동일 단어 한 번만 등장
        assert kws.count("대시보드") == 1

    def test_cap_at_eight(self):
        """최대 8개로 cap — title 이 길어도 제안 spam 방지."""
        title = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
        kws = self._kws(title)
        assert len(kws) <= 8

    def test_split_on_punctuation(self):
        """슬래시/콤마/하이픈/괄호 등 구분자로 split."""
        kws = self._kws("정적,동적/분석-결과(자동)")
        # 한국어 2자 단어들이 다 제외되더라도 split 자체는 동작해야 함
        # → 결과는 [] 일 수 있지만 예외 발생하면 안 됨
        assert isinstance(kws, list)
