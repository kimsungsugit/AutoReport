from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent

sys.path.insert(0, str(WORKSPACE_ROOT))
from scripts.design_system import DESIGN_CSS


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


def commits_ahead_of_origin(repo_root: Path, branch: str) -> int:
    """Return commits in HEAD that are not yet on origin/<branch>.

    A clean worktree does not imply that the branch is synchronized.  The evening
    job can successfully create a commit and then fail while pushing it; on the
    next run that repository is clean but still needs a push.
    """
    proc = run_git(
        repo_root,
        ["rev-list", "--count", f"origin/{branch}..HEAD"],
        check=False,
    )
    if proc.returncode != 0:
        return 0
    try:
        return max(0, int(proc.stdout.strip()))
    except ValueError:
        return 0


def jira_sync_sidecar_path(repo_root: Path) -> Path:
    """Return the sidecar written by the tracked post-commit hook."""
    queue_root = Path(
        os.environ.get("AUTOREPORT_QUEUE_ROOT")
        or (WORKSPACE_ROOT / "reports" / "jira_queue")
    )
    safe_repo = re.sub(r"[^A-Za-z0-9._-]", "", repo_root.name) or "repository"
    return queue_root / safe_repo / "post-commit-hook.last-exit"


def jira_sync_sidecar_fingerprint(repo_root: Path) -> tuple[int, int] | None:
    try:
        stat = jira_sync_sidecar_path(repo_root).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def read_jira_sync_exit(repo_root: Path) -> int | None:
    sidecar = jira_sync_sidecar_path(repo_root)
    try:
        return int(sidecar.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # A corrupt/unreadable completion marker is not safe to treat as success.
        return 2


def write_jira_sync_exit(repo_root: Path, exit_code: int) -> None:
    sidecar = jira_sync_sidecar_path(repo_root)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    temporary = sidecar.with_name(f".{sidecar.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(f"{exit_code}\n", encoding="utf-8")
        os.replace(temporary, sidecar)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def jira_sync_status(exit_code: int | None) -> str:
    if exit_code is None:
        return "not_run"
    if exit_code == 0:
        return "succeeded"
    if exit_code == 3:
        return "review_required"
    return "failed"


def run_jira_sync_worker(repo_root: Path, commit_sha: str) -> subprocess.CompletedProcess[str]:
    """Retry the same worker used by the hook for a stranded HEAD commit."""
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "sync_commit_to_jira.py"),
            "--repo",
            str(repo_root),
            "--commit",
            commit_sha,
        ],
        cwd=WORKSPACE_ROOT,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def record_jira_sync(result: dict[str, Any], exit_code: int | None) -> None:
    result["jira_sync_exit"] = exit_code
    result["jira_sync_status"] = jira_sync_status(exit_code)


