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

    def update_description(self, issue_key: str, description: str) -> bool:
        """Replace the description (설명) of an issue. Returns True on success."""
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
        self.last_error: str = ""  # last write-operation error message (for diagnostics)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Make an authenticated request to Jira REST API.

        On HTTPError, attempts to read the response body so callers see the real
        Jira error message (e.g. "Transition is not valid for the current state").
        """
        import urllib.request, urllib.error
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15, context=self._ssl_ctx) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            # Surface Jira's structured error so callers can see the real reason
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {err_body[:300]}") from e

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
        self.last_error = ""
        try:
            self._request("POST", f"/rest/api/2/issue/{issue_key}/comment", {
                "body": comment,
            })
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    # Idempotent target sets: a request to move to `key` is satisfied if the issue
    # is already in any of `value` statuses. Reflects the typical workflow:
    #   할 일 → 진행 중 → 종료 요청 → 완료
    # so e.g. asking to move to "종료 요청" is satisfied if it's already in 완료.
    IDEMPOTENT_TARGETS = {
        "진행 중": {"진행 중", "종료 요청", "완료"},
        "종료 요청": {"종료 요청", "완료"},
        "할 일": {"할 일"},
    }

    def transition_issue(self, issue_key: str, status: str, comment: str = "") -> bool:
        """Transition a Jira issue to the given status name.

        Idempotent: if the issue is already at or past the target status (per
        IDEMPOTENT_TARGETS), this is treated as success. If a comment was
        provided, it is still added separately so the user's note is preserved.

        Otherwise, available transitions are pre-checked so we can give a clear
        error like "현재 'X' 상태에서 'Y' 전환 불가" instead of a generic 400.
        """
        self.last_error = ""
        transition_id = self.TRANSITIONS.get(status)
        if not transition_id:
            self.last_error = f"unknown target status '{status}'"
            return False
        try:
            # 1) Idempotency check — already at/past the target?
            info = self._request("GET", f"/rest/api/2/issue/{issue_key}?fields=status")
            current = info.get("fields", {}).get("status", {}).get("name", "")
            satisfies = self.IDEMPOTENT_TARGETS.get(status, {status})
            if current in satisfies:
                # Goal already achieved. If user typed a comment, attach it; otherwise no-op.
                if comment:
                    return self.add_comment(issue_key, comment)
                return True

            # 2) Need to actually transition — pre-check available transitions
            avail = self._request("GET", f"/rest/api/2/issue/{issue_key}/transitions")
            available_ids = {tr["id"]: tr["name"] for tr in avail.get("transitions", [])}
            if transition_id not in available_ids:
                avail_names = list(available_ids.values()) or ["없음"]
                self.last_error = (
                    f"현재 '{current}' 상태에서 '{status}' 전환 불가 "
                    f"(가용 트랜지션: {', '.join(avail_names)})"
                )
                return False

            payload: dict[str, Any] = {"transition": {"id": transition_id}}
            if comment:
                payload["update"] = {"comment": [{"add": {"body": comment}}]}
            self._request("POST", f"/rest/api/2/issue/{issue_key}/transitions", payload)
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    def complete_issue(self, issue_key: str, comment: str = "") -> bool:
        """Mark issue as '종료 요청' with a completion comment."""
        if comment:
            return self.transition_issue(issue_key, "종료 요청", comment)
        return self.transition_issue(issue_key, "종료 요청")

    def update_description(self, issue_key: str, description: str) -> bool:
        """Replace an issue's description via PUT /rest/api/2/issue/{key}."""
        self.last_error = ""
        try:
            self._request("PUT", f"/rest/api/2/issue/{issue_key}", {
                "fields": {"description": description},
            })
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    def _fetch_descriptions(self, issue_keys: list[str]) -> dict[str, str]:
        """Batch-fetch description bodies for the given issue keys.

        The Jira agile sprint endpoint returns subtasks without their descriptions,
        so we issue one JQL search to fill them in. Returns {key: description}.
        """
        if not issue_keys:
            return {}
        # Jira JQL has URL length limits, chunk the keys
        descs: dict[str, str] = {}
        chunk = 50
        for i in range(0, len(issue_keys), chunk):
            keys_csv = ",".join(issue_keys[i:i + chunk])
            try:
                data = self._request("GET",
                    f"/rest/api/2/search?jql=key+in+({keys_csv})"
                    f"&fields=description&maxResults={chunk}")
                for issue in data.get("issues", []):
                    descs[issue["key"]] = issue.get("fields", {}).get("description") or ""
            except Exception:
                pass
        return descs

    def _convert_jira_response(self, data: dict, sprint_info: dict | None = None) -> dict[str, Any]:
        """Convert Jira REST API response to sprint_tasks.json format."""
        tasks = []
        seen_keys = set()
        # Collect parent + subtask keys so we can batch-fetch descriptions
        all_keys: list[str] = []
        for issue in data.get("issues", []):
            f = issue.get("fields", {})
            if f.get("issuetype", {}).get("name") in ("부작업", "Sub-task"):
                continue
            all_keys.append(issue["key"])
            for st in f.get("subtasks", []):
                if st.get("key"):
                    all_keys.append(st["key"])
        descs = self._fetch_descriptions(all_keys)

        for issue in data.get("issues", []):
            key = issue["key"]
            if key in seen_keys:
                continue
            fields = issue.get("fields", {})
            if fields.get("issuetype", {}).get("name") in ("부작업", "Sub-task"):
                continue
            seen_keys.add(key)

            status_name = fields.get("status", {}).get("name", "")
            status_map = {"완료": "done", "종료 요청": "done", "진행 중": "in_progress"}
            tasks.append({
                "key": key,
                "title": fields.get("summary", ""),
                "description": descs.get(key, ""),
                "status": status_map.get(status_name, "pending"),
                "start": str(fields.get("customfield_10230", "") or "")[:10],
                "end": str(fields.get("customfield_10900", "") or "")[:10],
                "subtasks": [
                    {
                        "title": st["fields"]["summary"],
                        "description": descs.get(st.get("key", ""), ""),
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
