"""Regression tests for the post-commit Jira proposal/apply queue."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import sync_commit_to_jira


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


def _make_commit(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "sample-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Jira Queue Test")
    _git(repo, "config", "user.email", "jira-queue@example.invalid")
    _git(repo, "remote", "add", "origin", "https://github.com/example/sample-repo.git")
    (repo / "src").mkdir()
    (repo / "docs").mkdir()
    (repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "docs" / "plan.md").write_text("# Plan\n", encoding="utf-8")
    _git(repo, "add", "src/feature.py", "docs/plan.md")
    body = (
        "문제: 커밋 결과와 계획의 Jira 추적이 누락됨\n\n"
        "실행 검증:\n- pytest tests/test_sync_commit_to_jira.py: 1 passed\n\n"
        "검증 환경:\n- Windows 11; Python 3.12\n\n"
        "남은 작업:\n- 사람의 승인 후 적용"
    )
    _git(repo, "commit", "-m", "feat(queue): APPL-501 커밋 증거 큐", "-m", body)
    return repo, _git(repo, "rev-parse", "HEAD"), body


def test_collect_commit_evidence_keeps_full_sha_body_and_files(tmp_path: Path) -> None:
    repo, sha, body = _make_commit(tmp_path)

    evidence = sync_commit_to_jira.collect_commit_evidence(repo)

    assert evidence["sha"] == sha
    assert len(evidence["sha"]) == 40
    assert evidence["parent_count"] == 0
    assert evidence["subject"] == "feat(queue): APPL-501 커밋 증거 큐"
    assert evidence["body"] == body
    assert evidence["files"] == ["docs/plan.md", "src/feature.py"]
    assert evidence["changed_files"] == evidence["files"]
    assert evidence["validation"] == [
        "pytest tests/test_sync_commit_to_jira.py: 1 passed"
    ]


def test_collect_commit_evidence_does_not_treat_unverified_prose_as_validation(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "validation-prose"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Jira Queue Test")
    _git(repo, "config", "user.email", "jira-queue@example.invalid")
    (repo / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(
        repo,
        "commit",
        "-m",
        "fix: close proxy write exposure",
        "-m",
        "미검증 요청이 쓰기로 이어질 수 있었다.\n\npytest tests/test_proxy.py: 3 passed",
    )

    evidence = sync_commit_to_jira.collect_commit_evidence(repo)

    assert evidence["validation"] == ["pytest tests/test_proxy.py: 3 passed"]


def test_plan_sections_require_heading_delimiter_and_end_at_blank_line() -> None:
    evidence = {
        "sha": "c" * 40,
        "subject": "test: keep validation evidence bounded",
        "body": (
            "Validation:\n"
            "- pytest focused passed\n\n"
            "tests/ 174 green\n"
            "- must not be captured\n"
            "Co-Authored-By: Reviewer <reviewer@example.invalid>\n"
            "Risks:\n"
            "- manual review remains"
        ),
    }

    plan = sync_commit_to_jira.plan_sections_from_commit(evidence)
    task = plan["tasks"][0]

    assert task["validation"] == ["pytest focused passed"]
    assert task["risks"] == ["manual review remains"]
    assert "tests/ 174 green" not in str(task)
    assert "Co-Authored-By" not in str(task)


def test_queue_commit_writes_atomic_review_only_proposal(tmp_path: Path) -> None:
    repo, sha, body = _make_commit(tmp_path)
    queue_dir = tmp_path / "queue"

    output_path, payload = sync_commit_to_jira.queue_commit(repo, output_dir=queue_dir)
    persisted = json.loads(output_path.read_text(encoding="utf-8"))

    assert output_path.name == f"sample-repo-{sha}.proposal.json"
    assert persisted == payload
    assert payload["mode"] == "review_only"
    assert payload["external_write_performed"] is False
    assert payload["auto_apply_supported"] is True
    assert payload["quality_gate"]["passed"] is False
    assert payload["quality_gate"]["commit_gate_passed"] is True
    assert payload["quality_gate"]["proposal_gate_passed"] is False
    assert "existing_jira_binding_suppresses_create" in payload["quality_gate"]["reasons"]
    assert payload["quality_gate"]["structured_plan_from_commit"] is True
    assert payload["commit"]["sha"] == sha
    assert payload["commit"]["body"] == body
    assert payload["commit"]["files"] == ["docs/plan.md", "src/feature.py"]
    assert len(payload["proposals"]) == 1
    proposal = payload["proposals"][0]
    assert proposal["action"] == "create_task"
    assert proposal["related_jira_key"] == "APPL-501"
    assert proposal["source_commits"][0]["sha"] == sha
    assert proposal["source_commits"][0]["url"] == (
        f"https://github.com/example/sample-repo/commit/{sha}"
    )
    assert not list(queue_dir.glob("*.tmp"))


def test_auto_apply_request_is_queued_but_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, _, _ = _make_commit(tmp_path)
    queue_dir = tmp_path / "queue"
    monkeypatch.setenv("JIRA_AUTO_APPLY", "1")

    exit_code = sync_commit_to_jira.main(
        ["--repo", str(repo), "--output-dir", str(queue_dir)]
    )

    assert exit_code == sync_commit_to_jira.EXIT_AUTO_APPLY_BLOCKED
    payload = json.loads(next(queue_dir.glob("*.proposal.json")).read_text(encoding="utf-8"))
    assert payload["mode"] == "review_only"
    assert payload["auto_apply_requested"] is True
    assert payload["external_write_performed"] is False
    assert "blocked" in capsys.readouterr().err.lower()


def test_project_config_sets_project_and_epic_on_proposal(tmp_path: Path) -> None:
    repo, _, _ = _make_commit(tmp_path)
    evidence = sync_commit_to_jira.collect_commit_evidence(repo)

    payload = sync_commit_to_jira.build_queue_payload(
        repo,
        evidence,
        {
            "name": "sample-repo",
            "path": str(repo),
            "enabled": True,
            "jira": {
                "project_key": "APPL",
                "epic_key": "APPL-42",
                "auto_plan": True,
            },
        },
    )

    assert payload["jira_auto_plan_enabled"] is True
    assert payload["proposals"][0]["project_key"] == "APPL"
    # Explicit APPL-501 in the commit takes binding precedence over the default Epic.
    assert payload["proposals"][0]["related_jira_key"] == "APPL-501"


@pytest.mark.parametrize(
    ("subject", "parent_count"),
    [
        ("chore(auto): end-of-day snapshot 2026-08-11", 1),
        ("chore: auto commit 2026-08-11", 1),
        ("snapshot 2026-08-11", 1),
        ("docs: nightly snapshot", 1),
        ("Merge branch 'main'", 2),
        ("chore: merge main", 1),
        ("WIP parser changes", 1),
        ("[WIP] parser changes", 1),
        ("chore: tmp", 1),
        ("temporary checkpoint", 1),
        ("feat: combine release histories", 2),
    ],
)
def test_generic_auto_commit_fails_quality_gate(
    subject: str,
    parent_count: int,
) -> None:
    evidence = {
        "sha": "a" * 40,
        "subject": subject,
        "body": "",
        "parent_count": parent_count,
        "files": ["src/app.py"],
        "changed_files": ["src/app.py"],
        "validation": [],
    }

    # No repository I/O occurs in this quality assertion; optional git metadata
    # lookups simply resolve to empty strings for the workspace repo.
    payload = sync_commit_to_jira.build_queue_payload(Path.cwd(), evidence, {})

    assert payload["quality_gate"]["passed"] is False
    assert "generic or non-actionable commit subject" in payload["quality_gate"]["reasons"]
    assert payload["proposals"] == []


@pytest.mark.parametrize(
    "subject",
    [
        "fix(sync): retry timed-out Jira proposal delivery",
        "feat: add snapshot export endpoint",
    ],
)
def test_meaningful_commit_without_structured_body_still_builds_plan(subject: str) -> None:
    evidence = {
        "sha": "b" * 40,
        "subject": subject,
        "body": "",
        "parent_count": 1,
        "files": ["src/sync.py"],
        "changed_files": ["src/sync.py"],
        "validation": [],
    }

    payload = sync_commit_to_jira.build_queue_payload(Path.cwd(), evidence, {})

    assert payload["quality_gate"]["passed"] is False
    assert payload["quality_gate"]["commit_gate_passed"] is True
    assert payload["quality_gate"]["proposal_gate_passed"] is False
    assert payload["quality_gate"]["structured_plan_from_commit"] is False
    assert len(payload["proposals"]) == 1
    assert payload["proposals"][0]["summary"]
    assert payload["proposals"][0]["source_commits"][0]["subject"] == subject
    assert payload["proposals"][0]["plan_source"] == "commit_evidence"
    assert payload["proposals"][0]["evidence_type"] == "commit_backed"
    assert payload["proposals"][0]["auto_apply_eligible"] is False


def test_structured_commit_with_complete_quality_evidence_is_auto_apply_eligible() -> None:
    sha = "d" * 40
    evidence = {
        "sha": sha,
        "subject": "feat(quality): Jira 제안 품질 게이트 추가",
        "body": (
            "문제:\n- 품질 근거가 없는 제안이 승인 후보로 표시됨\n\n"
            "목표 결과:\n- 품질 필수조건을 통과한 제안만 승인 가능\n\n"
            "범위:\n- workflow/jira_planning.py 품질 판정\n\n"
            "완료 조건:\n"
            "- 동일 입력을 2회 평가해 품질 점수 차이가 0점인지 확인한다.\n"
            "- 품질 미달 입력 3건이 모두 자동 적용 차단되는지 확인한다.\n\n"
            "실행 검증:\n- pytest tests/test_jira_planning.py: 36 passed\n\n"
            "검증 환경:\n- Windows 11; Python 3.12\n\n"
            "남은 작업:\n- 섀도 운영 표본을 수집한다.\n\n"
            "리스크:\n- workflow/jira_planning.py 규칙 변경으로 기존 제안 점수가 달라질 수 있다."
        ),
        "parent_count": 1,
        "files": ["workflow/jira_planning.py", "tests/test_jira_planning.py"],
        "changed_files": ["workflow/jira_planning.py", "tests/test_jira_planning.py"],
        "validation": ["pytest tests/test_jira_planning.py: 36 passed"],
        "executed_validation": [{
            "command": "pytest tests/test_jira_planning.py",
            "environment": "Windows 11; Python 3.12",
            "actual_result": "36 passed",
            "source": f"commit:{sha}",
        }],
        "verification_plan": [{
            "command": "pytest tests/test_jira_planning.py",
            "environment": "Windows 11; Python 3.12",
            "expected_result": "실패 0건",
            "pass_criteria": "전체 테스트 통과",
            "status": "not_run",
        }],
        "validation_environment": ["Windows 11; Python 3.12"],
    }

    payload = sync_commit_to_jira.build_queue_payload(
        Path.cwd(),
        evidence,
        {
            "name": "AutoReport",
            "enabled": True,
            "jira": {
                "project_key": "APPL",
                "epic_key": "APPL-500",
                "auto_plan": True,
            },
        },
    )

    proposal = payload["proposals"][0]
    assert payload["quality_gate"]["passed"] is True
    assert proposal["quality_score"] >= 85
    assert proposal["quality_grade"] == "high"
    assert proposal["auto_apply_eligible"] is True
    assert proposal["executed_validation"][0]["actual_result"] == "36 passed"


def test_sync_error_is_nonzero(tmp_path: Path) -> None:
    missing_repo = tmp_path / "missing"

    assert sync_commit_to_jira.main(["--repo", str(missing_repo)]) == sync_commit_to_jira.EXIT_ERROR


def test_post_commit_hook_logs_worker_failure_but_returns_success(tmp_path: Path) -> None:
    repo, _, _ = _make_commit(tmp_path)
    hook = Path(__file__).resolve().parent.parent / ".githooks" / "post-commit"
    _git(repo, "config", "--local", "core.hooksPath", hook.parent.as_posix())
    (repo / "hook-trigger.txt").write_text("trigger\n", encoding="utf-8")
    _git(repo, "add", "hook-trigger.txt")
    missing_script = tmp_path / "missing-sync-script.py"
    env = os.environ.copy()
    env["AUTOREPORT_PYTHON"] = sys.executable
    env["AUTOREPORT_SYNC_SCRIPT"] = str(missing_script)
    env["AUTOREPORT_QUEUE_ROOT"] = str(tmp_path / "central-queue")

    proc = subprocess.run(
        ["git", "commit", "-m", "test: hook failure is non-fatal"],
        cwd=repo,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )

    queue_dir = tmp_path / "central-queue" / "sample-repo"
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert (queue_dir / "post-commit-hook.last-exit").read_text(encoding="utf-8").strip() == "2"
    log = (queue_dir / "post-commit-hook.log").read_text(encoding="utf-8")
    assert "[ERROR]" in log
    assert "worker_exit=2" in log


def test_auto_apply_success_path_rewrites_persisted_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, _ = _make_commit(tmp_path)
    queue_dir = tmp_path / "queue"
    config_path = tmp_path / "projects.json"
    config_path.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "sample-repo",
                        "path": str(repo),
                        "enabled": True,
                        "jira": {"project_key": "APPL", "auto_plan": True},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JIRA_AUTO_APPLY", "1")

    def fake_apply(payload: dict, project_config: dict):
        assert project_config["jira"]["project_key"] == "APPL"
        payload["mode"] = "applied"
        payload["external_write_performed"] = True
        payload["apply_results"] = [
            {"proposal_id": payload["proposals"][0]["id"], "status": "applied", "created": True}
        ]
        payload["review"] = {"required": False, "reason": "test applied"}
        return payload, True

    monkeypatch.setattr(sync_commit_to_jira, "apply_queued_proposals", fake_apply)

    exit_code = sync_commit_to_jira.main(
        [
            "--repo",
            str(repo),
            "--output-dir",
            str(queue_dir),
            "--config",
            str(config_path),
        ]
    )

    assert exit_code == sync_commit_to_jira.EXIT_OK
    payload = json.loads(next(queue_dir.glob("*.proposal.json")).read_text(encoding="utf-8"))
    assert payload["mode"] == "applied"
    assert payload["external_write_performed"] is True


def test_installer_configures_tracked_hook_path_on_target_repo(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("PowerShell is required to exercise the hook installer")
    repo, _, _ = _make_commit(tmp_path)
    installer = Path(__file__).resolve().parent.parent / "scripts" / "install_git_hooks.ps1"

    proc = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
            "-TargetRepo",
            str(repo),
        ],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
    configured = _git(repo, "config", "--local", "--get", "core.hooksPath")
    expected = (Path(__file__).resolve().parent.parent / ".githooks").as_posix()
    assert configured == expected


def test_installer_preserves_existing_hook_path_and_chains_post_commit(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("PowerShell is required to exercise the hook installer")
    repo, _, _ = _make_commit(tmp_path)
    existing_hooks = repo / ".existing-hooks"
    existing_hooks.mkdir()
    (existing_hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _git(repo, "config", "--local", "core.hooksPath", ".existing-hooks")
    installer = Path(__file__).resolve().parent.parent / "scripts" / "install_git_hooks.ps1"

    proc = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
            "-TargetRepo",
            str(repo),
        ],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _git(repo, "config", "--local", "--get", "core.hooksPath") == ".existing-hooks"
    assert (existing_hooks / "pre-commit").read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"
    wrapper = (existing_hooks / "post-commit").read_text(encoding="utf-8")
    assert "AutoReport chained post-commit hook" in wrapper
    assert "/.githooks/post-commit" in wrapper


def test_installer_refuses_to_overwrite_existing_post_commit(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("PowerShell is required to exercise the hook installer")
    repo, _, _ = _make_commit(tmp_path)
    existing_hooks = repo / ".existing-hooks"
    existing_hooks.mkdir()
    original = "#!/bin/sh\nprintf 'custom hook\\n'\n"
    (existing_hooks / "post-commit").write_text(original, encoding="utf-8")
    _git(repo, "config", "--local", "core.hooksPath", ".existing-hooks")
    installer = Path(__file__).resolve().parent.parent / "scripts" / "install_git_hooks.ps1"

    proc = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
            "-TargetRepo",
            str(repo),
        ],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert proc.returncode != 0
    assert (existing_hooks / "post-commit").read_text(encoding="utf-8") == original


def test_commit_body_headings_carry_purpose_exclusions_and_a_breakdown():
    """The hook path renders the same reviewer-facing sections as the report path.

    A developer who writes 목적/제외 범위/서브작업 in the commit body should get a
    review card that already reads like a ticket, instead of one where those
    lines were parsed into nothing.
    """
    evidence = {
        "sha": "b" * 40,
        "subject": "feat(tara): ISO 21434 커버리지 매트릭스 최신화",
        "body": (
            "목적: 심사 시점에 조항별 갭을 즉시 확인할 수 있게 한다.\n"
            "문제: 현행 매트릭스는 Clause 15 계열 7단계만 판정한다.\n"
            "범위:\n"
            "- Clause 5~15 RQ/WP 원장 재정비\n"
            "제외 범위:\n"
            "- 도출된 갭의 해소\n"
            "서브작업:\n"
            "- 규범 요구사항 기준 원장 정비\n"
            "- 산출물·근거 인벤토리 매핑\n"
        ),
        "changed_files": ["GUI/tara_simulator.py"],
    }

    sections = sync_commit_to_jira.plan_sections_from_commit(evidence)
    task = sections["tasks"][0]

    assert task["purpose"] == ["심사 시점에 조항별 갭을 즉시 확인할 수 있게 한다."]
    assert task["out_of_scope"] == ["도출된 갭의 해소"]
    assert task["subtasks"] == ["규범 요구사항 기준 원장 정비", "산출물·근거 인벤토리 매핑"]
    # "제외 범위" must not be swallowed by the anchored "범위" heading.
    assert task["scope"] == ["Clause 5~15 RQ/WP 원장 재정비"]

    from workflow.jira_planning import build_create_task_proposals

    proposal = build_create_task_proposals(
        [evidence], sections, default_project_key="APPL"
    )[0]
    body = sync_commit_to_jira._jira_description(proposal)

    assert body.startswith("h2. 목적\n심사 시점에")
    assert "h2. 제외 범위\n* 도출된 갭의 해소" in body
    assert "h2. 서브작업\n* *1. 규범 요구사항 기준 원장 정비*\n* *2. 산출물·근거 인벤토리 매핑*" in body
