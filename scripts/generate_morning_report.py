from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path


FIELD_SEP = "\x1f"
DEFAULT_EXCLUDED_TOP_LEVEL_DIRS = {
    "TResultParser",
    "backup_before_split",
    "backup_phase_a",
    "my_lin_gateway_251118_bakup",
    "report",
    "reports",
    "output",
    "jenkins_reports_http_192.168.110.40_7000_job_HDPDM01_PDS64_RD_lastSuccessfulBuild_20260119_115031",
    "jenkins_reports_http_192.168.110.40_7000_job_KJPDS02_DV_lastSuccessfulBuild_20260119_115122",
}
IGNORED_PATH_SEGMENTS = {
    ".svn",
    ".vs",
    "__pycache__",
    ".pytest_cache",
    ".codex_tmp",
}


@dataclass
class Commit:
    full_hash: str
    short_hash: str
    authored_at: str
    author: str
    subject: str


@dataclass
class FilterResult:
    kept: list[str]
    excluded_count: int
    excluded_roots: list[str]


def run_git(repo_root: Path, args: list[str], check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise RuntimeError(f"{' '.join(args)}: {message}")
    return proc.stdout.strip()


def detect_repo_root(start: Path) -> Path:
    root = run_git(start, ["rev-parse", "--show-toplevel"])
    return Path(root)


def detect_branch(repo_root: Path) -> str:
    return run_git(repo_root, ["branch", "--show-current"])


def detect_remote_url(repo_root: Path) -> str:
    url = run_git(repo_root, ["remote", "get-url", "origin"], check=False)
    return url or "-"


def detect_upstream(repo_root: Path) -> str | None:
    upstream = run_git(
        repo_root,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        check=False,
    )
    return upstream or None


def ahead_behind(repo_root: Path, upstream: str | None) -> tuple[int, int] | None:
    if not upstream:
        return None
    counts = run_git(repo_root, ["rev-list", "--left-right", "--count", f"{upstream}...HEAD"], check=False)
    if not counts:
        return None
    left, right = counts.split()
    behind = int(left)
    ahead = int(right)
    return ahead, behind


def parse_commits(raw: str) -> list[Commit]:
    commits: list[Commit] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split(FIELD_SEP)
        if len(parts) != 5:
            continue
        commits.append(
            Commit(
                full_hash=parts[0],
                short_hash=parts[1],
                authored_at=parts[2],
                author=parts[3],
                subject=parts[4],
            )
        )
    return commits


def get_commits(repo_root: Path, branch: str, since_iso: str) -> list[Commit]:
    raw = run_git(
        repo_root,
        [
            "log",
            branch,
            f"--since={since_iso}",
            f"--pretty=format:%H{FIELD_SEP}%h{FIELD_SEP}%ad{FIELD_SEP}%an{FIELD_SEP}%s",
            "--date=iso",
        ],
        check=False,
    )
    return parse_commits(raw)


def get_changed_files(repo_root: Path, branch: str, since_iso: str) -> list[str]:
    raw = run_git(
        repo_root,
        ["log", branch, f"--since={since_iso}", "--name-only", "--pretty=format:"],
        check=False,
    )
    files: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        path = line.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def is_relevant_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    return not any(part in IGNORED_PATH_SEGMENTS for part in parts)


def top_level_dir(path: str) -> str:
    normalized = path.replace("\\", "/")
    return normalized.split("/", 1)[0]


def filter_changed_files(paths: list[str]) -> FilterResult:
    kept: list[str] = []
    excluded_count = 0
    excluded_roots: set[str] = set()

    for path in paths:
        if not is_relevant_path(path):
            excluded_count += 1
            excluded_roots.add(top_level_dir(path))
            continue
        root = top_level_dir(path)
        if root in DEFAULT_EXCLUDED_TOP_LEVEL_DIRS:
            excluded_count += 1
            excluded_roots.add(root)
            continue
        kept.append(path)

    return FilterResult(
        kept=kept,
        excluded_count=excluded_count,
        excluded_roots=sorted(excluded_roots),
    )


def get_uncommitted(repo_root: Path) -> list[str]:
    raw = run_git(repo_root, ["status", "--short"], check=False)
    return [line for line in raw.splitlines() if line.strip()]


def top_directories(paths: list[str], limit: int = 5) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for path in paths:
        normalized = path.replace("\\", "/")
        head = normalized.split("/", 1)[0]
        counts[head] += 1
    return counts.most_common(limit)


def summarize_subjects(commits: list[Commit], limit: int = 5) -> list[str]:
    subjects = [commit.subject.strip() for commit in commits if commit.subject.strip()]
    return subjects[:limit]


def build_markdown(
    repo_root: Path,
    branch: str,
    remote_url: str,
    upstream: str | None,
    sync_state: tuple[int, int] | None,
    report_date: date,
    since_date: date,
    commits: list[Commit],
    changed_files: list[str],
    excluded_count: int,
    excluded_roots: list[str],
    uncommitted: list[str],
) -> str:
    lines: list[str] = []
    lines.append(f"# Morning Report - {report_date.isoformat()}")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- Repository: `{repo_root.name}`")
    lines.append(f"- Branch: `{branch}`")
    lines.append(f"- Remote: {remote_url}")
    lines.append(f"- Work window: `{since_date.isoformat()} 00:00` to `{report_date.isoformat()} report time`")
    lines.append(f"- Commit count: `{len(commits)}`")
    lines.append(f"- Changed files: `{len(changed_files)}`")
    lines.append(f"- Excluded noisy paths: `{excluded_count}`")
    lines.append(f"- Uncommitted changes: `{len(uncommitted)}`")
    if upstream:
        lines.append(f"- Upstream: `{upstream}`")
    if sync_state:
        ahead, behind = sync_state
        lines.append(f"- Sync status: `ahead {ahead}, behind {behind}`")
    if excluded_roots:
        lines.append(f"- Hidden roots: `{', '.join(excluded_roots[:8])}`")
    lines.append("")

    lines.append("## Key Points")
    lines.append("")
    if commits:
        for subject in summarize_subjects(commits):
            lines.append(f"- {subject}")
    else:
        lines.append("- No commits found in the selected time window")
    lines.append("")

    lines.append("## Activity By Area")
    lines.append("")
    if changed_files:
        for area, count in top_directories(changed_files):
            lines.append(f"- `{area}`: {count} file(s)")
    else:
        lines.append("- No changed files found")
    lines.append("")

    lines.append("## Recent Commits")
    lines.append("")
    if commits:
        for commit in commits:
            lines.append(
                f"- `{commit.short_hash}` {commit.subject} ({commit.author}, {commit.authored_at})"
            )
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Changed Files")
    lines.append("")
    if changed_files:
        for path in changed_files[:50]:
            lines.append(f"- `{path}`")
        if len(changed_files) > 50:
            lines.append(f"- ... and {len(changed_files) - 50} more")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Working Tree")
    lines.append("")
    if uncommitted:
        for line in uncommitted:
            lines.append(f"- `{line}`")
    else:
        lines.append("- Clean")
    lines.append("")
    return "\n".join(lines)


def pick_python_output_path(repo_root: Path, report_date: date) -> Path:
    return repo_root / "reports" / "morning_brief" / f"{report_date.isoformat()}-morning-report.md"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a morning git activity report.")
    parser.add_argument("--repo", default=".", help="Repository path")
    parser.add_argument("--branch", default=None, help="Branch to inspect")
    parser.add_argument("--since-date", default=None, help="Start date in YYYY-MM-DD")
    parser.add_argument("--report-date", default=None, help="Report date in YYYY-MM-DD")
    parser.add_argument("--output", default=None, help="Markdown output path")
    parser.add_argument("--stdout", action="store_true", help="Also print report to stdout")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = detect_repo_root(Path(args.repo).resolve())
    report_date = date.fromisoformat(args.report_date) if args.report_date else date.today()
    since_date = date.fromisoformat(args.since_date) if args.since_date else report_date - timedelta(days=1)
    since_iso = datetime.combine(since_date, time.min).isoformat()

    branch = args.branch or detect_branch(repo_root)
    remote_url = detect_remote_url(repo_root)
    upstream = detect_upstream(repo_root)
    sync_state = ahead_behind(repo_root, upstream)
    commits = get_commits(repo_root, branch, since_iso)
    changed_files = get_changed_files(repo_root, branch, since_iso)
    filter_result = filter_changed_files(changed_files)
    uncommitted = get_uncommitted(repo_root)

    output_path = Path(args.output) if args.output else pick_python_output_path(repo_root, report_date)
    if not output_path.is_absolute():
        output_path = repo_root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    report_text = build_markdown(
        repo_root=repo_root,
        branch=branch,
        remote_url=remote_url,
        upstream=upstream,
        sync_state=sync_state,
        report_date=report_date,
        since_date=since_date,
        commits=commits,
        changed_files=filter_result.kept,
        excluded_count=filter_result.excluded_count,
        excluded_roots=filter_result.excluded_roots,
        uncommitted=uncommitted,
    )
    output_path.write_text(report_text, encoding="utf-8")

    print(f"Report written to: {output_path}")
    if args.stdout:
        print()
        print(report_text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"Failed to generate morning report: {exc}", file=sys.stderr)
        raise SystemExit(1)
