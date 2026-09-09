#!/usr/bin/env python3
"""Register a reviewed Jira plan (tasks + subtasks) from a draft JSON file.

Why this exists: an ad-hoc script once registered 534 issues that nobody had
approved, and undoing it meant deleting them one by one.  So the approval gate
lives in the code here — the default run touches nothing, and a real write needs
both ``--apply`` and the reviewed draft path spelled out.

The dashboard's "계획 작성" modal and this CLI share one implementation
(``workflow.jira_plan_register``): same validation, same preview, same writes.

What it does NOT do unless asked:
  * no status transitions — ``--auto-status`` opts in to the date rule
    (ended period → 종료 요청 with a comment, current period → 진행 중)
  * comments only where the draft carries an explicit ``done_note``

Usage
-----
    python scripts/register_jira_plan.py --spec reports/.tmp_jira_draft_50.json
    python scripts/register_jira_plan.py --spec reports/.tmp_jira_draft_50.json --apply
    ... --apply --sprint 152          # also add every created issue to that sprint
    ... --apply --auto-status         # then move issues per their dates

Draft format: ``{"epics": {key: name}, "tasks": [{epic, summary, description,
start, end, done_note?, subtasks: [{summary, description, start, end, done_note?}]}]}``
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from workflow.jira_plan_register import (  # noqa: E402
    PlanRunStore, apply_status_by_dates, describe_plan, probe_existing, register_plan, validate_plan,
)

PROJECT_KEY = "APPL"


def load_spec(path: Path) -> dict:
    plan, errors = validate_plan(json.loads(path.read_text(encoding="utf-8")))
    if errors:
        raise SystemExit("초안 오류:\n  " + "\n  ".join(errors))
    return plan


def describe(plan: dict, existing: dict | None = None) -> None:
    d = describe_plan(plan, existing)
    print(f"등록 예정: 상위 {d['tasks']}건 · 부작업 {d['subtasks']}건 · 합계 {d['total']}건"
          f" (신규 {d['new']} · 기존 재사용 {d['reused']})")
    print(f"완료 코멘트: {d['notes']}건")
    for line in d["lines"]:
        mark = "  [코멘트]" if line["note"] else ""
        if line["existing_key"]:
            mark += f"  [기존 {line['existing_key']}]"
        if line["kind"] == "작업":
            print(f"  {line['epic']}  {line['start']}~{line['end']}  {line['summary']}{mark}")
        else:
            print(f"      └ {line['summary']}{mark}")


def _provider():
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    from workflow.task_provider import JiraApiTaskProvider
    return JiraApiTaskProvider(
        os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "",
        os.environ.get("JIRA_PAT") or os.environ.get("JIRA_TOKEN") or "",
        PROJECT_KEY,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="검토를 마친 Jira 계획을 등록합니다.")
    parser.add_argument("--spec", required=True, help="검토를 마친 초안 JSON 경로")
    parser.add_argument("--apply", action="store_true",
                        help="실제로 Jira 에 등록합니다. 없으면 예정 목록만 출력합니다.")
    parser.add_argument("--sprint", default=None, help="생성한 이슈를 넣을 스프린트 id (선택)")
    parser.add_argument("--auto-status", action="store_true",
                        help="등록 후 날짜 기준으로 상태 전환 (종료일 경과→종료 요청, 기간 중→진행 중)")
    parser.add_argument("--out", default=None, help="생성 결과를 저장할 JSON 경로 (선택)")
    parser.add_argument("--check-existing", action="store_true",
                        help="dry-run 에서도 Jira 를 읽어 이미 있는 항목을 표시 (읽기 전용)")
    args = parser.parse_args()

    plan = load_spec(Path(args.spec))
    if args.sprint and not str(args.sprint).isdigit():
        raise SystemExit("--sprint 는 숫자여야 합니다")
    # Same run record as the dashboard: re-running after a partial failure
    # reuses what already exists and creates only the gaps.
    store = PlanRunStore(REPO_ROOT / "reports" / "jira_plan_runs")
    existing: dict = {}
    provider = None
    if args.apply or args.check_existing:
        provider = _provider()
        existing = probe_existing(plan, provider, PROJECT_KEY, store)
    describe(plan, existing)
    if not args.apply:
        print("\n(dry-run — Jira 쓰기 없음. 실제 등록은 --apply 를 붙이세요.)")
        return 0

    created = register_plan(plan, provider, PROJECT_KEY, args.sprint, log=print, store=store, existing=existing)
    if args.auto_status:
        apply_status_by_dates(created, provider, date.today().isoformat(), log=print)

    ok = [c for c in created if c.get("key") and not c.get("status_error")]
    reused = sum(1 for c in created if c.get("reused"))
    print(f"\n반영 완료: {len(ok)}건 (기존 재사용 {reused}건) / 문제 {len(created) - len(ok)}건")
    if len(ok) != len(created):
        print("같은 초안으로 다시 --apply 하면 이미 만들어진 항목은 재사용되고 실패분만 생성됩니다.")
    out = Path(args.out) if args.out else REPO_ROOT / "reports" / "jira_registered.json"
    out.write_text(json.dumps(created, ensure_ascii=False, indent=1), encoding="utf-8")
    print("생성 목록:", out)
    return 0 if len(ok) == len(created) else 1


if __name__ == "__main__":
    raise SystemExit(main())
