"""Tests for startup_projects.json structure and content."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECTS_FILE = Path(__file__).resolve().parent.parent / "scripts" / "startup_projects.json"


@pytest.fixture(scope="module")
def projects_data():
    assert PROJECTS_FILE.exists(), f"startup_projects.json not found at {PROJECTS_FILE}"
    return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))


class TestStartupProjectsStructure:
    def test_top_level_has_projects_key(self, projects_data):
        assert "projects" in projects_data

    def test_projects_is_list(self, projects_data):
        assert isinstance(projects_data["projects"], list)

    def test_projects_not_empty(self, projects_data):
        assert len(projects_data["projects"]) > 0

    def test_each_project_has_required_fields(self, projects_data):
        required = {"name", "path", "profile", "enabled"}
        for proj in projects_data["projects"]:
            missing = required - set(proj.keys())
            assert not missing, f"Project {proj.get('name', '?')} missing fields: {missing}"

    def test_name_is_nonempty_string(self, projects_data):
        for proj in projects_data["projects"]:
            assert isinstance(proj["name"], str) and proj["name"].strip()

    def test_path_is_nonempty_string(self, projects_data):
        for proj in projects_data["projects"]:
            assert isinstance(proj["path"], str) and proj["path"].strip()

    def test_enabled_is_bool(self, projects_data):
        for proj in projects_data["projects"]:
            assert isinstance(proj["enabled"], bool)

    def test_unique_names(self, projects_data):
        names = [p["name"] for p in projects_data["projects"]]
        assert len(names) == len(set(names)), f"Duplicate names: {names}"
