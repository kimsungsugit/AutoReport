"""Task provider abstraction for sprint task data.

Supports two backends:
  - JsonFileTaskProvider: reads sprint_tasks.json (default, no external deps)
  - JiraApiTaskProvider:  fetches from Jira Server REST API (requires PAT)

Usage:
    provider = get_task_provider()
    tasks = provider.get_tasks()
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class TaskProvider(ABC):
    """Abstract base for sprint task data sources."""

    @abstractmethod
    def get_tasks(self) -> dict[str, Any]:
        """Return sprint data in sprint_tasks.json format:
        {
            "sprint": {"name": str, "start": str, "end": str},
            "tasks": [{"key": str, "title": str, ...}]
        }
        """

    @abstractmethod
    def update_subtask_status(self, task_key: str, subtask_title: str, status: str) -> bool:
        """Update a subtask's status. Returns True on success."""


class JsonFileTaskProvider(TaskProvider):
    """Reads/writes sprint tasks from a local JSON file."""

    def __init__(self, path: Path | str | None = None):
        if path is None:
            path = Path(__file__).resolve().parents[1] / "scripts" / "sprint_tasks.json"
        self.path = Path(path)

    def get_tasks(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def update_subtask_status(self, task_key: str, subtask_title: str, status: str) -> bool:
        data = self.get_tasks()
        for task in data.get("tasks", []):
            if task["key"] == task_key:
                for st in task.get("subtasks", []):
                    if st["title"] == subtask_title:
                        st["status"] = status
                        with open(self.path, "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        return True
        return False


class JiraApiTaskProvider(TaskProvider):
    """Fetches sprint tasks from Jira Server REST API.

    Requires JIRA_URL and JIRA_TOKEN environment variables.
    Falls back to JsonFileTaskProvider if connection fails.
    """

    def __init__(self, base_url: str, token: str, project_key: str = "APPL"):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.project_key = project_key
        self._fallback = JsonFileTaskProvider()

    def get_tasks(self) -> dict[str, Any]:
        try:
            import urllib.request
            url = f"{self.base_url}/rest/api/2/search?jql=project={self.project_key}+AND+type=Task&fields=summary,subtasks,status,customfield_10015,customfield_10016"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return self._convert_jira_response(data)
        except Exception:
            return self._fallback.get_tasks()

    def update_subtask_status(self, task_key: str, subtask_title: str, status: str) -> bool:
        # Jira API transition 구현 시 여기에 REST call 추가
        return self._fallback.update_subtask_status(task_key, subtask_title, status)

    def _convert_jira_response(self, data: dict) -> dict[str, Any]:
        """Convert Jira REST API response to sprint_tasks.json format."""
        tasks = []
        for issue in data.get("issues", []):
            fields = issue.get("fields", {})
            tasks.append({
                "key": issue["key"],
                "title": fields.get("summary", ""),
                "start": str(fields.get("customfield_10015", "")),
                "end": str(fields.get("customfield_10016", "")),
                "subtasks": [
                    {
                        "title": st["fields"]["summary"],
                        "description": "",
                        "status": "done" if st["fields"]["status"]["name"] == "Done" else "in_progress" if st["fields"]["status"]["name"] == "In Progress" else "pending",
                    }
                    for st in fields.get("subtasks", [])
                ],
                "keywords": [],
            })
        return {"sprint": {"name": "", "start": "", "end": ""}, "tasks": tasks}


def get_task_provider() -> TaskProvider:
    """Factory: returns JiraApiTaskProvider if configured, else JsonFileTaskProvider."""
    import os
    jira_url = os.environ.get("JIRA_URL", "")
    jira_token = os.environ.get("JIRA_TOKEN", "")
    if jira_url and jira_token:
        return JiraApiTaskProvider(jira_url, jira_token)
    return JsonFileTaskProvider()
