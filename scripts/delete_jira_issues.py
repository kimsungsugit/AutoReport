#!/usr/bin/env python3
"""Delete a reviewed list of Jira issues (backup first). Dry-run unless --apply.

    python scripts/delete_jira_issues.py --keys APPL-993..APPL-1017
    python scripts/delete_jira_issues.py --keys APPL-993..APPL-1017 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def expand(spec: str) -> list[str]:
    keys: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        if ".." in part:
            a, b = part.split("..")
            proj, lo = a.rsplit("-", 1)
            hi = int(b.rsplit("-", 1)[1])
            keys += [f"{proj}-{n}" for n in range(int(lo), hi + 1)]
        elif part:
            keys.append(part)
    return keys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", required=True, help="APPL-1,APPL-2 or APPL-993..APPL-1017")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    from workflow.task_provider import JiraApiTaskProvider
    p = JiraApiTaskProvider(
        os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "",
        os.environ.get("JIRA_PAT") or os.environ.get("JIRA_TOKEN") or "", "APPL")

    keys = expand(args.keys)
    jql = urllib.parse.quote(f"key in ({','.join(keys)}) ORDER BY key")
    found = p._request("GET", f"/rest/api/2/search?jql={jql}&fields=*all&maxResults=200")["issues"]
    print(f"삭제 대상 {len(found)}건 (요청 {len(keys)}건)")
    for i in found:
        f = i["fields"]
        print(f"  {i['key']}  {f['issuetype']['name']}  {f['status']['name']}  {f['summary']}")
    if not args.apply:
        print("\n(dry-run — 삭제 없음. 실제 삭제는 --apply)")
        return 0

    backup = REPO_ROOT / "reports" / f"jira_backup_{date.today()}_delete.json"
    backup.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    print("백업:", backup)
    # subtasks first so a parent delete never cascades past what was listed
    order = sorted(found, key=lambda i: 0 if i["fields"].get("parent") else 1)
    done = 0
    for i in order:
        try:
            p._request("DELETE", f"/rest/api/2/issue/{i['key']}")
            done += 1
            print("  삭제", i["key"])
        except Exception as exc:
            print("  ! 실패", i["key"], exc)
    print(f"삭제 완료 {done}/{len(found)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
