"""Reviewed Jira plan (tasks + subtasks) — validation, preview, registration.

A *plan* is the coarse, manager-facing shape that ``register_jira_plan.py`` and
the dashboard's "계획 작성" modal both speak::

    {"epics": {"APPL-373": "..."},
     "tasks": [{"epic": "APPL-373", "summary": "...", "description": "...",
                "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "done_note": "",
                "subtasks": [{"summary", "description", "start", "end", "done_note"}]}]}

``validate_plan`` / ``describe_plan`` never touch Jira.  ``register_plan`` is the
only writer and takes the provider explicitly, so the proxy (single-threaded —
it must not call itself over HTTP) and tests can hand it whatever they like.
Everything created stays in '할 일' unless ``apply_status_by_dates`` is asked
for; comments land only for explicit ``done_note`` values.

Re-registration is safe: every item has a stable id, and ``register_plan``
reuses an issue that a previous (possibly crashed) run already created — first
from the local run record (``PlanRunStore``), then by exact summary under the
same epic/parent in Jira — instead of creating it again.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TASK_REQUIRED = ("epic", "summary", "description", "start", "end")
_FIELD_KO = {"epic": "큰틀", "summary": "제목", "description": "본문", "start": "시작일", "end": "종료일"}
# Fields Jira marks required for 부작업 on this instance; inherited from the parent.
SUBTASK_INHERITED_FIELDS = ("customfield_10230", "customfield_10900", "customfield_11100", "components")


def _s(value: Any) -> str:
    return str(value or "").strip()


def validate_plan(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Return (normalized_plan, errors). errors == [] means registrable."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return {"epics": {}, "tasks": []}, ["계획은 JSON 객체여야 합니다"]
    epics = raw.get("epics") if isinstance(raw.get("epics"), dict) else {}
    tasks_in = raw.get("tasks")
    if not isinstance(tasks_in, list) or not tasks_in:
        return {"epics": epics, "tasks": []}, ["등록할 작업이 없습니다"]

    tasks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for idx, t in enumerate(tasks_in, 1):
        if not isinstance(t, dict):
            errors.append(f"작업 {idx}: 객체가 아닙니다")
            continue
        task: dict[str, Any] = {k: _s(t.get(k)) for k in (*_TASK_REQUIRED, "done_note")}
        label = task["summary"] or f"작업 {idx}"
        for field in _TASK_REQUIRED:
            if not task[field]:
                errors.append(f"{label}: {_FIELD_KO[field]}이(가) 비어 있습니다")
        if task["epic"] and not _ISSUE_KEY_RE.match(task["epic"]):
            errors.append(f"{label}: 큰틀 키 형식이 잘못됐습니다 ({task['epic']})")
        for field in ("start", "end"):
            if task[field] and not _DATE_RE.match(task[field]):
                errors.append(f"{label}: {_FIELD_KO[field]}은 YYYY-MM-DD 여야 합니다")
        if task["start"] and task["end"] and task["start"] > task["end"]:
            errors.append(f"{label}: 시작일이 종료일보다 늦습니다")
        # Same title twice under one epic would be indistinguishable on retry.
        if (task["epic"], task["summary"]) in seen:
            errors.append(f"{label}: 같은 큰틀 아래 같은 제목의 작업이 두 번 있습니다")
        seen.add((task["epic"], task["summary"]))

        subs: list[dict[str, Any]] = []
        sub_seen: set[str] = set()
        for sidx, s in enumerate(t.get("subtasks") or [], 1):
            if not isinstance(s, dict):
                errors.append(f"{label} 부작업 {sidx}: 객체가 아닙니다")
                continue
            sub = {k: _s(s.get(k)) for k in ("summary", "description", "start", "end", "done_note")}
            slabel = sub["summary"] or f"{label} 부작업 {sidx}"
            if not sub["summary"]:
                errors.append(f"{label} 부작업 {sidx}: 제목이 비어 있습니다")
            for field in ("start", "end"):
                if sub[field] and not _DATE_RE.match(sub[field]):
                    errors.append(f"{slabel}: {_FIELD_KO[field]}은 YYYY-MM-DD 여야 합니다")
            if bool(sub["start"]) != bool(sub["end"]):
                errors.append(f"{slabel}: 시작일·종료일은 둘 다 적거나 둘 다 비워야 합니다")
            if sub["start"] and sub["end"] and sub["start"] > sub["end"]:
                errors.append(f"{slabel}: 시작일이 종료일보다 늦습니다")
            if sub["summary"] in sub_seen:
                errors.append(f"{slabel}: 같은 작업 아래 같은 제목의 부작업이 두 번 있습니다")
            sub_seen.add(sub["summary"])
            subs.append(sub)
        task["subtasks"] = subs
        tasks.append(task)
    return {"epics": epics, "tasks": tasks}, errors


def plan_fingerprint(plan: dict[str, Any]) -> str:
    """Stable digest of the normalized plan content.

    The dashboard previews a plan, then registers it; the fingerprint travels
    with the preview and must come back unchanged, so an edit that keeps the
    item count (retitling, moving a date) still invalidates the preview.
    """
    canon = json.dumps(plan.get("tasks") or [], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]


def item_id(kind: str, scope: str, summary: str) -> str:
    """Stable id of one plan item: task = (epic, summary), subtask = (parent task id, summary).

    Dates and descriptions are deliberately left out — fixing a typo in the
    body on a retry must still match the issue the first run created.
    """
    return hashlib.sha1(f"{kind}|{scope}|{summary}".encode("utf-8")).hexdigest()[:12]


def describe_plan(plan: dict[str, Any], existing: dict[str, str] | None = None) -> dict[str, Any]:
    """Counts + human lines for the preview — the same list the CLI dry-run prints.

    *existing* (from ``probe_existing``) marks lines that will be reused rather
    than created, and splits the total into ``new`` / ``reused``.
    """
    existing = existing or {}
    tasks = plan.get("tasks") or []
    subs = sum(len(t.get("subtasks") or []) for t in tasks)
    notes = sum(bool(t.get("done_note")) for t in tasks)
    notes += sum(bool(s.get("done_note")) for t in tasks for s in (t.get("subtasks") or []))
    lines: list[dict[str, Any]] = []
    for t in tasks:
        tid = item_id("task", t["epic"], t["summary"])
        lines.append({"kind": "작업", "epic": t["epic"], "summary": t["summary"],
                      "start": t["start"], "end": t["end"], "note": bool(t.get("done_note")),
                      "item_id": tid, "existing_key": existing.get(tid, "")})
        for s in t.get("subtasks") or []:
            sid = item_id("subtask", tid, s["summary"])
            lines.append({"kind": "부작업", "epic": t["epic"], "summary": s["summary"],
                          "start": s.get("start", ""), "end": s.get("end", ""), "note": bool(s.get("done_note")),
                          "item_id": sid, "existing_key": existing.get(sid, "")})
    reused = sum(1 for l in lines if l["existing_key"])
    return {"tasks": len(tasks), "subtasks": subs, "total": len(tasks) + subs,
            "new": len(lines) - reused, "reused": reused,
            "notes": notes, "lines": lines, "fingerprint": plan_fingerprint(plan)}


class PlanRunStore:
    """Per-plan record of what a registration run created — `item_id → key`.

    One JSON file per plan fingerprint.  Written after *every* successful
    create so a crash mid-run loses nothing; a retry of the same plan reads it
    back and skips the items that already exist.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, fingerprint: str) -> Path:
        return self.root / f"{fingerprint}.json"

    def load(self, fingerprint: str) -> dict[str, str]:
        path = self._path(fingerprint)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = data.get("items") if isinstance(data, dict) else None
        return {str(k): str(v) for k, v in (items or {}).items() if v}

    def record(self, fingerprint: str, iid: str, key: str, summary: str = "") -> None:
        path = self._path(fingerprint)
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        items = data.setdefault("items", {})
        items[iid] = key
        data.setdefault("summaries", {})[iid] = summary
        data["fingerprint"] = fingerprint
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)


