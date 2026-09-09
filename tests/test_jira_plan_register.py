"""Reviewed plan (tasks + subtasks): validation, preview, registration, proxy route."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from workflow.jira_plan_register import (
    PlanRunStore, apply_status_by_dates, create_subtask, describe_plan, item_id, plan_fingerprint,
    probe_existing, register_plan, status_for_dates, validate_plan,
)


def _plan(**overrides):
    plan = {
        "epics": {"APPL-373": "ISO26262"},
        "tasks": [
            {
                "epic": "APPL-373",
                "summary": "1차 배포 준비",
                "description": "* 배포 문서 정정\n* 백업 절차",
                "start": "2026-09-08",
                "end": "2026-10-15",
                "done_note": "",
                "subtasks": [
                    {"summary": "배포 문서 정정", "description": "문서", "start": "2026-09-08", "end": "2026-09-25"},
                    {"summary": "백업 절차 신설", "description": "", "start": "", "end": "", "done_note": "실증 완료"},
                ],
            },
            {
                "epic": "APPL-373",
                "summary": "품질 기반 구축",
                "description": "* 추적성",
                "start": "2026-07-21",
                "end": "2026-09-07",
                "done_note": "09-07 완료",
                "subtasks": [],
            },
        ],
    }
    plan.update(overrides)
    return plan


class FakeProvider:
    """Records every write; never talks to Jira."""

    def __init__(self, fail_task: str = "", fail_transition: str = "", fail_subtask: str = "",
                 existing: dict | None = None):
        self.calls: list[tuple] = []
        self.last_error = ""
        self._n = 100
        self._fail_task = fail_task
        self._fail_transition = fail_transition
        self._fail_subtask = fail_subtask
        self._existing = existing or {}  # summary -> key, what "Jira" already holds

    def find_issue_by_exact_summary(self, summary, project_key="", epic_key="", parent_key=""):
        self.calls.append(("find", summary, epic_key, parent_key))
        return self._existing.get(summary, "")

    def _next(self) -> str:
        self._n += 1
        return f"APPL-{self._n}"

    def create_issue(self, issuetype, summary, description="", start="", end="", epic_key="",
                     project_key="", report_required="", labels=None):
        if summary == self._fail_task:
            self.last_error = "주간보고 사항 필수"
            return ""
        key = self._next()
        self.calls.append(("task", key, summary, start, end, epic_key, project_key, report_required))
        return key

    def _request(self, method, path, body=None):
        if method == "GET" and path.startswith("/rest/api/2/issue/"):
            return {"fields": {"customfield_11100": {"id": "1"}, "components": [{"id": "9"}],
                               "customfield_10230": "2026-09-08"}}
        if method == "POST" and path == "/rest/api/2/issue":
            if body["fields"]["summary"] == self._fail_subtask:
                raise RuntimeError("HTTP 400: 필수 필드 누락")
            key = self._next()
            self.calls.append(("subtask", key, body["fields"]))
            return {"key": key}
        if method == "POST" and path.startswith("/rest/agile/1.0/sprint/"):
            self.calls.append(("sprint", path, list(body["issues"])))
            return {}
        raise AssertionError(f"unexpected request {method} {path}")

    def add_comment(self, key, text):
        self.calls.append(("comment", key, text))
        return True

    def update_dates(self, key, start, end):
        self.calls.append(("dates", key, start, end))
        return True

    def transition_issue(self, key, status, comment=""):
        if key == self._fail_transition:
            self.last_error = "현재 '할 일' 상태에서 전환 불가"
            return False
        self.calls.append(("transition", key, status, comment))
        return True

    def complete_issue(self, key, comment=""):
        self.calls.append(("complete", key, comment))
        return True


# --- validate / describe -----------------------------------------------------

def test_validate_accepts_reviewed_plan_and_normalizes():
    plan, errors = validate_plan(_plan())
    assert errors == []
    assert plan["tasks"][0]["subtasks"][1]["done_note"] == "실증 완료"
    assert plan["tasks"][0]["subtasks"][0]["start"] == "2026-09-08"


@pytest.mark.parametrize("mutate, expected", [
    (lambda t: t.__setitem__("epic", "appl-373"), "큰틀 키 형식"),
    (lambda t: t.__setitem__("summary", " "), "제목이(가) 비어"),
    (lambda t: t.__setitem__("start", "2026-11-01"), "시작일이 종료일보다"),
    (lambda t: t.__setitem__("end", "20261015"), "YYYY-MM-DD"),
    (lambda t: t["subtasks"][0].__setitem__("end", ""), "둘 다 적거나"),
    (lambda t: t["subtasks"][0].__setitem__("summary", ""), "부작업 1: 제목이 비어"),
])
def test_validate_reports_each_defect(mutate, expected):
    raw = _plan()
    mutate(raw["tasks"][0])
    _plan_out, errors = validate_plan(raw)
    assert any(expected in e for e in errors), errors


def test_validate_rejects_empty_or_non_object():
    assert validate_plan(None)[1] == ["계획은 JSON 객체여야 합니다"]
    assert validate_plan({"tasks": []})[1] == ["등록할 작업이 없습니다"]


def test_describe_counts_match_cli_dry_run():
    plan, _ = validate_plan(_plan())
    d = describe_plan(plan)
    assert (d["tasks"], d["subtasks"], d["total"], d["notes"]) == (2, 2, 4, 2)
    assert [l["kind"] for l in d["lines"]] == ["작업", "부작업", "부작업", "작업"]
    assert d["lines"][2]["note"] is True and d["lines"][2]["start"] == ""


# --- register ----------------------------------------------------------------

def test_register_writes_in_order_and_adds_to_sprint():
    plan, _ = validate_plan(_plan())
    prov = FakeProvider()
    created = register_plan(plan, prov, "APPL", sprint_id=152)

    kinds = [c[0] for c in prov.calls if c[0] != "find"]
    # task → its subtasks (dates/comments right after each) → next task → sprint last
    assert kinds == ["task", "subtask", "dates", "subtask", "comment", "task", "comment", "sprint"]
    prov.calls = [c for c in prov.calls if c[0] != "find"]
    task_call = prov.calls[0]
    assert task_call[5:] == ("APPL-373", "APPL", "yes")
    sub_fields = prov.calls[1][2]
    assert sub_fields["parent"] == {"key": "APPL-101"}
    assert sub_fields["issuetype"] == {"name": "부작업"}
    assert sub_fields["customfield_11100"] == {"id": "1"}  # inherited from parent
    assert sub_fields["components"] == [{"id": "9"}]
    assert prov.calls[2] == ("dates", "APPL-102", "2026-09-08", "2026-09-25")
    assert prov.calls[4] == ("comment", "APPL-103", "실증 완료")
    assert prov.calls[6] == ("comment", "APPL-104", "09-07 완료")
    assert prov.calls[7][2] == ["APPL-101", "APPL-102", "APPL-103", "APPL-104"]
    assert [c["key"] for c in created] == ["APPL-101", "APPL-102", "APPL-103", "APPL-104"]
    assert created[1]["parent"] == "APPL-101"


def test_register_skips_subtasks_of_a_failed_task_and_reports_it():
    plan, _ = validate_plan(_plan())
    prov = FakeProvider(fail_task="1차 배포 준비")
    created = register_plan(plan, prov, "APPL")
    assert created[0]["key"] == "" and created[0]["error"] == "주간보고 사항 필수"
    assert [c[0] for c in prov.calls if c[0] != "find"] == ["task", "comment"]  # second task only, no orphan subtasks
    assert created[1]["key"] == "APPL-101"


def test_create_subtask_without_parent_fetch_still_posts():
    class Bare:
        def _request(self, method, path, body=None):
            if method == "GET":
                raise RuntimeError("parent unreadable")
            return {"key": "APPL-7"}
    assert create_subtask(Bare(), "APPL-1", "x") == "APPL-7"


# --- proxy route -------------------------------------------------------------

def _serve(jp):
    server = jp._ReusableHTTPServer(("127.0.0.1", 0), jp.ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _post(port, path, payload, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-Proxy-Token": token})
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class MustNotWrite:
    def create_issue(self, *a, **k):
        raise AssertionError("dry-run must not create")

    def _request(self, *a, **k):
        raise AssertionError("dry-run must not call Jira")

    add_comment = update_dates = _request


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    from scripts import jira_proxy as jp
    monkeypatch.setattr(jp, "REPO_ROOT", tmp_path)
    server, port = _serve(jp)
    try:
        yield jp, port
    finally:
        server.shutdown()
        server.server_close()


def test_plan_route_preview_never_touches_jira(proxy, monkeypatch):
    jp, port = proxy
    monkeypatch.setattr(jp, "provider", MustNotWrite())
    code, body = _post(port, "/api/jira/plan", {"plan": _plan()}, jp.PROXY_TOKEN)
    assert code == 200
    assert body["ok"] is True and body["applied"] is False
    assert body["preview"]["total"] == 4


def test_plan_route_returns_validation_errors(proxy, monkeypatch):
    jp, port = proxy
    monkeypatch.setattr(jp, "provider", MustNotWrite())
    raw = _plan(); raw["tasks"][0]["start"] = "2027-01-01"
    code, body = _post(port, "/api/jira/plan", {"plan": raw, "apply": True, "confirm_total": 4}, jp.PROXY_TOKEN)
    assert code == 400 and body["ok"] is False
    assert any("시작일이 종료일보다" in e for e in body["errors"])


def _previewed(port, token, plan):
    _code, body = _post(port, "/api/jira/plan", {"plan": plan}, token)
    return body["preview"]["total"], body["preview"]["fingerprint"]


def test_plan_route_refuses_apply_with_stale_preview(proxy, monkeypatch):
    jp, port = proxy
    monkeypatch.setattr(jp, "provider", MustNotWrite())
    total, fp = _previewed(port, jp.PROXY_TOKEN, _plan())
    # same count, different content → still refused
    edited = _plan(); edited["tasks"][0]["summary"] = "1차 배포 준비 (수정)"
    code, body = _post(port, "/api/jira/plan",
                       {"plan": edited, "apply": True, "confirm_total": total, "confirm_fingerprint": fp}, jp.PROXY_TOKEN)
    assert code == 409 and body["ok"] is False
    assert "다시 미리보기" in body["errors"][0]
    # fingerprint missing entirely → refused
    code, _ = _post(port, "/api/jira/plan", {"plan": _plan(), "apply": True, "confirm_total": total}, jp.PROXY_TOKEN)
    assert code == 409


def test_plan_route_validates_project_key_before_writing(proxy, monkeypatch):
    jp, port = proxy
    monkeypatch.setattr(jp, "provider", MustNotWrite())
    total, fp = _previewed(port, jp.PROXY_TOKEN, _plan())
    code, body = _post(port, "/api/jira/plan",
                       {"plan": _plan(), "apply": True, "confirm_total": total, "confirm_fingerprint": fp,
                        "project_key": "appl; drop"}, jp.PROXY_TOKEN)
    assert code == 400 and "project_key" in body["errors"][0]


def test_plan_route_requires_token(proxy, monkeypatch):
    jp, port = proxy
    monkeypatch.setattr(jp, "provider", MustNotWrite())
    code, _ = _post(port, "/api/jira/plan", {"plan": _plan()}, "wrong")
    assert code == 403


def test_plan_route_apply_registers_through_provider(proxy, monkeypatch):
    jp, port = proxy
    fake = FakeProvider()
    monkeypatch.setattr(jp, "provider", fake)
    total, fp = _previewed(port, jp.PROXY_TOKEN, _plan())
    code, body = _post(port, "/api/jira/plan",
                       {"plan": _plan(), "apply": True, "confirm_total": total, "confirm_fingerprint": fp,
                        "sprint_id": "152", "project_key": "APPL"},
                       jp.PROXY_TOKEN)
    assert code == 200 and body["ok"] is True and body["applied"] is True
    assert body["reused"] == 0
    assert not any(c[0] in ("transition", "complete") for c in fake.calls)  # auto_status off by default
    assert [c["key"] for c in body["created"]] == ["APPL-101", "APPL-102", "APPL-103", "APPL-104"]
    assert body["failed"] == 0
    assert prov_sprint(fake) == ["APPL-101", "APPL-102", "APPL-103", "APPL-104"]


def prov_sprint(prov: FakeProvider):
    return next(c[2] for c in prov.calls if c[0] == "sprint")


def test_subtask_create_route_uses_shared_helper(proxy, monkeypatch):
    jp, port = proxy
    fake = FakeProvider()
    monkeypatch.setattr(jp, "provider", fake)
    code, body = _post(port, "/api/issue/create", {"parent_key": "APPL-1", "summary": "s", "description": "d"}, jp.PROXY_TOKEN)
    assert code == 200 and body == {"ok": True, "key": "APPL-101"}
    assert fake.calls[0][2]["customfield_11100"] == {"id": "1"}


def test_dashboard_board_exposes_plan_composer():
    from scripts.generate_periodic_reports import JIRA_BOARD_SCRIPT
    assert "window.jiraNewPlan = function" in JIRA_BOARD_SCRIPT
    assert "/api/jira/plan" in JIRA_BOARD_SCRIPT
    # a non-raw Python string: the JS newline literal must survive as two characters
    assert "split('\\n')" in JIRA_BOARD_SCRIPT


def test_plan_route_apply_with_auto_status_transitions_by_dates(proxy, monkeypatch):
    jp, port = proxy
    fake = FakeProvider()
    monkeypatch.setattr(jp, "provider", fake)

    class FixedDate:
        @staticmethod
        def today():
            from datetime import date as _d
            return _d(2026, 9, 9)
    monkeypatch.setattr(jp, "date", FixedDate)
    total, fp = _previewed(port, jp.PROXY_TOKEN, _plan())
    code, body = _post(port, "/api/jira/plan",
                       {"plan": _plan(), "apply": True, "confirm_total": total, "confirm_fingerprint": fp,
                        "auto_status": True, "project_key": "APPL"}, jp.PROXY_TOKEN)
    assert code == 200 and body["ok"] is True
    statuses = {c["key"]: c["status"] for c in body["created"]}
    # today (2026-09-09): task1 09-08~10-15 → 진행 중, sub1 09-08~09-25 → 진행 중,
    # sub2 no dates → '', task2 07-21~09-07 → 종료 요청
    assert statuses == {"APPL-101": "진행 중", "APPL-102": "진행 중", "APPL-103": "", "APPL-104": "종료 요청"}


# --- fingerprint / status rule --------------------------------------------------

def test_fingerprint_changes_on_content_not_on_key_order():
    a, _ = validate_plan(_plan())
    b, _ = validate_plan(_plan())
    assert plan_fingerprint(a) == plan_fingerprint(b) == describe_plan(a)["fingerprint"]
    b["tasks"][0]["end"] = "2026-10-16"
    assert plan_fingerprint(a) != plan_fingerprint(b)


@pytest.mark.parametrize("start, end, expected", [
    ("2026-07-21", "2026-09-07", "종료 요청"),
    ("2026-09-08", "2026-10-15", "진행 중"),
    ("2026-09-09", "2026-09-09", "진행 중"),
    ("2026-09-14", "2026-10-04", ""),
    ("", "", ""),
])
def test_status_for_dates(start, end, expected):
    assert status_for_dates(start, end, "2026-09-09") == expected


def test_apply_status_walks_workflow_and_reports_failures():
    prov = FakeProvider(fail_transition="APPL-2")
    created = [
        {"key": "APPL-1", "kind": "작업", "start": "2026-07-01", "end": "2026-08-01", "done_note": "끝"},
        {"key": "APPL-2", "kind": "작업", "start": "2026-09-01", "end": "2026-09-30", "done_note": ""},
        {"key": "APPL-3", "kind": "작업", "start": "2026-07-01", "end": "2026-08-01", "done_note": ""},
        {"key": "", "kind": "작업", "start": "2026-07-01", "end": "2026-08-01", "error": "x"},
    ]
    apply_status_by_dates(created, prov, "2026-09-09")
    # finished item: 할 일 → 진행 중 → 종료 요청 (Jira allows no shortcut), note = done_note
    assert prov.calls[0] == ("transition", "APPL-1", "진행 중", "")
    assert prov.calls[1] == ("complete", "APPL-1", "끝")
    assert created[1]["status"] == "진행 중" and "전환 불가" in created[1]["status_error"]
    # no done_note → dated default comment so the audit trail still exists
    assert prov.calls[-1] == ("complete", "APPL-3", "2026-09-09 종료 요청 — 계획 종료일(2026-08-01) 경과")
    assert created[3]["status"] == "종료 요청" and "status_error" not in created[3]


def test_cli_register_script_reuses_shared_validation(tmp_path, capsys):
    import subprocess, sys
    spec = tmp_path / "draft.json"
    raw = _plan(); raw["tasks"][0]["end"] = "2026-01-01"
    spec.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([sys.executable, "scripts/register_jira_plan.py", "--spec", str(spec)],
                          capture_output=True, text=True, encoding="utf-8", cwd="D:/Project/Program/AutoReport")
    assert proc.returncode != 0 and "시작일이 종료일보다" in (proc.stderr + proc.stdout)
    spec.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([sys.executable, "scripts/register_jira_plan.py", "--spec", str(spec)],
                          capture_output=True, text=True, encoding="utf-8", cwd="D:/Project/Program/AutoReport")
    assert proc.returncode == 0 and "합계 4건" in proc.stdout and "dry-run" in proc.stdout


# --- retry after partial failure -------------------------------------------------

def test_validate_rejects_duplicate_titles_that_would_be_ambiguous_on_retry():
    raw = _plan(); raw["tasks"][1]["summary"] = raw["tasks"][0]["summary"]
    assert any("같은 제목의 작업" in e for e in validate_plan(raw)[1])
    raw = _plan(); raw["tasks"][0]["subtasks"][1]["summary"] = raw["tasks"][0]["subtasks"][0]["summary"]
    assert any("같은 제목의 부작업" in e for e in validate_plan(raw)[1])


def test_item_id_ignores_dates_and_body_but_not_scope():
    assert item_id("task", "APPL-373", "x") == item_id("task", "APPL-373", "x")
    assert item_id("task", "APPL-373", "x") != item_id("task", "APPL-418", "x")
    assert item_id("task", "APPL-373", "x") != item_id("subtask", "APPL-373", "x")


def test_retry_reuses_run_record_and_creates_only_the_gaps(tmp_path):
    plan, _ = validate_plan(_plan())
    store = PlanRunStore(tmp_path / "runs")
    first = FakeProvider(fail_subtask="백업 절차 신설")
    created = register_plan(plan, first, "APPL", store=store)
    assert [c.get("key") for c in created] == ["APPL-101", "APPL-102", "", "APPL-103"]
    assert created[2]["error"].startswith("HTTP 400")
    rec = store.load(plan_fingerprint(plan))
    assert set(rec.values()) == {"APPL-101", "APPL-102", "APPL-103"}  # written despite the crash mid-run

    second = FakeProvider()  # a fresh provider: nothing in "Jira" search either
    created = register_plan(plan, second, "APPL", sprint_id=152, store=store)
    kinds = [c[0] for c in second.calls if c[0] != "find"]
    # only the missing subtask is created (+ its comment); no second task/comment/dates
    assert kinds == ["subtask", "comment", "sprint"]
    assert [(c["key"], bool(c.get("reused"))) for c in created] == [
        ("APPL-101", True), ("APPL-102", True), ("APPL-101", False), ("APPL-103", True)]
    assert created[2]["parent"] == "APPL-101"
    assert store.load(plan_fingerprint(plan))[created[2]["item_id"]] == "APPL-101"


def test_retry_without_run_record_falls_back_to_jira_exact_summary(tmp_path):
    plan, _ = validate_plan(_plan())
    prov = FakeProvider(existing={"1차 배포 준비": "APPL-900", "배포 문서 정정": "APPL-901"})
    created = register_plan(plan, prov, "APPL", store=PlanRunStore(tmp_path / "runs"))
    assert [(c["key"], bool(c.get("reused"))) for c in created] == [
        ("APPL-900", True), ("APPL-901", True), ("APPL-101", False), ("APPL-102", False)]
    # the subtask probe is scoped to the reused parent, the task probe to the epic
    assert ("find", "배포 문서 정정", "", "APPL-900") in prov.calls
    assert ("find", "1차 배포 준비", "APPL-373", "") in prov.calls
    # a reused item gets no second done_note comment
    assert not any(c[0] == "comment" and c[1] == "APPL-900" for c in prov.calls)


def test_run_store_survives_corrupt_file_and_writes_atomically(tmp_path):
    store = PlanRunStore(tmp_path)
    (tmp_path / "abc.json").write_text("{not json", encoding="utf-8")
    assert store.load("abc") == {}
    store.record("abc", "i1", "APPL-1", "one")
    store.record("abc", "i2", "APPL-2", "two")
    assert store.load("abc") == {"i1": "APPL-1", "i2": "APPL-2"}
    assert not (tmp_path / "abc.json.tmp").exists()


def test_plan_route_retry_reports_reused_and_zero_failed(proxy, monkeypatch):
    jp, port = proxy
    fake = FakeProvider(fail_subtask="백업 절차 신설")
    monkeypatch.setattr(jp, "provider", fake)
    total, fp = _previewed(port, jp.PROXY_TOKEN, _plan())
    req = {"plan": _plan(), "apply": True, "confirm_total": total, "confirm_fingerprint": fp, "project_key": "APPL"}
    code, body = _post(port, "/api/jira/plan", req, jp.PROXY_TOKEN)
    assert code == 200 and body["ok"] is False and body["failed"] == 1 and body["reused"] == 0

    monkeypatch.setattr(jp, "provider", FakeProvider())
    code, body = _post(port, "/api/jira/plan", req, jp.PROXY_TOKEN)
    assert code == 200 and body["ok"] is True
    assert body["failed"] == 0 and body["reused"] == 3
    assert [c["key"] for c in body["created"]] == ["APPL-101", "APPL-102", "APPL-101", "APPL-103"]


# --- preview-time existence probe ------------------------------------------------

def test_probe_existing_only_searches_subtasks_under_found_parents(tmp_path):
    plan, _ = validate_plan(_plan())
    prov = FakeProvider(existing={"1차 배포 준비": "APPL-900", "배포 문서 정정": "APPL-901"})
    found = probe_existing(plan, prov, "APPL", PlanRunStore(tmp_path))
    tid = item_id("task", "APPL-373", "1차 배포 준비")
    assert found == {tid: "APPL-900", item_id("subtask", tid, "배포 문서 정정"): "APPL-901"}
    probes = [c[1] for c in prov.calls if c[0] == "find"]
    # task 2 does not exist → its (zero) subtasks are never probed; task 1's two are
    assert probes == ["1차 배포 준비", "배포 문서 정정", "백업 절차 신설", "품질 기반 구축"]

    d = describe_plan(plan, found)
    assert (d["new"], d["reused"]) == (2, 2)
    assert [l["existing_key"] for l in d["lines"]] == ["APPL-900", "APPL-901", "", ""]


def test_register_with_probed_map_does_not_search_again(tmp_path):
    plan, _ = validate_plan(_plan())
    prov = FakeProvider(existing={"1차 배포 준비": "APPL-900"})
    found = probe_existing(plan, prov, "APPL", PlanRunStore(tmp_path))
    prov.calls.clear()
    created = register_plan(plan, prov, "APPL", store=PlanRunStore(tmp_path), existing=found)
    assert created[0] == {**created[0], "key": "APPL-900", "reused": True}
    assert not any(c[0] == "find" and c[1] == "1차 배포 준비" for c in prov.calls)


def test_auto_status_keeps_reused_items_untouched():
    prov = FakeProvider()
    created = [
        {"key": "APPL-1", "kind": "작업", "start": "2026-07-01", "end": "2026-08-01", "done_note": "끝", "reused": True},
        {"key": "APPL-2", "kind": "작업", "start": "2026-07-01", "end": "2026-08-01", "done_note": ""},
    ]
    apply_status_by_dates(created, prov, "2026-09-09")
    assert created[0]["status"] == "" and created[0]["status_kept"] is True
    assert [c[1] for c in prov.calls] == ["APPL-2", "APPL-2"]  # only the new one moves


def test_plan_route_preview_reports_reuse_before_apply(proxy, monkeypatch):
    jp, port = proxy
    monkeypatch.setattr(jp, "provider", FakeProvider(existing={"품질 기반 구축": "APPL-777"}))
    code, body = _post(port, "/api/jira/plan", {"plan": _plan(), "project_key": "APPL"}, jp.PROXY_TOKEN)
    assert code == 200 and body["applied"] is False
    assert (body["preview"]["new"], body["preview"]["reused"]) == (3, 1)
    assert body["preview"]["lines"][3]["existing_key"] == "APPL-777"


def test_validation_messages_use_korean_field_names():
    raw = _plan(); raw["tasks"][0]["description"] = ""; raw["tasks"][0]["epic"] = ""
    errors = validate_plan(raw)[1]
    assert any("본문이(가) 비어" in e for e in errors) and any("큰틀이(가) 비어" in e for e in errors)
    assert not any("description" in e or "epic 가" in e for e in errors)
