"""Focused regression tests for the evening auto commit/push recovery path."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import auto_commit_push


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout.strip()


def _make_repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "origin.git"
    worktree = tmp_path / "worktree"
    remote.mkdir()
    worktree.mkdir()
    _git(remote, "init", "--bare")
    _git(worktree, "init", "-b", "main")
    _git(worktree, "config", "user.name", "AutoReport Test")
    _git(worktree, "config", "user.email", "autoreport@example.invalid")
    (worktree / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(worktree, "add", "tracked.txt")
    _git(worktree, "commit", "-m", "initial")
    _git(worktree, "remote", "add", "origin", str(remote))
    _git(worktree, "push", "-u", "origin", "main")
    return worktree, remote


def _install_sidecar_hook(repo: Path, sidecar: Path, exit_code: int) -> None:
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    hook = repo / ".git" / "hooks" / "post-commit"
    quoted_sidecar = sidecar.as_posix().replace("'", "'\"'\"'")
    hook.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' '{exit_code}' > '{quoted_sidecar}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def test_clean_worktree_with_ahead_commit_is_pushed(tmp_path: Path) -> None:
    worktree, remote = _make_repo_with_origin(tmp_path)
    (worktree / "tracked.txt").write_text("initial\nlocal commit\n", encoding="utf-8")
    _git(worktree, "add", "tracked.txt")
    _git(worktree, "commit", "-m", "local only")
    local_head = _git(worktree, "rev-parse", "HEAD")

    assert _git(worktree, "status", "--short") == ""
    assert _git(worktree, "rev-list", "--count", "origin/main..HEAD") == "1"

    result = auto_commit_push.auto_commit_repo(
        worktree,
        "2026-08-11",
        "chore(auto): test",
    )

    assert result["status"] == "pushed"
    assert result["commit"] == local_head[:7]
    assert result["jira_sync_exit"] is None
    assert result["jira_sync_status"] == "not_run"
    assert _git(remote, "rev-parse", "refs/heads/main") == local_head


def test_push_failure_records_commit_and_clean_retry_pushes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, remote = _make_repo_with_origin(tmp_path)
    queue_root = tmp_path / "jira-queue"
    monkeypatch.setenv("AUTOREPORT_QUEUE_ROOT", str(queue_root))
    _install_sidecar_hook(
        worktree,
        queue_root / worktree.name / "post-commit-hook.last-exit",
        0,
    )
    missing_remote = tmp_path / "missing-origin.git"
    _git(worktree, "remote", "set-url", "origin", str(missing_remote))
    (worktree / "tracked.txt").write_text("initial\nneeds retry\n", encoding="utf-8")

    failed = auto_commit_push.auto_commit_repo(
        worktree,
        "2026-08-11",
        "chore(auto): test",
    )
    stranded_head = _git(worktree, "rev-parse", "HEAD")

    assert failed["status"] == "failed"
    assert failed["commit"] == stranded_head[:7]
    assert failed["error"]
    assert _git(worktree, "status", "--short") == ""

    _git(worktree, "remote", "set-url", "origin", str(remote))
    retried = auto_commit_push.auto_commit_repo(
        worktree,
        "2026-08-11",
        "chore(auto): test",
    )

    assert retried["status"] == "pushed"
    assert retried["commit"] == stranded_head[:7]
    assert _git(remote, "rev-parse", "refs/heads/main") == stranded_head


def test_missing_post_commit_sidecar_blocks_push_and_marks_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, remote = _make_repo_with_origin(tmp_path)
    queue_root = tmp_path / "jira-queue"
    monkeypatch.setenv("AUTOREPORT_QUEUE_ROOT", str(queue_root))
    (worktree / "tracked.txt").write_text("initial\nhook missing\n", encoding="utf-8")

    result = auto_commit_push.auto_commit_repo(
        worktree,
        "2026-08-11",
        "feat: require Jira evidence",
    )
    committed_head = _git(worktree, "rev-parse", "HEAD")
    sidecar = queue_root / worktree.name / "post-commit-hook.last-exit"

    assert result["status"] == "failed"
    assert result["commit"] == committed_head[:7]
    assert result["jira_sync_exit"] == 2
    assert result["jira_sync_status"] == "failed"
    assert "sidecar was not updated" in result["error"]
    assert sidecar.read_text(encoding="utf-8").strip() == "2"
    assert _git(worktree, "rev-list", "--count", "origin/main..HEAD") == "1"
    assert _git(remote, "rev-parse", "refs/heads/main") != committed_head


def test_jira_sync_failure_blocks_push_and_clean_retry_recovers_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, remote = _make_repo_with_origin(tmp_path)
    queue_root = tmp_path / "jira-queue"
    monkeypatch.setenv("AUTOREPORT_QUEUE_ROOT", str(queue_root))
    sidecar = queue_root / worktree.name / "post-commit-hook.last-exit"
    _install_sidecar_hook(worktree, sidecar, 2)
    (worktree / "tracked.txt").write_text("initial\nneeds Jira sync\n", encoding="utf-8")

    failed = auto_commit_push.auto_commit_repo(
        worktree,
        "2026-08-11",
        "feat: sync Jira evidence",
    )
    stranded_head = _git(worktree, "rev-parse", "HEAD")

    assert failed["status"] == "failed"
    assert failed["commit"] == stranded_head[:7]
    assert failed["jira_sync_exit"] == 2
    assert failed["jira_sync_status"] == "failed"
    assert _git(worktree, "rev-list", "--count", "origin/main..HEAD") == "1"
    assert _git(remote, "rev-parse", "refs/heads/main") != stranded_head

    worker_calls: list[tuple[Path, str]] = []

    def recovered_worker(repo_root: Path, commit_sha: str) -> subprocess.CompletedProcess[str]:
        worker_calls.append((repo_root, commit_sha))
        return subprocess.CompletedProcess([], 0, "proposal queued", "")

    monkeypatch.setattr(auto_commit_push, "run_jira_sync_worker", recovered_worker)
    retried = auto_commit_push.auto_commit_repo(
        worktree,
        "2026-08-11",
        "feat: sync Jira evidence",
    )

    assert worker_calls == [(worktree, stranded_head)]
    assert retried["status"] == "pushed"
    assert retried["commit"] == stranded_head[:7]
    assert retried["jira_sync_exit"] == 0
    assert retried["jira_sync_status"] == "succeeded"
    assert sidecar.read_text(encoding="utf-8").strip() == "0"
    assert _git(remote, "rev-parse", "refs/heads/main") == stranded_head


def test_jira_sync_review_required_preserves_commit_and_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, remote = _make_repo_with_origin(tmp_path)
    queue_root = tmp_path / "jira-queue"
    monkeypatch.setenv("AUTOREPORT_QUEUE_ROOT", str(queue_root))
    sidecar = queue_root / worktree.name / "post-commit-hook.last-exit"
    _install_sidecar_hook(worktree, sidecar, 3)
    (worktree / "tracked.txt").write_text("initial\nreview needed\n", encoding="utf-8")

    result = auto_commit_push.auto_commit_repo(
        worktree,
        "2026-08-11",
        "feat: prepare Jira review",
    )
    committed_head = _git(worktree, "rev-parse", "HEAD")

    assert result["status"] == "pushed"
    assert result["commit"] == committed_head[:7]
    assert result["jira_sync_exit"] == 3
    assert result["jira_sync_status"] == "review_required"
    assert _git(remote, "rev-parse", "refs/heads/main") == committed_head


def test_main_returns_nonzero_when_any_project_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        auto_commit_push,
        "parse_args",
        lambda: argparse.Namespace(
            config=str(tmp_path / "projects.json"),
            date="2026-08-11",
            message_prefix="chore(auto): test",
            dry_run=False,
        ),
    )
    monkeypatch.setattr(
        auto_commit_push,
        "load_projects",
        lambda _path: [{"name": "broken", "path": str(repo), "enabled": True}],
    )
    monkeypatch.setattr(auto_commit_push, "is_git_repo", lambda _path: True)
    monkeypatch.setattr(
        auto_commit_push,
        "auto_commit_repo",
        lambda *_args, **_kwargs: {
            "name": "broken",
            "path": str(repo),
            "branch": "main",
            "changed_files": 1,
            "status": "failed",
            "message": "자동 커밋/푸시 실패",
            "commit": "abc1234",
            "error": "push failed",
            "jira_sync_exit": 2,
            "jira_sync_status": "failed",
            "ran_at": "2026-08-11T17:00:00",
        },
    )
    monkeypatch.setattr(auto_commit_push, "WORKSPACE_ROOT", tmp_path)

    assert auto_commit_push.main() == 1


def test_render_html_exposes_jira_sync_status_and_exit() -> None:
    html = auto_commit_push.render_html(
        {
            "date": "2026-08-11",
            "projects": [
                {
                    "name": "sample",
                    "branch": "main",
                    "status": "pushed",
                    "changed_files": 1,
                    "commit": "abc1234",
                    "jira_sync_exit": 3,
                    "jira_sync_status": "review_required",
                    "message": "done",
                }
            ],
        }
    )

    assert "<th>Jira Sync</th>" in html
    assert "review_required (3)" in html