def create_subtask(provider: Any, parent_key: str, summary: str, description: str = "") -> str:
    """Create a 부작업 under parent_key, inheriting the required customfields.

    Shared by the proxy's ``/api/issue/create`` and ``register_plan`` so both
    paths build the same payload.  Returns the new key ('' if Jira gave none).
    """
    fields: dict[str, Any] = {
        "project": {"key": parent_key.split("-")[0]},
        "parent": {"key": parent_key},
        "summary": summary,
        "issuetype": {"name": "부작업"},
    }
    if description:
        fields["description"] = description
    try:
        parent = provider._request(
            "GET", f"/rest/api/2/issue/{parent_key}?fields={','.join(SUBTASK_INHERITED_FIELDS)}")
        pfields = (parent or {}).get("fields", {}) or {}
        for cf in SUBTASK_INHERITED_FIELDS:
            val = pfields.get(cf)
            if val is None:
                continue
            if isinstance(val, dict) and "id" in val:
                fields[cf] = {"id": val["id"]}
            elif isinstance(val, list):
                fields[cf] = [{"id": v["id"]} if isinstance(v, dict) and "id" in v else v for v in val]
            else:
                fields[cf] = val
    except Exception:
        # Parent fetch failing just means no inheritance; Jira's own 400 then
        # names the missing field to the caller.
        pass
    result = provider._request("POST", "/rest/api/2/issue", {"fields": fields})
    return str((result or {}).get("key") or "")


