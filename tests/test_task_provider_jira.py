from __future__ import annotations

import ssl
from urllib.parse import unquote

import pytest

from workflow.task_provider import JiraApiTaskProvider


class _PagedProvider(JiraApiTaskProvider):
    def __init__(self, pages: dict[int, dict]):
        super().__init__("https://jira.example.test", "token")
        self.pages = pages
        self.paths: list[str] = []

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        assert method == "GET"
        self.paths.append(path)
        marker = "startAt="
        start = int(path.split(marker, 1)[1].split("&", 1)[0])
        return self.pages[start]


def test_request_all_issues_reads_every_page():
    provider = _PagedProvider(
        {
            0: {"issues": [{"key": f"A-{i}"} for i in range(100)], "total": 205},
            100: {"issues": [{"key": f"A-{i}"} for i in range(100, 200)], "total": 205},
            200: {"issues": [{"key": f"A-{i}"} for i in range(200, 205)], "total": 205},
        }
    )

    result = provider._request_all_issues("/rest/api/2/search?jql=project%3DAPPL")

    assert len(result["issues"]) == 205
    assert ["startAt=0", "startAt=100", "startAt=200"] == [
        next(part for part in path.split("&") if part.startswith("startAt="))
        for path in provider.paths
    ]


def test_request_all_issues_rejects_malformed_page():
    provider = _PagedProvider({0: {"issues": {}, "total": 1}})

    with pytest.raises(RuntimeError, match="issues"):
        provider._request_all_issues("/rest/api/2/search")


def test_tls_verification_is_secure_by_default(monkeypatch):
    monkeypatch.delenv("JIRA_VERIFY_TLS", raising=False)

    provider = JiraApiTaskProvider("https://jira.example.test", "token")

    assert provider._ssl_ctx.verify_mode == ssl.CERT_REQUIRED
    assert provider._ssl_ctx.check_hostname is True


def test_create_issue_sends_stable_deduped_labels(monkeypatch):
    provider = JiraApiTaskProvider("https://jira.example.test", "token")
    monkeypatch.setattr(
        provider,
        "_detect_issuetype_names",
        lambda: {"epic": "Epic", "task": "Task"},
    )
    captured: dict = {}

    def fake_request(method: str, path: str, body: dict | None = None) -> dict:
        captured.update({"method": method, "path": path, "body": body})
        return {"key": "APPL-900"}

    monkeypatch.setattr(provider, "_request", fake_request)

    key = provider.create_issue(
        "task",
        "commit-driven task",
        project_key="APPL",
        report_required="yes",
        labels=["autoreport-abc", "quality", "autoreport-abc", ""],
    )

    assert key == "APPL-900"
    assert captured["body"]["fields"]["labels"] == ["autoreport-abc", "quality"]


def test_find_issues_by_marker_uses_exact_summary_jql(monkeypatch):
    provider = JiraApiTaskProvider("https://jira.example.test", "token")
    captured: dict[str, str] = {}

    def fake_request(method: str, path: str, body: dict | None = None) -> dict:
        captured.update({"method": method, "path": path})
        return {
            "issues": [
                {
                    "key": "APPL-901",
                    "fields": {
                        "summary": "plan [ARID0123456789ABCDEF0123]",
                        "status": {"name": "할 일"},
                    },
                }
            ]
        }

    monkeypatch.setattr(provider, "_request", fake_request)

    found = provider.find_issues_by_marker("ARID0123456789ABCDEF0123", "APPL")

    assert found == [{"key": "APPL-901", "summary": "plan [ARID0123456789ABCDEF0123]", "status": "할 일"}]
    assert captured["method"] == "GET"
    decoded = unquote(captured["path"])
    assert 'summary ~ "\\"ARID0123456789ABCDEF0123\\""' in decoded


def test_find_issues_by_marker_rejects_untrusted_jql_input(monkeypatch):
    provider = JiraApiTaskProvider("https://jira.example.test", "token")
    monkeypatch.setattr(
        provider,
        "_request",
        lambda *args, **kwargs: pytest.fail("invalid marker must not reach Jira"),
    )

    assert provider.find_issues_by_marker('ARID123" OR project=OTHER', "APPL") == []
    assert "invalid proposal marker" in provider.last_error
