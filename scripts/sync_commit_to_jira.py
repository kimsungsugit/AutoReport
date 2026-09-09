"""Queue one Git commit as a plan and optionally create its Jira Task once.

The proposal is always persisted before any Jira mutation.  Live apply is opt-in
(``JIRA_AUTO_APPLY=1``) and goes through ``JiraApplyService`` so a durable outbox
and a Jira-side proposal label can reconcile crashes, timeouts, and retries.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
AUTOREPORT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(AUTOREPORT_ROOT))

from workflow.jira_planning import (
    assess_proposal_quality,
    build_create_task_proposals,
    enrich_proposal_quality,
    parse_validation_evidence,
)


EXIT_OK = 0
EXIT_ERROR = 2
EXIT_AUTO_APPLY_BLOCKED = 3

_EXECUTABLE_VALIDATION_COMMAND_RE = re.compile(
    r"(?i)(?:^|[:：]\s*|[`'\"])(?:"
    r"(?:python\s+-m\s+)?pytest\b|python\s+-m\s+unittest\b|"
    r"python\s+-m\s+compileall\b|npm(?:\.cmd)?\s+(?:run\s+)?test\b|"
    r"dotnet\s+test\b|node\s+--check\b|ruff\b|mypy\b|"
    r"cargo\s+test\b|go\s+test\b|mvn\s+test\b|gradle(?:w)?\s+test\b)"
)


def _load_environment() -> None:
    """Load repo-local secrets without overriding an explicit hook environment."""
    try:
        from dotenv import load_dotenv

        load_dotenv(AUTOREPORT_ROOT / ".env", override=False)
    except ImportError:
        # Keep the Git hook functional under a minimal system Python. This parser
        # intentionally supports only plain KEY=VALUE lines; shell expansion and
        # command substitution are never evaluated.
        env_path = AUTOREPORT_ROOT / ".env"
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Queue a commit as review-only Jira create-task proposals."
    )
    parser.add_argument("--repo", default=".", help="Git repository to inspect.")
    parser.add_argument("--commit", default="HEAD", help="Commit-ish to queue (default: HEAD).")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Queue directory (default: AutoReport/reports/jira_queue/<repo>).",
    )
    parser.add_argument(
        "--config",
        default=str(SCRIPT_DIR / "startup_projects.json"),
        help="Project/Jira configuration file.",
    )
    return parser.parse_args(argv)


def _run_git(repo_root: Path, args: Sequence[str], *, check: bool = True) -> str:
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
        detail = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise RuntimeError(f"git {' '.join(args)}: {detail}")
    return proc.stdout


def resolve_repo_root(repo: Path) -> Path:
    candidate = repo.resolve()
    root = _run_git(candidate, ["rev-parse", "--show-toplevel"]).strip()
    if not root:
        raise RuntimeError(f"Git repository root not found: {candidate}")
    return Path(root).resolve()


def collect_commit_evidence(repo_root: Path, commitish: str = "HEAD") -> dict[str, Any]:
    """Collect lossless evidence for one commit using its full object id."""
    sha = _run_git(repo_root, ["rev-parse", "--verify", f"{commitish}^{{commit}}"]).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
        raise RuntimeError(f"Unexpected commit id returned by Git: {sha!r}")

    subject = _run_git(repo_root, ["show", "-s", "--format=%s", sha]).rstrip("\r\n")
    body = _run_git(repo_root, ["show", "-s", "--format=%b", sha]).rstrip("\r\n")
    author = _run_git(repo_root, ["show", "-s", "--format=%an <%ae>", sha]).rstrip("\r\n")
    authored_at = _run_git(repo_root, ["show", "-s", "--format=%aI", sha]).strip()
    parents = _run_git(repo_root, ["rev-list", "--parents", "-n", "1", sha]).split()
    parent_count = max(0, len(parents) - 1)
    changed_raw = _run_git(
        repo_root,
        ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", sha],
    )
    files = sorted({item for item in changed_raw.split("\0") if item})
    body_lines = [
        re.sub(r"^[\s*+-]+", "", line).strip()
        for line in body.splitlines()
        if line.strip()
    ]
    validation = [
        line
        for line in body_lines
        if _EXECUTABLE_VALIDATION_COMMAND_RE.search(line)
    ][:12]
    executed_validation, verification_plan = parse_validation_evidence(
        validation,
        source=f"commit:{sha}",
    )
    return {
        "sha": sha,
        "subject": subject,
        "body": body,
        "author": author,
        "authored_at": authored_at,
        "parent_count": parent_count,
        "files": files,
        "changed_files": files,
        "validation": validation,
        "executed_validation": executed_validation,
        "verification_plan": verification_plan,
    }


def _plan_heading_pattern(*aliases: str) -> re.Pattern[str]:
    alternatives = "|".join(
        re.escape(alias) for alias in sorted(aliases, key=len, reverse=True)
    )
    # A section name is a heading only when it occupies the whole line or uses
    # an explicit ASCII/full-width colon. This keeps evidence such as
    # ``tests/ 174 green`` from opening a validation section.
    return re.compile(
        rf"^(?:{alternatives})(?:\s*[:\uff1a]\s*(.*)|\s*)$",
        re.IGNORECASE,
    )


_PLAN_HEADINGS = {
    "problem": _plan_heading_pattern("\ubb38\uc81c", "\ubc30\uacbd", "problem", "context"),
    "outcome": _plan_heading_pattern(
        "\ubaa9\ud45c \uacb0\uacfc", "\ubaa9\ud45c", "\uacb0\uacfc", "outcome", "goal"
    ),
    # "\ubaa9\uc801" only \u2014 "goal" stays with outcome, and _PLAN_HEADINGS breaks on
    # the first matching pattern, so sharing an alias would shadow a section.
    "purpose": _plan_heading_pattern("\ubaa9\uc801", "purpose", "objective"),
    "scope": _plan_heading_pattern("\ubc94\uc704", "scope"),
    # Anchored patterns, so "\uc81c\uc678 \ubc94\uc704" never matches the scope heading.
    "out_of_scope": _plan_heading_pattern(
        "\uc81c\uc678 \ubc94\uc704", "\uc81c\uc678", "out of scope", "non-goals", "exclusions"
    ),
    "subtasks": _plan_heading_pattern(
        "\uc11c\ube0c\uc791\uc5c5", "\ubd80\uc791\uc5c5", "\ud558\uc704\uc791\uc5c5", "subtasks", "breakdown"
    ),
    "acceptance_criteria": _plan_heading_pattern(
        "\uc644\ub8cc \uc870\uac74",
        "\uc778\uc218 \uc870\uac74",
        "acceptance criteria",
        "done criteria",
    ),
    "validation": _plan_heading_pattern(
        "\uac80\uc99d", "\ud14c\uc2a4\ud2b8", "validation", "test", "tests"
    ),
    "executed_validation": _plan_heading_pattern(
        "\uc2e4\ud589 \uac80\uc99d", "\uac80\uc99d \uacb0\uacfc", "\uc2e4\ud589 \uacb0\uacfc",
        "executed validation", "validation results", "test results",
    ),
    "verification_plan": _plan_heading_pattern(
        "\uac80\uc99d \uacc4\ud68d", "\ud14c\uc2a4\ud2b8 \uacc4\ud68d", "verification plan", "test plan"
    ),
    "validation_environment": _plan_heading_pattern(
        "\uac80\uc99d \ud658\uacbd", "\ud14c\uc2a4\ud2b8 \ud658\uacbd", "\uc2e4\ud589 \ud658\uacbd",
        "validation environment", "test environment", "environment",
    ),
    "schedule_rationale": _plan_heading_pattern(
        "\uc77c\uc815 \uadfc\uac70", "\uc77c\uc815 \uc0b0\uc815 \uadfc\uac70", "schedule rationale", "schedule basis"
    ),
    "epic_rationale": _plan_heading_pattern(
        "\uc5d0\ud53d \uadfc\uac70", "\uc5d0\ud53d \uc5f0\uacb0 \uadfc\uac70", "epic rationale", "epic alignment"
    ),
    "remaining_work": _plan_heading_pattern(
        "\ub0a8\uc740 \uc791\uc5c5",
        "\ud6c4\uc18d \uc791\uc5c5",
        "remaining work",
        "next step",
        "next steps",
    ),
    "risks": _plan_heading_pattern(
        "\uc704\ud5d8", "\ub9ac\uc2a4\ud06c", "risk", "risks"
    ),
}


def plan_sections_from_commit(evidence: dict[str, Any]) -> dict[str, Any]:
    """Parse optional commit-body headings into the planning module's task schema."""
    fields: dict[str, list[str]] = {name: [] for name in _PLAN_HEADINGS}
    active = ""
    for raw_line in str(evidence.get("body") or "").splitlines():
        line = raw_line.strip()
        if not line:
            active = ""
            continue
        matched = False
        for name, pattern in _PLAN_HEADINGS.items():
            match = pattern.match(line)
            if match:
                active = name
                value = re.sub(r"^[\s*+-]+", "", match.group(1) or "").strip()
                if value:
                    fields[name].append(value)
                matched = True
                break
        if matched:
            continue
        if active:
            value = re.sub(r"^[\s*+-]+", "", line).strip()
            if value:
                fields[active].append(value)

    task = {
        "summary": evidence.get("subject") or "",
        "commit_shas": [evidence.get("sha")],
        **{name: values for name, values in fields.items() if values},
    }
    return {"tasks": [task]} if any(fields.values()) else {}