def _existing_key(provider: Any, store: PlanRunStore | None, fingerprint: str, iid: str,
                  summary: str, project_key: str, epic_key: str = "", parent_key: str = "",
                  known: dict[str, str] | None = None) -> str:
    if known and known.get(iid):
        return known[iid]
    if store is not None:
        key = store.load(fingerprint).get(iid, "")
        if key:
            return key
    finder = getattr(provider, "find_issue_by_exact_summary", None)
    if callable(finder):
        return str(finder(summary, project_key=project_key, epic_key=epic_key, parent_key=parent_key) or "")
    return ""


def probe_existing(plan: dict[str, Any], provider: Any, project_key: str,
                   store: PlanRunStore | None = None) -> dict[str, str]:
    """`item_id → existing key` for every plan item that already exists.

    Run at preview time so the user sees "재사용" before clicking register.
    Subtasks are only probed under a parent that exists — a task that will be
    created fresh cannot have pre-existing subtasks — which keeps the number
    of Jira searches close to the number of tasks.
    """
    fp = plan_fingerprint(plan)
    found: dict[str, str] = {}
    for task in plan["tasks"]:
        tid = item_id("task", task["epic"], task["summary"])
        key = _existing_key(provider, store, fp, tid, task["summary"],
                            project_key, epic_key=task["epic"])
        if not key:
            continue
        found[tid] = key
        for sub in task.get("subtasks") or []:
            sid = item_id("subtask", tid, sub["summary"])
            sub_key = _existing_key(provider, store, fp, sid, sub["summary"],
                                    project_key, parent_key=key)
            if sub_key:
                found[sid] = sub_key
    return found