def auto_commit_repo(repo_root: Path, run_day: str, message_prefix: str, dry_run: bool = False) -> dict[str, Any]:
    status_lines = collect_status_lines(repo_root)
    branch = current_branch(repo_root)
    ahead_count = commits_ahead_of_origin(repo_root, branch)
    sidecar_before_commit = jira_sync_sidecar_fingerprint(repo_root)
    result: dict[str, Any] = {
        "name": repo_root.name,
        "path": str(repo_root),
        "branch": branch,
        "changed_files": len(status_lines),
        "status": "no_changes",
        "message": "변경 없음",
        "commit": "",
        "error": "",
        "jira_sync_exit": None,
        "jira_sync_status": "not_run",
        "ran_at": datetime.now().isoformat(timespec="seconds"),
    }

    try:
        if not status_lines:
            previous_sync_exit = read_jira_sync_exit(repo_root)
            record_jira_sync(result, previous_sync_exit)
            if not dry_run and jira_sync_status(previous_sync_exit) == "failed":
                head_sha = run_git(repo_root, ["rev-parse", "HEAD"]).stdout.strip()
                result["commit"] = head_sha[:7]
                sync_proc = run_jira_sync_worker(repo_root, head_sha)
                write_jira_sync_exit(repo_root, sync_proc.returncode)
                record_jira_sync(result, sync_proc.returncode)
                if result["jira_sync_status"] == "failed":
                    result["status"] = "failed"
                    result["message"] = "Jira commit sync retry failed"
                    result["error"] = (
                        sync_proc.stderr.strip()
                        or sync_proc.stdout.strip()
                        or f"Jira sync worker exited {sync_proc.returncode}"
                    )
                    return result

            if ahead_count == 0:
                return result

        if dry_run:
            result["status"] = "dry_run"
            result["message"] = (
                f"미푸시 커밋 {ahead_count}건 푸시 대상"
                if not status_lines
                else "자동 커밋/푸시 대상 점검 완료"
            )
            if ahead_count:
                result["commit"] = run_git(repo_root, ["rev-parse", "--short", "HEAD"]).stdout.strip()
            return result

        commit_proc: subprocess.CompletedProcess[str] | None = None
        if status_lines:
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
                if ahead_count == 0:
                    result["status"] = "no_staged_changes"
                    result["message"] = "스테이징 후 커밋 대상 없음"
                    return result
            else:
                commit_message = f"{message_prefix} {run_day}"
                commit_proc = run_git(repo_root, ["commit", "-m", commit_message])

        # Record HEAD before attempting the push.  If the push fails, this hash is
        # the durable recovery handle and the next clean-worktree run can retry it.
        commit_hash = run_git(repo_root, ["rev-parse", "--short", "HEAD"]).stdout.strip()
        result["commit"] = commit_hash
        if commit_proc is not None:
            sidecar_after_commit = jira_sync_sidecar_fingerprint(repo_root)
            if sidecar_after_commit == sidecar_before_commit:
                write_jira_sync_exit(repo_root, 2)
                record_jira_sync(result, 2)
                result["status"] = "failed"
                result["message"] = "Jira commit sync failed"
                result["error"] = "post-commit Jira sync sidecar was not updated"
                return result
            record_jira_sync(result, read_jira_sync_exit(repo_root))
            if result["jira_sync_status"] == "failed":
                result["status"] = "failed"
                result["message"] = "Jira commit sync failed"
                result["error"] = (
                    f"post-commit Jira sync exited {result['jira_sync_exit']}"
                )
                return result
        push_proc = run_git(repo_root, ["push", "origin", branch])
        result["status"] = "pushed"
        result["message"] = (
            push_proc.stdout.strip()
            or (commit_proc.stdout.strip() if commit_proc else "")
            or "자동 커밋/푸시 완료"
        )
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
        jira_status = str(item.get("jira_sync_status") or "not_run")
        jira_exit = item.get("jira_sync_exit")
        jira_text = jira_status if jira_exit is None else f"{jira_status} ({jira_exit})"
        rows.append(
            f"""
<tr>
  <td>{item.get("name","")}</td>
  <td>{item.get("branch","")}</td>
  <td class="{cls}">{status}</td>
  <td>{item.get("changed_files",0)}</td>
  <td>{item.get("commit","-") or "-"}</td>
  <td>{jira_text}</td>
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
{DESIGN_CSS}
    .ok {{ color:var(--ok-ink); font-weight:700; }}
    .warn {{ color:var(--warn-ink); font-weight:700; }}
    .fail {{ color:var(--danger-ink); font-weight:700; }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>17:00 자동 Commit / Push 상태</h1>
      <p>{payload.get("date","")} 기준 자동화 실행 결과</p>
    </section>
    <div class="actions">
      {f'<a class="btn" href="{retry_link}">Retry Auto Commit/Push</a>' if retry_link else ''}
    </div>
    <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Project</th>
          <th>Branch</th>
          <th>Status</th>
          <th>Changed</th>
          <th>Commit</th>
          <th>Jira Sync</th>
          <th>Message</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    </div>
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
                    "jira_sync_exit": None,
                    "jira_sync_status": "not_run",
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
                    "jira_sync_exit": None,
                    "jira_sync_status": "not_run",
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
    return 1 if any(item.get("status") == "failed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