def _optional_git(repo_root: Path, args: Sequence[str]) -> str:
    try:
        return _run_git(repo_root, args, check=False).strip()
    except OSError:
        return ""


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace *path* so queue readers never observe partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_project_config(repo_root: Path, config_path: Path) -> dict[str, Any]:
    """Return the enabled startup-project entry matching *repo_root*."""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read project config {config_path}: {exc}") from exc
    target = os.path.normcase(str(repo_root.resolve()))
    for project in data.get("projects") or []:
        if not isinstance(project, dict) or project.get("enabled") is False:
            continue
        raw_path = str(project.get("path") or "").strip()
        if not raw_path:
            continue
        if os.path.normcase(str(Path(raw_path).resolve())) == target:
            return dict(project)
    return {}


# Kept in sync with _SCOPE_RENDER_LIMIT / _SUBTASK_RENDER_LIMIT in
# generate_periodic_reports.py.
_SCOPE_RENDER_LIMIT = 8
_SUBTASK_RENDER_LIMIT = 5


def _jira_description(proposal: dict[str, Any]) -> str:
    def bullets(values: Any, *, limit: int = 0) -> str:
        """Bullet list, or "" so the caller can drop the section entirely.

        ``limit`` truncates for reading only — the proposal keeps every entry,
        since the quality gate anchors task-specific risks on the full scope set.
        """
        items = [str(value).strip() for value in (values or []) if str(value).strip()]
        if limit and len(items) > limit:
            rendered = items[:limit] + [f"외 {len(items) - limit}개"]
        else:
            rendered = items
        return "\n".join(f"* {item}" for item in rendered)

    commits: list[str] = []
    for commit in proposal.get("source_commits") or []:
        sha = str(commit.get("sha") or "")
        subject = str(commit.get("subject") or "")
        url = str(commit.get("url") or "")
        label = f"{sha[:12]} {subject}".strip()
        commits.append(f"[{label}|{url}]" if url else label)
    def validation_bullets(values: Any, *, planned: bool) -> str:
        rendered: list[str] = []
        for item in values or []:
            if not isinstance(item, dict):
                text = str(item or "").strip()
                if text:
                    rendered.append(text)
                continue
            command = str(item.get("command") or "").strip()
            environment = str(item.get("environment") or "").strip()
            if planned:
                expected = str(item.get("expected_result") or "").strip()
                criteria = str(item.get("pass_criteria") or "").strip()
                status = str(item.get("status") or "not_run").strip()
                parts = [command, f"환경={environment}", f"기대={expected}",
                         f"판정={criteria}", f"상태={status}"]
            else:
                actual = str(item.get("actual_result") or item.get("result") or "").strip()
                source = str(item.get("source") or "").strip()
                parts = [command, f"환경={environment}", f"실제={actual}", f"출처={source}"]
            rendered.append(" | ".join(part for part in parts if part and not part.endswith("=")))
        return bullets(rendered)

    def subtask_bullets(values: Any, *, limit: int = _SUBTASK_RENDER_LIMIT) -> str:
        """Numbered breakdown: title — 설명 (완료: 조건). Mirrors _jira_subtask_lines()."""
        rendered: list[str] = []
        for item in values or []:
            if isinstance(item, dict):
                summary = str(item.get("summary") or "").strip()
                description = str(item.get("description") or "").strip()
                criteria = [
                    str(entry).strip()
                    for entry in (item.get("acceptance_criteria") or [])
                    if str(entry).strip()
                ]
            else:
                summary, description, criteria = str(item or "").strip(), "", []
            if not summary:
                continue
            line = f"*{len(rendered) + 1}. {summary}*"
            if description:
                line += f" — {description}"
            if criteria:
                line += f" (완료: {'; '.join(criteria)})"
            rendered.append(line)
        return bullets(rendered, limit=limit)

    sections = (
        ("목적", str(proposal.get("purpose") or "").strip()),
        ("문제 / 배경", str(proposal.get("problem") or "").strip()),
        ("목표 결과", str(proposal.get("outcome") or "").strip()),
        ("작업 범위", bullets(proposal.get("scope"), limit=_SCOPE_RENDER_LIMIT)),
        ("제외 범위", bullets(proposal.get("out_of_scope"))),
        ("서브작업", subtask_bullets(proposal.get("subtasks"))),
        ("완료 조건", bullets(proposal.get("acceptance_criteria"))),
        # Kept even when empty — "없음" is the audit signal that nothing ran, so a
        # reviewer cannot mistake 향후 검증 계획 for a result. See
        # _jira_plan_description() in generate_periodic_reports.py.
        ("실행된 검증", validation_bullets(
            proposal.get("executed_validation"), planned=False
        ) or "* 없음"),
        ("향후 검증 계획", validation_bullets(
            proposal.get("verification_plan"), planned=True
        )),
        ("남은 작업", bullets(proposal.get("remaining_work"))),
        ("리스크", bullets(proposal.get("risks"))),
        # 일정 / Epic 근거 is intentionally not rendered — it explains the
        # generator's own scheduling/Epic choice, not the work. The fields stay on
        # the proposal for the quality gate. Mirrors _jira_plan_description().
        ("근거 커밋", bullets(commits)),
    )
    # Empty sections are dropped, and the dedupe_marker stays out of the body —
    # Jira-side idempotency is the ARID… marker in the summary, and nothing ever
    # read a marker back out of a description. Mirrors _jira_plan_description().
    return "\n\n".join(f"h2. {title}\n{body}" for title, body in sections if body)


