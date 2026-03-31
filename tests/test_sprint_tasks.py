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
    _keyword_pattern,
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