def register_plan(plan: dict[str, Any], provider: Any, project_key: str,
                  sprint_id: int | str | None = None,
                  log: Callable[[str], None] | None = None,
                  store: PlanRunStore | None = None,
                  existing: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Write a validated plan to Jira through *provider*. Returns one row per item.

    Order per task: task → done_note comment → each subtask (create, dates,
    done_note).  A failed task create skips its subtasks rather than
    orphaning them; the failure is reported in the returned list so the
    caller can show it next to the successes.  Items that already exist
    (from the run record or by exact summary in Jira) are reused and flagged
    ``reused: True`` — no second create, no second comment.
    """
    say = log or (lambda _m: None)
    created: list[dict[str, Any]] = []
    fp = plan_fingerprint(plan)

    def note(key: str, text: str) -> None:
        if text and not provider.add_comment(key, text):
            say(f"    ! 코멘트 실패 {key}: {getattr(provider, 'last_error', '')}")

    def remember(iid: str, key: str, summary: str) -> None:
        if store is not None:
            store.record(fp, iid, key, summary)

    for task in plan["tasks"]:
        tid = item_id("task", task["epic"], task["summary"])
        row: dict[str, Any] = {"key": "", "summary": task["summary"], "kind": "작업", "item_id": tid,
                               "start": task["start"], "end": task["end"],
                               "done_note": task.get("done_note", "")}
        key = _existing_key(provider, store, fp, tid, task["summary"], project_key,
                            epic_key=task["epic"], known=existing)
        if key:
            row.update(key=key, reused=True)
            remember(tid, key, task["summary"])
            say(f"작업 {key}  {task['summary'][:46]}  (기존)")
        else:
            error = ""
            try:
                key = provider.create_issue(
                    "task", task["summary"], task["description"], task["start"], task["end"],
                    task["epic"], project_key=project_key, report_required="yes")
            except Exception as exc:
                error = str(exc)
            if not key:
                row["error"] = error or getattr(provider, "last_error", "") or "이슈 생성 실패"
                say(f"작업 실패  {task['summary'][:46]}  {row['error']}")
                created.append(row)
                continue
            row["key"] = key
            remember(tid, key, task["summary"])
            say(f"작업 {key}  {task['summary'][:46]}")
            note(key, task.get("done_note", ""))
        created.append(row)

        for sub in task.get("subtasks") or []:
            sid = item_id("subtask", tid, sub["summary"])
            srow: dict[str, Any] = {"key": "", "summary": sub["summary"], "kind": "부작업", "item_id": sid,
                                    "parent": key, "start": sub.get("start", ""), "end": sub.get("end", ""),
                                    "done_note": sub.get("done_note", "")}
            sub_key = _existing_key(provider, store, fp, sid, sub["summary"], project_key,
                                    parent_key=key, known=existing)
            if sub_key:
                srow.update(key=sub_key, reused=True)
                remember(sid, sub_key, sub["summary"])
                say(f"  부작업 {sub_key}  {sub['summary'][:42]}  (기존)")
                created.append(srow)
                continue
            try:
                sub_key = create_subtask(provider, key, sub["summary"], sub.get("description", ""))
                error = "" if sub_key else "부작업 생성 실패"
            except Exception as exc:
                sub_key, error = "", str(exc)
            if not sub_key:
                srow["error"] = error
                say(f"  부작업 실패  {sub['summary'][:42]}  {error}")
                created.append(srow)
                continue
            srow["key"] = sub_key
            remember(sid, sub_key, sub["summary"])
            say(f"  부작업 {sub_key}  {sub['summary'][:42]}")
            if sub.get("start") and sub.get("end"):
                provider.update_dates(sub_key, sub["start"], sub["end"])
            note(sub_key, sub.get("done_note", ""))
            created.append(srow)

    # Sprint membership is idempotent on Jira's side, so re-adding reused keys is harmless.
    keys = [c["key"] for c in created if c["key"]]
    if sprint_id and keys:
        for i in range(0, len(keys), 50):
            provider._request("POST", f"/rest/agile/1.0/sprint/{sprint_id}/issue",
                              {"issues": keys[i:i + 50]})
        say(f"스프린트 {sprint_id} 에 {len(keys)}건 추가")
    return created


def status_for_dates(start: str, end: str, today: str) -> str:
    """Which status an item's period implies on *today* ('' = leave at 할 일).

    Mirrors the manual rule used on the board: a period that has ended is
    '종료 요청', one that contains today is '진행 중', a future one stays put.
    """
    if not start or not end:
        return ""
    if end < today:
        return "종료 요청"
    if start <= today <= end:
        return "진행 중"
    return ""


def apply_status_by_dates(created: list[dict[str, Any]], provider: Any, today: str,
                          log: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Move freshly created issues to the status their dates imply (opt-in).

    Jira's workflow here only allows 할 일 → 진행 중 → 종료 요청, so a finished
    item takes both steps; ``transition_issue`` is idempotent for issues that
    already moved, so reused items are safe.  The completion comment is the
    item's done_note, or a dated default so the audit trail never goes
    missing.  Each entry in *created* gets a ``status`` (target or '') and, on
    failure, ``status_error``.
    """
    say = log or (lambda _m: None)
    for item in created:
        key = item.get("key")
        target = status_for_dates(item.get("start", ""), item.get("end", ""), today)
        item["status"] = target
        if not key or not target:
            continue
        if item.get("reused"):
            # A previous run (or a human) already owns this issue's status; moving
            # it again would at best no-op and at worst post a second 종료 comment.
            item["status"] = ""
            item["status_kept"] = True
            continue
        ok = provider.transition_issue(key, "진행 중", "")
        if ok and target == "종료 요청":
            note = item.get("done_note") or f"{today} 종료 요청 — 계획 종료일({item['end']}) 경과"
            ok = provider.complete_issue(key, note)
        if ok:
            say(f"  상태 {key} → {target}")
        else:
            item["status_error"] = getattr(provider, "last_error", "") or "전환 실패"
            say(f"  ! 상태 전환 실패 {key}: {item['status_error']}")
    return created