def _next_business_day(value: date) -> date:
    current = value + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def _add_business_days(value: date, working_days: int) -> date:
    current = value
    remaining = max(0, int(working_days))
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _stored_create_proposal(
    proposal: dict[str, Any], jira_config: dict[str, Any]
) -> dict[str, Any]:
    horizon = int(jira_config.get("plan_horizon_days") or 7)
    horizon = min(max(horizon, 1), 90)
    start = _next_business_day(date.today())
    raw_estimate = (proposal.get("schedule") or {}).get("estimated_working_days")
    try:
        estimated_working_days = int(raw_estimate or horizon)
    except (TypeError, ValueError):
        estimated_working_days = horizon
    estimated_working_days = min(max(estimated_working_days, 1), horizon)
    stored = {
        "id": proposal.get("id"),
        "type": "create_task",
        "task_key": "",
        "project_key": proposal.get("project_key") or jira_config.get("project_key") or "",
        "epic_key": proposal.get("epic_key") or jira_config.get("epic_key") or "",
        "suggested_text": proposal.get("summary") or "",
        "suggested_description": _jira_description(proposal),
        "start": start.isoformat(),
        "end": _add_business_days(start, estimated_working_days).isoformat(),
        "report_required": str(jira_config.get("report_required") or "yes"),
        "labels": ["autoreport", f"autoreport-{str(proposal.get('id') or '').lower()}"],
        "approval_channel": "hook_auto",
    }
    # JiraApplyService recomputes the quality assessment from this authoritative
    # server-side evidence.  Do not collapse it to presentation-only fields.
    for field in (
        "action",
        "issue_type",
        "related_jira_key",
        "binding_source",
        "dedupe_marker",
        "problem",
        "outcome",
        "scope",
        "acceptance_criteria",
        "validation",
        "executed_validation",
        "verification_plan",
        "validation_environment",
        "remaining_work",
        "risks",
        "task_specific_risks",
        "schedule",
        "schedule_rationale",
        "epic_rationale",
        "source_commits",
        "source_files",
        "proposal_type",
        "evidence_type",
        "grouping_rationale",
        "create_suppressed",
        "plan_source",
        "quality_score",
        "quality_grade",
        "quality_dimensions",
        "blocking_reasons",
        "auto_apply_eligible",
    ):
        if field in proposal:
            stored[field] = proposal[field]
    return stored


