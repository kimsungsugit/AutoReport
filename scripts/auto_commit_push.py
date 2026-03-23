from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto commit and push configured repositories.")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "startup_projects.json"))
    parser.add_argument("--date", default=None, help="Reference date YYYY-MM-DD")
    parser.add_argument("--message-prefix", default="chore(auto): end-of-day snapshot")
    parser.add_argument("--dry-run", action="store_true", help="Inspect what would be committed without making changes.")
    return parser.parse_args()


def run_git(repo_root: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-c", f"safe.directory={repo_root}", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise RuntimeError(message)
    return proc


def load_projects(config_path: Path) -> list[dict[str, Any]]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return [item for item in (data.get("projects") or []) if isinstance(item, dict) and item.get("enabled", True)]


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def collect_status_lines(repo_root: Path) -> list[str]:
    proc = run_git(repo_root, ["status", "--short"], check=False)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def current_branch(repo_root: Path) -> str:
    proc = run_git(repo_root, ["branch", "--show-current"], check=False)
    return proc.stdout.strip() or "main"


def auto_commit_repo(repo_root: Path, run_day: str, message_prefix: str, dry_run: bool = False) -> dict[str, Any]:
    status_lines = collect_status_lines(repo_root)
    branch = current_branch(repo_root)
    result: dict[str, Any] = {
        "name": repo_root.name,
        "path": str(repo_root),
        "branch": branch,
        "changed_files": len(status_lines),
        "status": "no_changes",
        "message": "변경 없음",
        "commit": "",
        "error": "",
        "ran_at": datetime.now().isoformat(timespec="seconds"),
    }
    if not status_lines:
        return result

    if dry_run:
        result["status"] = "dry_run"
        result["message"] = "자동 커밋/푸시 대상 점검 완료"
        return result

    try:
        run_git(repo_root, ["add", "-A"])
        staged_check = subprocess.run(
            ["git", "-c", f"safe.directory={repo_root}", "diff", "--cached", "--quiet"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if staged_check.returncode == 0:
            result["status"] = "no_staged_changes"
            result["message"] = "스테이징 후 커밋 대상 없음"
            return result

        commit_message = f"{message_prefix} {run_day}"
        commit_proc = run_git(repo_root, ["commit", "-m", commit_message])
        commit_hash = run_git(repo_root, ["rev-parse", "--short", "HEAD"]).stdout.strip()
        push_proc = run_git(repo_root, ["push", "origin", branch])
        result["status"] = "pushed"
        result["message"] = push_proc.stdout.strip() or commit_proc.stdout.strip() or "자동 커밋/푸시 완료"
        result["commit"] = commit_hash
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["message"] = "자동 커밋/푸시 실패"
        result["error"] = str(exc)
        return result


def render_html(payload: dict[str, Any]) -> str:
    retry_cmd = WORKSPACE_ROOT / "scripts" / "retry_evening_auto_commit_push.cmd"
    retry_link = retry_cmd.as_uri() if retry_cmd.exists() else ""
    rows = []
    for item in payload.get("projects") or []:
        status = str(item.get("status") or "")
        cls = "ok" if status == "pushed" else ("warn" if status in {"no_changes", "no_staged_changes"} else "fail")
        rows.append(
            f"""
<tr>
  <td>{item.get("name","")}</td>
  <td>{item.get("branch","")}</td>
  <td class="{cls}">{status}</td>
  <td>{item.get("changed_files",0)}</td>
  <td>{item.get("commit","-") or "-"}</td>
  <td>{item.get("message","")}</td>
</tr>
"""
        )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Auto Commit Push Status {payload.get("date","")}</title>
  <style>
    body {{ font-family:"Segoe UI","Noto Sans KR",sans-serif; margin:0; background:#f6f1e8; color:#17212b; }}
    .wrap {{ max-width:1100px; margin:0 auto; padding:28px; }}
    .hero {{ background:linear-gradient(135deg,#12343b,#2c6e63); color:#fff; padding:24px; border-radius:24px; margin-bottom:18px; }}
    table {{ width:100%; border-collapse:collapse; background:#fffdf9; border:1px solid #ddd2c1; border-radius:18px; overflow:hidden; }}
    th, td {{ padding:12px 10px; border-bottom:1px solid #eee3d2; text-align:left; vertical-align:top; }}
    th {{ background:#f8f1e4; font-size:12px; text-transform:uppercase; letter-spacing:.06em; }}
    .ok {{ color:#166534; font-weight:700; }}
    .warn {{ color:#92400e; font-weight:700; }}
    .fail {{ color:#991b1b; font-weight:700; }}
    .actions {{ margin:16px 0 18px; display:flex; gap:10px; flex-wrap:wrap; }}
    .button {{ display:inline-flex; text-decoration:none; color:#0f4c5c; font-weight:700; background:#edf6f9; border:1px solid #c8d9dd; padding:11px 15px; border-radius:999px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>17:00 자동 Commit / Push 상태</h1>
      <p>{payload.get("date","")} 기준 자동화 실행 결과</p>
    </section>
    <div class="actions">
      {f'<a class="button" href="{retry_link}">Retry Auto Commit/Push</a>' if retry_link else ''}
    </div>
    <table>
      <thead>
        <tr>
          <th>Project</th>
          <th>Branch</th>
          <th>Status</th>
          <th>Changed</th>
          <th>Commit</th>
          <th>Message</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
  </div>
</body>
</html>"""


def main() -> int:
    args = parse_args()
    run_day = args.date or date.today().isoformat()
    projects = load_projects(Path(args.config))
    results = []
    for project in projects:
        repo_path = Path(str(project.get("path") or "")).resolve()
        if not repo_path.exists():
            results.append(
                {
                    "name": str(project.get("name") or repo_path.name),
                    "path": str(repo_path),
                    "branch": "",
                    "changed_files": 0,
                    "status": "skipped",
                    "message": "경로 없음",
                    "commit": "",
                    "error": "",
                    "ran_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            continue
        if not is_git_repo(repo_path):
            results.append(
                {
                    "name": str(project.get("name") or repo_path.name),
                    "path": str(repo_path),
                    "branch": "",
                    "changed_files": 0,
                    "status": "skipped",
                    "message": "Git 저장소 아님",
                    "commit": "",
                    "error": "",
                    "ran_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            continue
        results.append(auto_commit_repo(repo_path, run_day, args.message_prefix, dry_run=args.dry_run))

    payload = {
        "date": run_day,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "projects": results,
    }
    output_dir = WORKSPACE_ROOT / "reports" / "automation_status"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{run_day}-auto-commit-push.json"
    html_path = output_dir / f"{run_day}-auto-commit-push.html"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")

    print("Generated automation status:")
    print(json_path)
    print(html_path)
    for item in results:
        print(f"{item['name']}: {item['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
