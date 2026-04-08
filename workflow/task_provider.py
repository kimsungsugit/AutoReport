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
import ssl
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

    def add_comment(self, issue_key: str, comment: str) -> bool:
        """Add a comment to an issue. Returns True on success."""
        return False

    def transition_issue(self, issue_key: str, status: str, comment: str = "") -> bool:
        """Transition issue to a new status with optional comment. Returns True on success."""
        return False

    def complete_issue(self, issue_key: str, comment: str = "") -> bool:
        """Convenience: transition to '종료 요청' and add comment. Returns True on success."""
        return False


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

    # Jira workflow transitions: 할 일→진행 중(11), 진행 중→종료 요청(21), 종료 요청→할 일(61)
    TRANSITIONS = {
        "진행 중": "11",
        "종료 요청": "21",
        "할 일": "61",
    }

    def __init__(self, base_url: str, token: str, project_key: str = "APPL",
                 sprint_id: str | int | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.project_key = project_key
        self.sprint_id = sprint_id
        self._fallback = JsonFileTaskProvider()
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Make an authenticated request to Jira REST API."""
        import urllib.request
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15, context=self._ssl_ctx) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

    def get_tasks(self) -> dict[str, Any]:
        try:
            if self.sprint_id:
                data = self._request("GET",
                    f"/rest/agile/1.0/sprint/{self.sprint_id}/issue"
                    f"?maxResults=100&fields=summary,status,issuetype,subtasks,"
                    f"customfield_10230,customfield_10900")
                sprint_info = self._request("GET",
                    f"/rest/agile/1.0/sprint/{self.sprint_id}")
                return self._convert_jira_response(data, sprint_info)
            else:
                data = self._request("GET",
                    f"/rest/api/2/search?jql=project={self.project_key}"
                    f"+AND+type=Task&fields=summary,subtasks,status,"
                    f"customfield_10230,customfield_10900")
                return self._convert_jira_response(data)
        except Exception:
            return self._fallback.get_tasks()

    def update_subtask_status(self, task_key: str, subtask_title: str, status: str) -> bool:
        return self._fallback.update_subtask_status(task_key, subtask_title, status)

    def add_comment(self, issue_key: str, comment: str) -> bool:
        """Add a comment to a Jira issue."""
        try:
            self._request("POST", f"/rest/api/2/issue/{issue_key}/comment", {
                "body": comment,
            })
            return True
        except Exception:
            return False

    def transition_issue(self, issue_key: str, status: str, comment: str = "") -> bool:
        """Transition a Jira issue to the given status name."""
        transition_id = self.TRANSITIONS.get(status)
        if not transition_id:
            return False
        try:
            payload: dict[str, Any] = {
                "transition": {"id": transition_id},
            }
            if comment:
                payload["update"] = {
                    "comment": [{"add": {"body": comment}}],
                }
            self._request("POST", f"/rest/api/2/issue/{issue_key}/transitions", payload)
            return True
        except Exception:
            return False

    def complete_issue(self, issue_key: str, comment: str = "") -> bool:
        """Mark issue as '종료 요청' with a completion comment."""
        if comment:
            return self.transition_issue(issue_key, "종료 요청", comment)
        return self.transition_issue(issue_key, "종료 요청")

    def _convert_jira_response(self, data: dict, sprint_info: dict | None = None) -> dict[str, Any]:
        """Convert Jira REST API response to sprint_tasks.json format."""
        tasks = []
        seen_keys = set()
        for issue in data.get("issues", []):
            key = issue["key"]
            if key in seen_keys:
                continue
            fields = issue.get("fields", {})
            # Only include parent tasks (작업/큰틀), skip subtasks listed at top level
            if fields.get("issuetype", {}).get("name") in ("부작업", "Sub-task"):
                continue
            seen_keys.add(key)

            status_name = fields.get("status", {}).get("name", "")
            status_map = {"완료": "done", "종료 요청": "done", "진행 중": "in_progress"}
            tasks.append({
                "key": key,
                "title": fields.get("summary", ""),
                "status": status_map.get(status_name, "pending"),
                "start": str(fields.get("customfield_10230", "") or "")[:10],
                "end": str(fields.get("customfield_10900", "") or "")[:10],
                "subtasks": [
                    {
                        "title": st["fields"]["summary"],
                        "description": "",
                        "status": (
                            "done" if st["fields"]["status"]["name"] in ("완료", "종료 요청")
                            else "in_progress" if st["fields"]["status"]["name"] == "진행 중"
                            else "pending"
                        ),
                        "key": st.get("key", ""),
                    }
                    for st in fields.get("subtasks", [])
                ],
                "keywords": [],
            })

        sprint = {"name": "", "start": "", "end": ""}
        if sprint_info:
            sprint = {
                "name": sprint_info.get("name", ""),
                "start": str(sprint_info.get("startDate", ""))[:10],
                "end": str(sprint_info.get("endDate", ""))[:10],
            }
        return {"sprint": sprint, "tasks": tasks}


def get_task_provider(project_config: dict | None = None) -> TaskProvider:
    """Factory: returns JiraApiTaskProvider if configured, else JsonFileTaskProvider.

    Args:
        project_config: optional project dict from startup_projects.json
                        with jira.project_key, jira.sprint_id fields.
    """
    import os
    jira_url = os.environ.get("JIRA_URL", "")
    jira_token = os.environ.get("JIRA_TOKEN", "")
    if jira_url and jira_token:
        project_key = "APPL"
        sprint_id = None
        if project_config and isinstance(project_config.get("jira"), dict):
            jira_cfg = project_config["jira"]
            project_key = jira_cfg.get("project_key", project_key)
            sprint_id = jira_cfg.get("sprint_id")
        return JiraApiTaskProvider(jira_url, jira_token, project_key, sprint_id)
    return JsonFileTaskProvider()