_CONVENTIONAL_SUBJECT_PREFIX = re.compile(
    r"^(?:build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(?:\([^\r\n)]*\))?!?:\s*",
    re.IGNORECASE,
)


def commit_quality_reasons(evidence: dict[str, Any]) -> list[str]:
    """Return deterministic reasons why a commit must not become a Jira plan."""
    subject = str(evidence.get("subject") or "").strip()
    reasons: list[str] = []
    if not evidence.get("files"):
        reasons.append("commit has no changed files")
    if len(subject) < 8:
        reasons.append("commit subject is too short")

    normalized = _CONVENTIONAL_SUBJECT_PREFIX.sub("", subject, count=1).strip()
    bracket_marker = re.match(
        r"^\s*\[(?:wip|tmp|temp|temporary|snapshot)\]",
        subject,
        re.IGNORECASE,
    )
    low_signal_subject = bool(
        bracket_marker
        or re.match(r"^auto[\s-]?commit\b", normalized, re.IGNORECASE)
        or re.match(
            r"^(?:(?:end[\s-]*of[\s-]*day|eod|daily|nightly)\s+)?snapshot\b",
            normalized,
            re.IGNORECASE,
        )
        or re.match(r"^(?:wip\b|work\s+in\s+progress\b)", normalized, re.IGNORECASE)
        or re.match(
            r"^(?:tmp|temp|temporary)(?:\s+(?:commit|changes?|save|checkpoint|work|"
            r"\d{4}[-/]\d{2}[-/]\d{2}))?\s*$",
            normalized,
            re.IGNORECASE,
        )
        or re.match(
            r"^merge(?:\s*$|\s+(?:branch|pull\s+request|remote|from|into|main|master|"
            r"develop|release|feature|hotfix)\b)",
            normalized,
            re.IGNORECASE,
        )
        or int(evidence.get("parent_count") or 0) > 1
    )
    if low_signal_subject:
        reasons.append("generic or non-actionable commit subject")
    return reasons


