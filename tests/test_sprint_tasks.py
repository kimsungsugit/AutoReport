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
        """end_date == today → "종료일 도래" (days_over=0 분기)."""
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
        assert s["confidence"] == "high"

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
