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

    def update_dates(self, issue_key: str, start: str, end: str) -> bool:
        """Set Start/End date custom fields. Empty string clears. Returns True on success."""
        return False

    def list_epics(self, project_key: str = "") -> list[dict]:
        """Return [{key, summary}] of open Epics. project_key overrides default."""
        return []

    def create_issue(self, issuetype: str, summary: str, description: str = "",
                     start: str = "", end: str = "", epic_key: str = "",
                     project_key: str = "", report_required: str = "") -> str:
        """Create a top-level issue (Epic or Task). project_key overrides default.

        Returns new issue key, or '' on failure.
        """
        return ""


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
        self._epic_link_field: str | None = None  # lazy-detected customfield ID for Epic Link
        self._epic_name_field: str | None = None  # lazy-detected customfield ID for Epic Name
        self._issuetype_names: dict[str, str] | None = None  # lazy {"epic": "에픽"|"Epic", "task": "작업"|"Task"}

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
            # epic_link customfield (보통 customfield_10008, 인스턴스마다 다를 수 있음)
            elf = self._detect_epic_link_field() or "customfield_10008"
            if self.sprint_id:
                data = self._request("GET",
                    f"/rest/agile/1.0/sprint/{self.sprint_id}/issue"
                    f"?maxResults=100&fields=summary,status,issuetype,subtasks,"
                    f"customfield_10230,customfield_10900,{elf}")
                sprint_info = self._request("GET",
                    f"/rest/agile/1.0/sprint/{self.sprint_id}")
                return self._convert_jira_response(data, sprint_info, epic_link_field=elf)
            else:
                data = self._request("GET",
                    f"/rest/api/2/search?jql=project={self.project_key}"
                    f"+AND+type=Task&fields=summary,subtasks,status,"
                    f"customfield_10230,customfield_10900,{elf}")
                return self._convert_jira_response(data, epic_link_field=elf)
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

    def _fetch_extra(self, issue_keys: list[str]) -> dict[str, dict[str, str]]:
        """Batch-fetch description + start/end dates for the given issue keys.

        The Jira agile sprint endpoint returns subtasks without their descriptions
        or custom date fields, so one JQL search fills them in. Returns
        {key: {"description": str, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}.
        Missing fields default to empty string.
        """
        if not issue_keys:
            return {}
        # Jira JQL has URL length limits, chunk the keys.
        out: dict[str, dict[str, str]] = {}
        chunk = 50
        for i in range(0, len(issue_keys), chunk):
            keys_csv = ",".join(issue_keys[i:i + chunk])
            try:
                data = self._request("GET",
                    f"/rest/api/2/search?jql=key+in+({keys_csv})"
                    f"&fields=description,customfield_10230,customfield_10900"
                    f"&maxResults={chunk}")
                for issue in data.get("issues", []):
                    f = issue.get("fields", {})
                    out[issue["key"]] = {
                        "description": f.get("description") or "",
                        "start": str(f.get("customfield_10230") or "")[:10],
                        "end": str(f.get("customfield_10900") or "")[:10],
                    }
            except Exception:
                pass
        return out

    def update_dates(self, issue_key: str, start: str, end: str) -> bool:
        """Set the Start/End date custom fields on an issue.

        Empty string clears the field (sends null to Jira). YYYY-MM-DD strings
        are passed through verbatim — Jira stores them as ISO dates.
        """
        self.last_error = ""
        try:
            self._request("PUT", f"/rest/api/2/issue/{issue_key}", {
                "fields": {
                    "customfield_10230": (start or None),
                    "customfield_10900": (end or None),
                },
            })
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    def _scan_fields(self) -> list[dict]:
        """Fetch /rest/api/2/field once and cache via instance for both helpers.

        Only caches non-empty results — a transient Jira failure on the first
        call would otherwise pin the cache to [] permanently, leaving all
        Epic-related field detection on the dumb fallback path forever.
        """
        cache_attr = "_field_scan_cache"
        cached = getattr(self, cache_attr, None)
        if cached:
            return cached
        try:
            data = self._request("GET", "/rest/api/2/field")
            scanned = data if isinstance(data, list) else []
        except Exception:
            scanned = []
        if scanned:
            setattr(self, cache_attr, scanned)
        return scanned

    def _detect_epic_link_field(self) -> str:
        """Lazy-detect the 'Epic Link' customfield ID.

        Prefers the Greenhopper schema marker (instance-name agnostic) and
        falls back to localised name matching, finally to customfield_10008.
        """
        if self._epic_link_field is not None:
            return self._epic_link_field
        cand = "customfield_10008"
        for f in self._scan_fields():
            schema = (f.get("schema") or {}).get("custom") or ""
            if schema == "com.pyxis.greenhopper.jira:gh-epic-link":
                cand = f.get("id", cand)
                break
        else:
            for f in self._scan_fields():
                name = (f.get("name") or "").strip().lower()
                if name in ("epic link", "에픽 링크", "큰틀 링크"):
                    cand = f.get("id", cand)
                    break
        self._epic_link_field = cand
        return cand

    def _detect_epic_name_field(self) -> str:
        """Lazy-detect the 'Epic Name' customfield ID.

        Some Jira instances localise the Epic Name field (e.g. "큰틀의 이름")
        so name-only matching is fragile. Prefer the Greenhopper schema
        marker `gh-epic-label`, falling back to localised name matching.
        Returns '' if the field doesn't exist in this instance.
        """
        if self._epic_name_field is not None:
            return self._epic_name_field
        cand = ""
        for f in self._scan_fields():
            schema = (f.get("schema") or {}).get("custom") or ""
            if schema == "com.pyxis.greenhopper.jira:gh-epic-label":
                cand = f.get("id", "")
                break
        else:
            for f in self._scan_fields():
                name = (f.get("name") or "").strip().lower()
                if name in ("epic name", "에픽 이름", "큰틀 이름", "큰틀의 이름"):
                    cand = f.get("id", "")
                    break
        self._epic_name_field = cand
        return cand

    def _detect_issuetype_names(self) -> dict[str, str]:
        """Lazy-detect Korean/English names of Epic and Task issue types.

        The instance may use Korean names (에픽/작업) or English (Epic/Task);
        we ask Jira once and cache the result. Falls back to English defaults.
        """
        if self._issuetype_names is not None:
            return self._issuetype_names
        out = {"epic": "Epic", "task": "Task"}
        try:
            data = self._request("GET", "/rest/api/2/issuetype")
            for it in data if isinstance(data, list) else []:
                name = (it.get("name") or "").strip()
                lname = name.lower()
                if it.get("subtask"):
                    continue
                # This Jira instance uses "큰틀" as the Epic-equivalent issuetype
                # (project APPL, id=10000). We accept the canonical English/Korean
                # names plus that instance-specific Korean alias.
                if lname in ("epic", "에픽", "큰틀"):
                    out["epic"] = name
                elif lname in ("task", "작업"):
                    out["task"] = name
        except Exception:
            pass
        self._issuetype_names = out
        return out

    def list_epics(self, project_key: str = "") -> list[dict]:
        """Return open Epics for the given project as [{key, summary}].

        Filters out Done/Closed so the user doesn't get a long stale list.
        Uses the detected issuetype name (Epic / 에픽) so JQL matches even on
        Korean-only Jira instances. `project_key` overrides the default.
        """
        self.last_error = ""
        pk = (project_key or self.project_key).strip()
        if not pk:
            self.last_error = "project_key 누락"
            return []
        # Defensive sanitisation: JQL identifiers are alphanumeric + dash/underscore
        # in practice. Reject anything else rather than escape — the project_key
        # comes from server-rendered config so a stray character indicates a bug.
        if not all(c.isalnum() or c in "-_" for c in pk):
            self.last_error = f"invalid project key: {pk!r}"
            return []
        try:
            from urllib.parse import quote
            epic_name = self._detect_issuetype_names().get("epic", "Epic")
            # epic_name may contain Korean characters; double-quote it in JQL.
            # Escape any literal double-quotes that could break the JQL string.
            epic_name_safe = epic_name.replace('"', '\\"')
            jql = (
                f'project={pk} AND issuetype="{epic_name_safe}" '
                f"AND statusCategory != Done ORDER BY created DESC"
            )
            data = self._request("GET",
                f"/rest/api/2/search?jql={quote(jql)}&fields=summary&maxResults=100")
            out: list[dict] = []
            for issue in data.get("issues", []):
                out.append({
                    "key": issue.get("key", ""),
                    "summary": (issue.get("fields") or {}).get("summary", ""),
                })
            return out
        except Exception as e:
            self.last_error = str(e)
            return []

    def create_issue(self, issuetype: str, summary: str, description: str = "",
                     start: str = "", end: str = "", epic_key: str = "",
                     project_key: str = "", report_required: str = "") -> str:
        """Create an Epic or Task at the project root.

        - issuetype: case-insensitive 'epic' or 'task' (Korean 에픽/작업/큰틀 also accepted).
        - project_key: overrides the provider's default project (multi-board support).
        - report_required: 'yes' / 'no' / '' — fills customfield_11100 (주간보고 사항)
          which this Jira instance marks required for Task. Explicit user value
          wins over parent-Epic inheritance.
        Returns the new issue key, or '' on failure (self.last_error is set).
        """
        self.last_error = ""
        kind = issuetype.strip().lower()
        if kind in ("에픽", "큰틀"):
            kind = "epic"
        elif kind in ("작업",):
            kind = "task"
        if kind not in ("epic", "task"):
            self.last_error = f"unsupported issuetype '{issuetype}'"
            return ""
        pk = (project_key or self.project_key).strip()
        if not pk:
            self.last_error = "project_key 누락"
            return ""
        try:
            names = self._detect_issuetype_names()
            fields: dict[str, Any] = {
                "project": {"key": pk},
                "summary": summary,
                "issuetype": {"name": names[kind]},
            }
            if description:
                fields["description"] = description
            # Start/End custom fields are configured on Task/Sub-task screens
            # only in this Jira instance. Sending them on the Epic (큰틀)
            # creation screen returns "Field 'customfield_10230' cannot be set".
            if kind != "epic":
                if start:
                    fields["customfield_10230"] = start
                if end:
                    fields["customfield_10900"] = end
                # 주간보고 사항 is a required select on Task creation in this
                # instance. Option IDs: 11100=Yes, 11101=No. Sent only when
                # the user picked a value — falls through to parent inheritance
                # otherwise, and if neither sets it, Jira's 400 surfaces.
                rr = (report_required or "").strip().lower()
                if rr in ("yes", "y", "true", "1"):
                    fields["customfield_11100"] = {"id": "11100"}
                elif rr in ("no", "n", "false", "0"):
                    fields["customfield_11100"] = {"id": "11101"}
            if kind == "epic":
                # Many Jira Server projects require a separate "Epic Name"
                # customfield. Auto-populate it with the summary so the user
                # isn't asked to type the same string twice.
                enf = self._detect_epic_name_field()
                if enf:
                    fields[enf] = summary
            elif kind == "task" and epic_key:
                elf = self._detect_epic_link_field()
                if elf:
                    fields[elf] = epic_key
                # Inherit required customfields from the parent Epic, mirroring
                # the subtask flow. The instance's required field 11100
                # (주간보고 사항) is a select-list — without inheritance, Task
                # creation under an Epic 400s with "주간보고 사항 항목은 필수".
                # User-supplied start/end already populated above take precedence.
                try:
                    inheritable = ("customfield_10230", "customfield_10900", "customfield_11100")
                    parent = self._request(
                        "GET",
                        f"/rest/api/2/issue/{epic_key}?fields={','.join(inheritable)}",
                    )
                    pfields = (parent or {}).get("fields", {}) or {}
                    for cf in inheritable:
                        if cf in fields:
                            continue
                        val = pfields.get(cf)
                        if val is None:
                            continue
                        if isinstance(val, dict) and "id" in val:
                            fields[cf] = {"id": val["id"]}
                        elif isinstance(val, list):
                            fields[cf] = [
                                {"id": v["id"]} if isinstance(v, dict) and "id" in v else v
                                for v in val
                            ]
                        else:
                            fields[cf] = val
                except Exception:
                    # Parent fetch failed — let Jira's own 400 surface the missing field.
                    pass
            result = self._request("POST", "/rest/api/2/issue", {"fields": fields})
            return result.get("key", "")
        except Exception as e:
            self.last_error = str(e)
            return ""

    def _convert_jira_response(self, data: dict, sprint_info: dict | None = None,
                                epic_link_field: str = "customfield_10008") -> dict[str, Any]:
        """Convert Jira REST API response to sprint_tasks.json format.

        Includes Epic link info (epic_key + epic_summary) so reports can group
        tasks by 큰틀(Epic).
        """
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
        extras = self._fetch_extra(all_keys)

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
                "description": extras.get(key, {}).get("description", ""),
                "status": status_map.get(status_name, "pending"),
                "start": str(fields.get("customfield_10230", "") or "")[:10],
                "end": str(fields.get("customfield_10900", "") or "")[:10],
                "epic_key": str(fields.get(epic_link_field, "") or ""),
                "subtasks": [
                    {
                        "title": st["fields"]["summary"],
                        "description": extras.get(st.get("key", ""), {}).get("description", ""),
                        "status": (
                            "done" if st["fields"]["status"]["name"] in ("완료", "종료 요청")
                            else "in_progress" if st["fields"]["status"]["name"] == "진행 중"
                            else "pending"
                        ),
                        "key": st.get("key", ""),
                        "start": extras.get(st.get("key", ""), {}).get("start", ""),
                        "end": extras.get(st.get("key", ""), {}).get("end", ""),
                    }
                    for st in fields.get("subtasks", [])
                ],
                "keywords": [],
            })

        # Batch-fetch epic summaries so the UI can show "APPL-401 소프트웨어 추가, 진단기능 개선"
        # instead of bare keys.
        epic_keys = sorted({t["epic_key"] for t in tasks if t.get("epic_key")})
        epic_summaries = self._fetch_epic_summaries(epic_keys)
        for t in tasks:
            t["epic_summary"] = epic_summaries.get(t.get("epic_key", ""), "")

        sprint = {"name": "", "start": "", "end": ""}
        if sprint_info:
            sprint = {
                "name": sprint_info.get("name", ""),
                "start": str(sprint_info.get("startDate", ""))[:10],
                "end": str(sprint_info.get("endDate", ""))[:10],
            }
        return {"sprint": sprint, "tasks": tasks}

    def _fetch_epic_summaries(self, epic_keys: list[str]) -> dict[str, str]:
        """Batch-fetch summaries for the given Epic keys. Returns {key: summary}.

        Missing keys / failures silently default to empty so callers can still render.
        """
        if not epic_keys:
            return {}
        out: dict[str, str] = {}
        chunk = 50
        for i in range(0, len(epic_keys), chunk):
            keys_csv = ",".join(epic_keys[i:i + chunk])
            try:
                data = self._request("GET",
                    f"/rest/api/2/search?jql=key+in+({keys_csv})"
                    f"&fields=summary&maxResults={chunk}")
                for issue in data.get("issues", []):
                    out[issue["key"]] = issue.get("fields", {}).get("summary", "")
            except Exception:
                pass
        return out


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