def build_queue_payload(
    repo_root: Path,
    evidence: dict[str, Any],
    project_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    remote_url = _optional_git(repo_root, ["remote", "get-url", "origin"])
    branch = _optional_git(repo_root, ["branch", "--show-current"])
    project_config = dict(project_config or {})
    jira_config = dict(project_config.get("jira") or {})
    parsed_plan = plan_sections_from_commit(evidence)
    auto_apply_requested = os.environ.get("JIRA_AUTO_APPLY", "").strip() == "1"
    rollout_stage = os.environ.get("JIRA_QUALITY_ROLLOUT_STAGE", "B").strip().upper() or "B"
    if rollout_stage not in {"A", "B", "C", "D"}:
        rollout_stage = "B"
    quality_reasons = commit_quality_reasons(evidence)
    # A rejected commit is retained as evidence, but it must not leak a Task
    # proposal into either the review queue or the optional apply path.
    proposals = []
    if not quality_reasons:
        proposals = [enrich_proposal_quality(item) for item in build_create_task_proposals(
            [evidence],
            parsed_plan,
            repository_url=remote_url,
            default_project_key=str(
                jira_config.get("project_key") or os.environ.get("JIRA_PROJECT_KEY") or ""
            ),
            default_epic_key=str(jira_config.get("epic_key") or ""),
        )]
    proposal_blockers = sorted({
        str(reason)
        for proposal in proposals
        for reason in (proposal.get("blocking_reasons") or [])
        if str(reason).strip()
    })
    proposal_gate_passed = bool(proposals) and all(
        bool(proposal.get("auto_apply_eligible")) for proposal in proposals
    )
    combined_quality_reasons = [*quality_reasons, *proposal_blockers]
    return {
        "schema_version": 1,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "mode": "review_only",
        "external_write_performed": False,
        "auto_apply_requested": auto_apply_requested,
        "auto_apply_supported": True,
        "quality_rollout_stage": rollout_stage,
        "project_configured": bool(project_config),
        "jira_auto_plan_enabled": bool(jira_config.get("auto_plan", False)),
        "quality_gate": {
            "passed": not quality_reasons and proposal_gate_passed,
            "commit_gate_passed": not quality_reasons,
            "proposal_gate_passed": proposal_gate_passed,
            "reasons": combined_quality_reasons,
            "structured_plan_from_commit": bool(parsed_plan),
            "validation_evidence_count": len(evidence.get("validation") or []),
            "executed_validation_count": len(evidence.get("executed_validation") or []),
            "verification_plan_count": len(evidence.get("verification_plan") or []),
            "proposal_scores": [
                {
                    "id": proposal.get("id"),
                    "score": proposal.get("quality_score"),
                    "grade": proposal.get("quality_grade"),
                    "auto_apply_eligible": proposal.get("auto_apply_eligible"),
                }
                for proposal in proposals
            ],
        },
        "repository": {
            "name": repo_root.name,
            "root": str(repo_root),
            "branch": branch,
            "remote_url": remote_url,
        },
        "commit": evidence,
        "proposals": proposals,
        "apply_results": [],
        "review": {
            "required": True,
            "reason": (
                f"Automatic apply was requested at rollout stage {rollout_stage} and is awaiting safety checks."
                if auto_apply_requested
                else "Proposal requires explicit review before any Jira write."
            ),
        },
    }


def apply_queued_proposals(
    payload: dict[str, Any], project_config: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Apply eligible create proposals and return ``(payload, all_satisfied)``."""
    jira_config = dict(project_config.get("jira") or {})
    if not project_config or not jira_config.get("auto_plan"):
        payload["review"]["reason"] = "Project is not enabled for Jira auto planning."
        return payload, False
    if not (payload.get("quality_gate") or {}).get("passed"):
        reasons = ", ".join((payload.get("quality_gate") or {}).get("reasons") or [])
        payload["review"]["reason"] = f"Commit quality gate failed: {reasons}"
        return payload, False
    project_key = str(jira_config.get("project_key") or os.environ.get("JIRA_PROJECT_KEY") or "")
    if not project_key:
        payload["review"]["reason"] = "Jira project key is not configured."
        return payload, False

    from workflow.jira_apply import JiraApplyError, JiraApplyService
    from workflow.jira_outbox import JiraOutbox
    from workflow.task_provider import JiraApiTaskProvider, get_task_provider

    provider = get_task_provider(project_config)
    if not isinstance(provider, JiraApiTaskProvider):
        payload["review"]["reason"] = "Live Jira URL/token are unavailable; no write attempted."
        return payload, False
    service = JiraApplyService(
        provider,
        JiraOutbox(AUTOREPORT_ROOT / "reports" / "jira_outbox.json"),
    )

    results: list[dict[str, Any]] = []
    all_satisfied = True
    for proposal in payload.get("proposals") or []:
        if proposal.get("related_jira_key"):
            results.append(
                {
                    "proposal_id": proposal.get("id"),
                    "status": "review_required",
                    "reason": "Explicit Jira key found; automatic create is suppressed.",
                }
            )
            all_satisfied = False
            continue
        stored = _stored_create_proposal(dict(proposal), jira_config)
        try:
            result = service.apply_create_task(stored)
            results.append({"proposal_id": proposal.get("id"), "status": "applied", **result.to_dict()})
        except JiraApplyError as exc:
            results.append(
                {
                    "proposal_id": proposal.get("id"),
                    "status": "uncertain" if exc.uncertain else "blocked",
                    "operation_id": exc.operation_id,
                    "reason": str(exc),
                }
            )
            all_satisfied = False

    payload["apply_results"] = results
    payload["external_write_performed"] = any(item.get("created") for item in results)
    payload["mode"] = "applied" if all_satisfied and results else "review_required"
    payload["review"] = {
        "required": not (all_satisfied and bool(results)),
        "reason": (
            "Every eligible proposal is reconciled with Jira."
            if all_satisfied and results
            else "At least one proposal requires review or reconciliation."
        ),
    }
    return payload, all_satisfied


def queue_commit(
    repo: Path,
    commitish: str = "HEAD",
    output_dir: Path | None = None,
    config_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    repo_root = resolve_repo_root(repo)
    evidence = collect_commit_evidence(repo_root, commitish)
    safe_repo = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_root.name).strip("-._") or "repository"
    project_config = load_project_config(
        repo_root,
        (config_path or (SCRIPT_DIR / "startup_projects.json")).resolve(),
    )
    payload = build_queue_payload(repo_root, evidence, project_config)
    queue_dir = (
        output_dir.resolve()
        if output_dir
        else AUTOREPORT_ROOT / "reports" / "jira_queue" / safe_repo
    )
    output_path = queue_dir / f"{safe_repo}-{evidence['sha']}.proposal.json"
    # Persist the exact proposal before an optional network mutation. If the
    # process dies later, the outbox and Jira label can reconcile the result.
    write_json_atomic(output_path, payload)
    if payload["auto_apply_requested"]:
        payload, _ = apply_queued_proposals(payload, project_config)
        write_json_atomic(output_path, payload)
    return output_path, payload


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _load_environment()
    try:
        output_path, payload = queue_commit(
            Path(args.repo),
            args.commit,
            Path(args.output_dir) if args.output_dir else None,
            Path(args.config),
        )
    except Exception as exc:
        print(f"[jira-queue] failed: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"[jira-queue] commit proposal saved: {output_path}")
    if payload["auto_apply_requested"]:
        if payload.get("mode") == "applied":
            print("[jira-queue] Jira proposal applied or reconciled successfully")
            return EXIT_OK
        print(f"[jira-queue] auto-apply blocked: {payload['review']['reason']}", file=sys.stderr)
        return EXIT_AUTO_APPLY_BLOCKED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
