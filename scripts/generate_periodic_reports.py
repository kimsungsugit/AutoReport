from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from urllib.parse import urlparse
from html import escape

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from scripts.design_system import (
    DESIGN_CSS, CHECKLIST_JS,
    SVG_PALETTE, SVG_BG_DARK, SVG_STROKE, SVG_TEXT_DARK, SVG_TEXT_DARKER,
    SVG_TEXT_LIGHT, SVG_TEXT_MUTED, SVG_TEXT_ACCENT, SVG_IMPACT_STROKE,
    SVG_SUBTITLE_TINTS, svg_text_color_for,
    SVG_SUB_ON_DARK, SVG_SUB_ON_WARM, SVG_META_LIGHT,
)


def load_get_adapter():
    module_path = REPO_ROOT / "workflow" / "llm_adapters.py"
    spec = importlib.util.spec_from_file_location("workflow_llm_adapters", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load adapter module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.get_adapter


get_adapter = load_get_adapter()


def load_sprint_tasks() -> dict[str, Any]:
    """Load sprint task definitions via TaskProvider.

    Uses JiraApiTaskProvider if JIRA_URL/JIRA_TOKEN are set,
    otherwise falls back to sprint_tasks.json.
    """
    try:
        task_provider_path = REPO_ROOT / "workflow" / "task_provider.py"
        spec = importlib.util.spec_from_file_location("task_provider", task_provider_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.get_task_provider().get_tasks()
    except Exception:
        pass
    # Direct fallback
    path = Path(__file__).resolve().parent / "sprint_tasks.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Build a word-boundary-aware regex for a keyword.

    Hyphens and underscores in keywords are treated as flexible
    separators so that e.g. ``github-actions`` matches ``github actions``,
    ``github-actions``, and ``github_actions``.  Short keywords (<=2 chars)
    always require word boundaries to avoid false positives
    (e.g. ``ci`` matching ``spe*ci*fication``).
    """
    # Split on hyphens/underscores, escape each part, rejoin with flexible separator
    parts = re.split(r"[-_]", keyword)
    normalized = r"[-_ ]?".join(re.escape(p) for p in parts)
    return re.compile(rf"(?<![a-z0-9]){normalized}(?![a-z0-9])", re.IGNORECASE)


def _parse_keywords(raw_keywords: list) -> list[dict[str, Any]]:
    """Normalize keywords to [{word, weight, pattern}] format.

    Supports both old format (list of strings) and new format
    (list of {word, weight} dicts).
    """
    result = []
    for kw in raw_keywords:
        if isinstance(kw, str):
            result.append({"word": kw.lower(), "weight": 1, "pattern": _keyword_pattern(kw)})
        elif isinstance(kw, dict):
            word = str(kw.get("word", "")).lower()
            weight = int(kw.get("weight", 1))
            if word:
                result.append({"word": word, "weight": weight, "pattern": _keyword_pattern(word)})
    return result


def match_commits_to_tasks(
    commits: list[dict[str, str]],
    changed_files: list[str],
    sprint_data: dict[str, Any],
    report_date: date,
) -> list[dict[str, Any]]:
    """Match commits and changed files to sprint tasks by weighted keywords."""
    tasks = sprint_data.get("tasks") or []
    if not tasks:
        return []
    search_text = " ".join(
        [c.get("subject", "") for c in commits]
        + changed_files
    ).lower()
    matched = []
    for task in tasks:
        try:
            task_start = date.fromisoformat(task["start"])
            task_end = date.fromisoformat(task["end"])
        except (ValueError, KeyError) as exc:
            print(f"[WARN] task {task.get('key', '?')} 날짜 파싱 실패: {exc}")
            continue
        # subtask status 집계
        subtasks = task.get("subtasks", [])
        subtask_done = sum(1 for s in subtasks if s.get("status") == "done")
        subtask_total = len(subtasks)
        # 날짜 + subtask 기반 상태 결정
        if report_date < task_start:
            status = "예정"
        elif report_date > task_end:
            status = "완료"
        elif subtask_total > 0 and subtask_done == subtask_total:
            status = "완료"
        else:
            status = "진행 중"
        kw_entries = _parse_keywords(task.get("keywords", []))
        weighted_score = sum(
            entry["weight"] for entry in kw_entries
            if entry["pattern"].search(search_text)
        )
        related_commits = [
            c["subject"] for c in commits
            if any(entry["pattern"].search(c.get("subject", "")) for entry in kw_entries)
        ]
        matched.append({
            "key": task["key"],
            "title": task["title"],
            "start": task["start"],
            "end": task["end"],
            "status": status,
            "subtasks": subtasks,
            "hit_count": weighted_score,
            "related_commits": related_commits[:5],
            "subtask_progress": f"{subtask_done}/{subtask_total}",
        })
    matched.sort(key=lambda x: x["hit_count"], reverse=True)
    return matched


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

REPORT_DEPRIORITIZED_TOP_LEVEL_DIRS = {
    "stremlit_",
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
    short_hash: str
    authored_at: str
    author: str
    subject: str


@dataclass
class ReportWindow:
    start: date
    end: date
    label: str


def run_git(repo_root: Path, args: list[str], check: bool = True) -> str:
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
        raise RuntimeError(f"{' '.join(args)}: {message}")
    return proc.stdout.strip()


def detect_repo_root(start: Path) -> Path:
    return Path(run_git(start, ["rev-parse", "--show-toplevel"]))


def detect_branch(repo_root: Path) -> str:
    return run_git(repo_root, ["branch", "--show-current"])


def detect_remote_url(repo_root: Path) -> str:
    url = run_git(repo_root, ["remote", "get-url", "origin"], check=False)
    return url or "-"


def detect_upstream(repo_root: Path) -> str | None:
    upstream = run_git(repo_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], check=False)
    return upstream or None


def ahead_behind(repo_root: Path, upstream: str | None) -> tuple[int, int] | None:
    if not upstream:
        return None
    counts = run_git(repo_root, ["rev-list", "--left-right", "--count", f"{upstream}...HEAD"], check=False)
    if not counts:
        return None
    left, right = counts.split()
    return int(right), int(left)


def parse_commits(raw: str) -> list[Commit]:
    commits: list[Commit] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split(FIELD_SEP)
        if len(parts) != 4:
            continue
        commits.append(Commit(parts[0], parts[1], parts[2], parts[3]))
    return commits


def get_commits(repo_root: Path, branch: str, start_day: date, end_day: date) -> list[Commit]:
    start_iso = datetime.combine(start_day, time.min).isoformat()
    end_iso = datetime.combine(end_day + timedelta(days=1), time.min).isoformat()
    raw = run_git(
        repo_root,
        ["log", branch, f"--since={start_iso}", f"--until={end_iso}", f"--pretty=format:%h{FIELD_SEP}%ad{FIELD_SEP}%an{FIELD_SEP}%s", "--date=iso"],
        check=False,
    )
    return parse_commits(raw)


def get_changed_files(repo_root: Path, branch: str, start_day: date, end_day: date) -> list[str]:
    start_iso = datetime.combine(start_day, time.min).isoformat()
    end_iso = datetime.combine(end_day + timedelta(days=1), time.min).isoformat()
    raw = run_git(repo_root, ["log", branch, f"--since={start_iso}", f"--until={end_iso}", "--name-only", "--pretty=format:"], check=False)
    files: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        path = line.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def get_diff_numstat(repo_root: Path, branch: str, start_day: date, end_day: date) -> list[dict[str, Any]]:
    start_iso = datetime.combine(start_day, time.min).isoformat()
    end_iso = datetime.combine(end_day + timedelta(days=1), time.min).isoformat()
    raw = run_git(
        repo_root,
        ["log", branch, f"--since={start_iso}", f"--until={end_iso}", "--numstat", "--pretty=format:"],
        check=False,
    )
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        try:
            added_i = int(added) if added.isdigit() else 0
            deleted_i = int(deleted) if deleted.isdigit() else 0
        except ValueError:
            added_i = 0
            deleted_i = 0
        rows.append({"path": path, "added": added_i, "deleted": deleted_i, "total": added_i + deleted_i})
    return rows


def summarize_diff_stats(numstats: list[dict[str, Any]]) -> dict[str, Any]:
    filtered = [item for item in numstats if is_relevant_path(str(item.get("path", "")))]
    total_added = sum(int(item["added"]) for item in filtered)
    total_deleted = sum(int(item["deleted"]) for item in filtered)
    top_files = sorted(filtered, key=lambda item: int(item["total"]), reverse=True)[:10]
    return {
        "total_added": total_added,
        "total_deleted": total_deleted,
        "top_files": top_files,
    }


def changed_markdown_docs(paths: list[str]) -> list[str]:
    return [path for path in paths if path.lower().endswith(".md")]


def is_relevant_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if any(part in IGNORED_PATH_SEGMENTS for part in parts):
        return False
    return parts[0] not in DEFAULT_EXCLUDED_TOP_LEVEL_DIRS


def get_uncommitted(repo_root: Path) -> list[str]:
    raw = run_git(repo_root, ["status", "--short"], check=False)
    return [line for line in raw.splitlines() if line.strip()]


def top_directories(paths: list[str], limit: int = 5) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for path in paths:
        normalized = path.replace("\\", "/")
        root = normalized.split("/", 1)[0]
        if root in REPORT_DEPRIORITIZED_TOP_LEVEL_DIRS:
            continue
        counts[root] += 1
    return counts.most_common(limit)


def month_bounds(target: date) -> tuple[date, date]:
    start = date(target.year, target.month, 1)
    if target.month == 12:
        next_month = date(target.year + 1, 1, 1)
    else:
        next_month = date(target.year, target.month + 1, 1)
    return start, next_month - timedelta(days=1)


def previous_month(today: date) -> tuple[date, date]:
    first_day_this_month = date(today.year, today.month, 1)
    return month_bounds(first_day_this_month - timedelta(days=1))


def previous_business_day(target: date) -> date:
    current = target - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def should_generate_weekly(today: date) -> bool:
    # Thursday(3) first, then Friday(4), then Monday(0)
    return today.weekday() in (3, 4, 0)


def should_generate_monthly(today: date) -> bool:
    # Generate on first 5 business days of new month (Mon-Fri)
    if today.weekday() >= 5:
        return False
    _, prev_month_end = previous_month(today)
    return today > prev_month_end and today.day <= 7


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    path.write_text(text, encoding="utf-8")


def cleanup_legacy_jira_outputs(output_root: Path, today: date) -> None:
    jira_dir = output_root / "reports" / "jira"
    legacy_names = [
        f"{today.isoformat()}-jira-plan.md",
        f"{today.isoformat()}-jira-plan.html",
        f"{today.isoformat()}-jira-result.md",
        f"{today.isoformat()}-jira-result.html",
    ]
    for name in legacy_names:
        path = jira_dir / name
        if path.exists():
            path.unlink()


def load_auto_commit_status(repo_name: str, target_day: date) -> dict[str, Any] | None:
    status_path = REPO_ROOT / "reports" / "automation_status" / f"{target_day.isoformat()}-auto-commit-push.json"
    if not status_path.exists():
        return None
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    for item in payload.get("projects") or []:
        if str(item.get("name") or "") == repo_name:
            return dict(item)
    return None


def choose_gemini_config() -> dict[str, Any] | None:
    configs = config.load_oai_config_list()
    gemini_items = []
    for item in configs:
        model = str(item.get("model") or "").lower()
        api_type = str(item.get("api_type") or "").lower()
        if "gemini" in model or api_type == "google":
            gemini_items.append(dict(item))
    if not gemini_items:
        return None

    def rank(item: dict[str, Any]) -> tuple[int, int]:
        model = str(item.get("model") or "").lower()
        return (1 if ("gemini-3" in model or "pro" in model) else 0, 1 if "flash" not in model else 0)

    gemini_items.sort(key=rank, reverse=True)
    return gemini_items[0]


def clean_json_block(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def parse_github_repo(remote_url: str) -> tuple[str, str] | None:
    if not remote_url or remote_url == "-":
        return None
    cleaned = remote_url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if cleaned.startswith("git@github.com:"):
        path = cleaned.split("git@github.com:", 1)[1]
    else:
        parsed = urlparse(cleaned)
        if parsed.netloc.lower() != "github.com":
            return None
        path = parsed.path.lstrip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def github_request(url: str, token: str | None = None, params: dict[str, Any] | None = None) -> Any:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    full_url = url
    if params:
        full_url = f"{url}?{urllib_parse.urlencode(params)}"
    req = urllib_request.Request(full_url, headers=headers)
    with urllib_request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def iso_window(start_day: date, end_day: date) -> tuple[str, str]:
    start_iso = datetime.combine(start_day, time.min, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    end_iso = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return start_iso, end_iso


def fetch_github_metadata(remote_url: str, branch: str, window: ReportWindow, local_commits: list[Commit]) -> dict[str, Any]:
    repo = parse_github_repo(remote_url)
    if not repo:
        return {"enabled": False, "reason": "remote_not_github"}
    owner, name = repo
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip() or None
    start_iso, end_iso = iso_window(window.start, window.end)
    base = f"https://api.github.com/repos/{owner}/{name}"
    try:
        commit_items = github_request(f"{base}/commits", token=token, params={"sha": branch, "since": start_iso, "until": end_iso, "per_page": 30})
        pulls = github_request(f"{base}/pulls", token=token, params={"state": "all", "sort": "updated", "direction": "desc", "per_page": 20})
    except Exception as exc:
        return {"enabled": False, "reason": "api_failed", "error": str(exc)}

    local_map = {commit.short_hash: commit for commit in local_commits}
    github_commits = []
    for item in commit_items:
        sha = str(item.get("sha") or "")
        short = sha[:7]
        if local_map and short not in local_map:
            continue
        commit_info = item.get("commit") or {}
        author_info = commit_info.get("author") or {}
        github_commits.append(
            {
                "sha": short,
                "message": str(commit_info.get("message") or "").splitlines()[0],
                "html_url": item.get("html_url") or "",
                "author_login": (item.get("author") or {}).get("login") or "",
                "authored_at": author_info.get("date") or "",
            }
        )

    prs = []
    for item in pulls:
        prs.append(
            {
                "number": item.get("number"),
                "title": item.get("title") or "",
                "state": item.get("state") or "",
                "html_url": item.get("html_url") or "",
                "updated_at": item.get("updated_at") or "",
                "merged_at": item.get("merged_at") or "",
            }
        )

    return {
        "enabled": True,
        "repo": f"{owner}/{name}",
        "commit_count": len(github_commits),
        "commits": github_commits[:20],
        "pull_requests": prs[:10],
        "token_used": bool(token),
    }


def infer_work_type(changed_files: list[str], commits: list[Commit], profile_name: str = "general_software") -> str:
    text = " ".join(commit.subject.lower() for commit in commits)
    normalized_paths = [path.replace("\\", "/").lower() for path in changed_files]
    uds_hits = sum(1 for path in normalized_paths if "uds" in path)
    quality_hits = sum(1 for path in normalized_paths if any(token in path for token in ("quality", "validation", "coverage", "baseline", "compare")))
    test_hits = sum(1 for path in normalized_paths if path.startswith("tests/") or "/test_" in path or path.endswith("_test.py"))
    docs_hits = sum(1 for path in normalized_paths if path.endswith(".md") or path.startswith("docs/") or path.startswith("project_docs/"))
    backend_hits = sum(1 for path in normalized_paths if path.startswith("backend/"))
    frontend_hits = sum(1 for path in normalized_paths if path.startswith("frontend/"))
    app_hits = sum(
        1
        for path in normalized_paths
        if path.startswith("src/")
        or path.endswith((".cs", ".xaml", ".csproj", ".sln"))
        or "/viewmodels/" in path
        or "/views/" in path
    )
    automation_hits = sum(
        1
        for path in normalized_paths
        if path.startswith("scripts/")
        or path.endswith((".ps1", ".cmd", ".bat"))
        or path.endswith(".json")
    )
    deploy_hits = sum(1 for path in normalized_paths if path.startswith("installer/") or "publish.ps1" in path or "build-installer" in path)

    if uds_hits >= 3 and (quality_hits >= 2 or test_hits >= 3):
        return "uds_quality"
    if uds_hits >= 3:
        return "uds_enhancement"
    if profile_name == "desktop_app" and (app_hits >= 5 or (app_hits >= 3 and deploy_hits >= 1)):
        return "app_bootstrap" if len(changed_files) >= 30 else "feature"
    if profile_name == "reporting_automation" and automation_hits >= 3:
        return "automation_build"
    if any(word in text for word in ("fix", "bug", "error", "hotfix")):
        return "bugfix"
    if any(word in text for word in ("refactor", "cleanup")):
        return "refactor"
    if any(word in text for word in ("test", "qa")):
        return "test"
    if backend_hits + frontend_hits >= 4:
        return "feature"
    if docs_hits >= max(app_hits, automation_hits, backend_hits + frontend_hits, 3):
        return "documentation"
    if app_hits >= 3:
        return "app_bootstrap"
    if automation_hits >= 3:
        return "automation_build"
    return "maintenance"


def work_type_label(work_type: str) -> str:
    mapping = {
        "uds_quality": "UDS 생성 및 품질 개선",
        "uds_enhancement": "UDS 생성 고도화",
        "app_bootstrap": "앱 초기 구축",
        "automation_build": "자동화 구축",
        "bugfix": "버그 수정",
        "refactor": "구조 개선",
        "test": "테스트 보강",
        "documentation": "문서화",
        "feature": "기능 개발",
        "maintenance": "유지보수",
    }
    return mapping.get(work_type, work_type)


def infer_change_facets(changed_files: list[str], commits: list[Commit], diff_summary: dict[str, Any]) -> list[dict[str, str]]:
    text = " ".join(commit.subject.lower() for commit in commits)
    normalized_paths = [path.replace("\\", "/").lower() for path in changed_files]
    top_files = [str(item.get("path", "")).replace("\\", "/").lower() for item in (diff_summary.get("top_files") or [])]
    all_paths = normalized_paths + top_files
    app_paths = [
        path
        for path in all_paths
        if path.startswith("src/")
        or path.endswith((".cs", ".xaml", ".csproj", ".sln", ".slnx"))
        or "/viewmodels/" in path
        or "/views/" in path
    ]
    automation_paths = [
        path
        for path in all_paths
        if path.startswith("scripts/")
        or path.endswith(".ps1")
        or path.endswith(".cmd")
        or path.endswith(".bat")
        or "startup" in path
        or "schedule" in path
    ]

    facets: list[dict[str, str]] = []

    def add(name: str, reason: str) -> None:
        if any(item["name"] == name for item in facets):
            return
        facets.append({"name": name, "reason": reason})

    if any("uds" in path for path in all_paths):
        add("UDS", "UDS 생성, 분석, 문서화 관련 경로 변경이 감지되었습니다.")
    if any(any(token in path for token in ("quality", "validation", "coverage", "baseline", "compare")) for path in all_paths):
        add("품질", "품질 평가, 검증, 커버리지 관련 변경이 포함되었습니다.")
    if app_paths:
        add("앱", "데스크톱 애플리케이션 구조, 화면, 디바이스 연동 관련 소스 변경이 포함되었습니다.")
    if automation_paths:
        add("자동화", "스크립트, 스케줄링, 시작 프로그램 연동 등 자동 실행 경로 변경이 감지되었습니다.")
    if any(word in text for word in ("feature", "add", "implement", "create", "신규", "추가")):
        add("기능", "커밋 메시지에 신규 기능 또는 추가 작업 표현이 포함되었습니다.")
    if any(word in text for word in ("fix", "bug", "error", "hotfix", "resolve", "수정", "오류")):
        add("버그수정", "커밋 메시지에 수정 또는 오류 대응 표현이 포함되었습니다.")
    if any(word in text for word in ("refactor", "cleanup", "restructure", "architecture", "구조", "리팩터")):
        add("구조개선", "커밋 메시지에 구조 정리 또는 리팩터링 표현이 포함되었습니다.")
    if any(path.startswith("frontend/") for path in all_paths) or any(word in text for word in ("ui", "ux", "screen", "layout")):
        add("UI", "프론트엔드 경로 또는 화면 관련 변경이 감지되었습니다.")
    if any(path.startswith("backend/") or "/api/" in path for path in all_paths) or any(word in text for word in ("api", "endpoint", "server")):
        add("API", "백엔드 또는 API 관련 경로가 변경되었습니다.")
    if any("/config" in path or path.endswith((".json", ".yaml", ".yml", ".ini", ".toml", ".env")) for path in all_paths):
        add("설정", "설정 파일 또는 구성 경로 변경이 감지되었습니다.")
    if any(path.startswith("tests/") or "/test_" in path or path.endswith("_test.py") for path in all_paths) or any(word in text for word in ("test", "qa", "검증")):
        add("테스트", "테스트 파일 또는 검증 관련 변경이 포함되었습니다.")
    if any(path.endswith(".md") or path.startswith("docs/") or path.startswith("project_docs/") for path in all_paths):
        add("문서", "문서 파일 또는 문서 디렉터리 변경이 포함되었습니다.")
    if any(path.startswith("installer/") or path.startswith(".github/") or "docker" in path or "build" in path or "deploy" in path for path in all_paths):
        add("배포", "배포, 빌드, 설치 관련 경로 변경이 감지되었습니다.")
    if any(word in text for word in ("performance", "optimize", "speed", "latency", "성능")):
        add("성능", "성능 개선 관련 표현이 커밋 메시지에 포함되었습니다.")

    if not facets:
        add("유지보수", "경로와 커밋 이력을 기준으로 일반 유지보수 작업으로 분류했습니다.")
    return facets[:6]


def split_change_facets(
    facets: list[dict[str, str]],
    work_type: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not facets:
        return [], []

    primary_keywords_by_work_type: dict[str, set[str]] = {
        "uds_quality": {"UDS", "품질", "테스트", "API", "기능", "구조개선"},
        "uds_enhancement": {"UDS", "기능", "API", "품질", "테스트"},
        "app_bootstrap": {"앱", "기능", "UI", "API", "구조개선", "품질", "배포"},
        "automation_build": {"자동화", "기능", "구조개선", "설정", "API", "테스트"},
        "feature": {"기능", "UI", "API", "구조개선", "품질"},
        "bugfix": {"버그수정", "품질", "테스트", "API", "UI"},
        "refactor": {"구조개선", "품질", "API", "테스트"},
        "test": {"테스트", "품질", "API", "기능"},
        "documentation": {"문서", "설정"},
        "maintenance": {"유지보수", "설정", "문서"},
    }
    fallback_secondary = {"문서", "설정", "배포"}
    primary_names = primary_keywords_by_work_type.get(work_type, {"기능", "API", "UI", "품질", "구조개선"})

    major: list[dict[str, str]] = []
    support: list[dict[str, str]] = []

    for item in facets:
        name = str(item.get("name", ""))
        if name in primary_names:
            major.append(item)
        else:
            support.append(item)

    if not major:
        for item in facets:
            name = str(item.get("name", ""))
            if name not in fallback_secondary:
                major.append(item)
                break

    if not major and facets:
        major.append(facets[0])

    support = [item for item in facets if item not in major]
    return major[:3], support[:3]


def infer_source_insights(changed_files: list[str], diff_summary: dict[str, Any]) -> list[str]:
    normalized_paths = [path.replace("\\", "/") for path in changed_files]
    insights: list[str] = []

    def top_matches(predicate, limit: int = 4) -> list[str]:
        return [path for path in normalized_paths if predicate(path.lower())][:limit]

    uds_paths = top_matches(lambda path: "uds" in path)
    quality_paths = top_matches(lambda path: any(token in path for token in ("quality", "validation", "coverage", "baseline", "compare")))
    test_paths = top_matches(lambda path: path.startswith("tests/") or "/test_" in path or path.endswith("_test.py"))
    parser_paths = top_matches(lambda path: any(token in path for token in ("parser", "analyzer", "source_parser", "function_analyzer", "impact_analysis")))

    if uds_paths:
        insights.append(
            f"UDS 생성 흐름이 확장되었습니다. 근거 파일: {', '.join(uds_paths[:4])}"
        )
    if quality_paths:
        insights.append(
            f"품질 평가와 검증 루프가 강화되었습니다. 근거 파일: {', '.join(quality_paths[:4])}"
        )
    if test_paths:
        insights.append(
            f"테스트와 회귀 검증 범위가 넓어졌습니다. 근거 파일: {', '.join(test_paths[:4])}"
        )
    if parser_paths:
        insights.append(
            f"소스 파싱과 영향 분석 로직이 보강되었습니다. 근거 파일: {', '.join(parser_paths[:4])}"
        )

    top_files = diff_summary.get("top_files") or []
    if top_files:
        major = top_files[:3]
        insights.append(
            "변경량이 큰 핵심 파일: " + ", ".join(
                f"{item.get('path', '')} (+{int(item.get('added', 0))}/-{int(item.get('deleted', 0))})"
                for item in major
            )
        )

    return insights[:5]


def build_context_payload(
    *,
    today: date,
    report_type: str,
    window: ReportWindow,
    repo_root: Path,
    branch: str,
    remote_url: str,
    upstream: str | None,
    sync_state: tuple[int, int] | None,
    commits: list[Commit],
    changed_files: list[str],
    uncommitted: list[str],
    github_meta: dict[str, Any],
    profile_name: str,
) -> dict[str, Any]:
    diff_summary = summarize_diff_stats(
        get_diff_numstat(repo_root, branch, window.start, window.end)
    )
    change_facets = infer_change_facets(changed_files, commits, diff_summary)
    work_type = infer_work_type(changed_files, commits, profile_name)
    primary_change_facets, supporting_change_facets = split_change_facets(change_facets, work_type)
    source_insights = infer_source_insights(changed_files, diff_summary)
    domain_profile = get_domain_profile(profile_name)
    auto_commit_status = load_auto_commit_status(repo_root.name, window.end)
    return {
        "today": today.isoformat(),
        "report_type": report_type,
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        "repository": repo_root.name,
        "repo_root": str(repo_root),
        "domain_profile": profile_name,
        "domain_profile_name": domain_profile["name"],
        "domain_focus": list(domain_profile["focus"]),
        "branch": branch,
        "remote_url": remote_url,
        "upstream": upstream or "",
        "sync_status": {"ahead": sync_state[0] if sync_state else 0, "behind": sync_state[1] if sync_state else 0},
        "commit_count": len(commits),
        "changed_file_count": len(changed_files),
        "uncommitted_count": len(uncommitted),
        "work_type": work_type,
        "change_facets": change_facets,
        "primary_change_facets": primary_change_facets,
        "supporting_change_facets": supporting_change_facets,
        "source_insights": source_insights,
        "auto_commit_status": auto_commit_status or {},
        "top_areas": [{"area": area, "count": count} for area, count in top_directories(changed_files, limit=8)],
        "diff_summary": diff_summary,
        "recent_commits": [
            {"hash": c.short_hash, "time": c.authored_at, "author": c.author, "subject": c.subject}
            for c in commits[:20]
        ],
        "changed_files": changed_files[:80],
        "changed_docs": changed_markdown_docs(changed_files)[:20],
        "uncommitted": uncommitted[:30],
        "github": github_meta,
        "sprint_tasks": match_commits_to_tasks(
            [{"hash": c.short_hash, "time": c.authored_at, "author": c.author, "subject": c.subject} for c in commits[:20]],
            changed_files,
            load_sprint_tasks(),
            today,
        ),
    }


def _build_sprint_summary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Build sprint task summary with completion details for weekly/monthly reports."""
    sprint_tasks = list(payload.get("sprint_tasks") or [])
    if not sprint_tasks:
        return []
    summary = []
    for task in sprint_tasks:
        subtasks = task.get("subtasks", [])
        done_subtasks = [s for s in subtasks if s.get("status") == "done"]
        in_progress_subtasks = [s for s in subtasks if s.get("status") == "in_progress"]
        completion_details = []
        for s in done_subtasks:
            completion_details.append(f"{s['title']}: {s.get('description', '')}")
        summary.append({
            "key": task["key"],
            "title": task["title"],
            "status": task["status"],
            "subtask_progress": task.get("subtask_progress", f"{len(done_subtasks)}/{len(subtasks)}"),
            "completion_details": completion_details,
            "in_progress_details": [s["title"] for s in in_progress_subtasks],
            "related_commits": task.get("related_commits", []),
        })
    return summary


def generate_jira_suggestions(
    payload: dict[str, Any],
    ai_sections: dict[str, Any] | None = None,
    max_suggestions: int = 10,
) -> list[dict[str, Any]]:
    """Generate actionable Jira suggestions from commit-task matching and AI analysis.

    Returns a list of suggestion dicts with id, task_key, type, title,
    suggested_text, reason, confidence, status fields.
    """
    # Always prefer Jira live data for suggestions (match_commits_to_tasks
    # overrides status based on dates, which diverges from actual Jira state)
    sprint_tasks = []
    try:
        from workflow.task_provider import get_task_provider
        _sp_path = Path(__file__).resolve().parent / "startup_projects.json"
        _pcs = []
        if _sp_path.exists():
            with open(_sp_path, encoding="utf-8") as _f:
                _pcs = json.load(_f).get("projects", [])
        for _pc in _pcs:
            if isinstance(_pc.get("jira"), dict):
                provider = get_task_provider(_pc)
                live_data = provider.get_tasks()
                sprint_tasks = live_data.get("tasks", [])
                break
    except Exception:
        pass
    # Fallback to payload data if Jira unavailable
    if not sprint_tasks:
        sprint_tasks = list(payload.get("sprint_tasks") or [])
    if not sprint_tasks:
        return []

    suggestions: list[dict[str, Any]] = []
    sid = 0
    today = date.today()

    # Build commit evidence — use sprint-wide commits, not just daily
    all_commits = [c.get("subject", "") for c in (payload.get("recent_commits") or [])]
    # Extend with git log from sprint period if available
    try:
        repo_root = Path(payload.get("repo_root", ""))
        if repo_root.exists():
            import subprocess
            sprint_start = sprint_tasks[0].get("start", "") if sprint_tasks else ""
            if sprint_start:
                result = subprocess.run(
                    ["git", "log", f"--since={sprint_start}", "--format=%s", "-50"],
                    cwd=str(repo_root), capture_output=True, text=True, timeout=5,
                    encoding="utf-8", errors="replace",
                )
                if result.returncode == 0:
                    git_commits = [l.strip() for l in result.stdout.splitlines() if l.strip()]
                    all_commits = list(dict.fromkeys(git_commits + all_commits))  # dedupe
    except Exception:
        pass
    local_sprint = {}
    try:
        _local_path = Path(__file__).resolve().parent / "sprint_tasks.json"
        if _local_path.exists():
            with open(_local_path, encoding="utf-8") as _f:
                local_sprint = {t["key"]: t for t in json.load(_f).get("tasks", []) if t.get("key")}
    except Exception:
        pass

    # Noise patterns — skip these commits in suggestions
    _NOISE_PREFIXES = ("chore(auto)", "chore:", "merge", "fix gitlab ci", "fix ci", "skip hanging")
    _NOISE_KEYWORDS = {"ci", "build", "fix", "merge", "snapshot", "chore"}

    def _is_noise_commit(subj: str) -> bool:
        sl = subj.lower().strip()
        return any(sl.startswith(p) for p in _NOISE_PREFIXES)

    def _match_commits_for(task_title: str, task_key: str) -> list[str]:
        """Find commits relevant to a task by keyword matching."""
        kw_task = local_sprint.get(task_key, {})
        keywords = [e.get("word", "").lower() for e in kw_task.get("keywords", [])
                     if e.get("word") and e.get("word", "").lower() not in _NOISE_KEYWORDS]
        # Also use words from the title (exclude generic words)
        title_words = [w.lower() for w in task_title.split()
                       if len(w) >= 3 and w.lower() not in ("및", "위한", "통한", "결과")]
        search_terms = set(keywords + title_words)
        if not search_terms:
            return []
        matched = []
        for subj in all_commits:
            if _is_noise_commit(subj):
                continue
            subj_lower = subj.lower()
            if any(term in subj_lower for term in search_terms):
                matched.append(subj)
        return matched[:5]

    def _summarize_commits(commits: list[str]) -> str:
        """Create a short summary from commit messages."""
        if not commits:
            return ""
        # Deduplicate and trim
        unique = list(dict.fromkeys(commits))[:3]
        return "; ".join(c[:60] for c in unique)

    for task in sprint_tasks:
        key = task.get("key", "")
        if not key:
            continue

        title = task.get("title", "")
        status = task.get("status", "")
        subtasks = task.get("subtasks", [])
        done_subs = [s for s in subtasks if s.get("status") == "done"]
        in_prog_subs = [s for s in subtasks if s.get("status") == "in_progress"]
        pending_subs = [s for s in subtasks if s.get("status") == "pending"]
        is_in_progress = status in ("진행 중", "in_progress")
        is_pending = status in ("예정", "pending")
        is_done = status in ("완료", "done")
        t_start = task.get("start", "")
        t_end = task.get("end", "")

        if is_done:
            continue

        # ── 하위작업 개별 제안 ──
        for sub in subtasks:
            skey = sub.get("key", "")
            stitle = sub.get("title", "")
            sst = sub.get("status", "")
            if not skey:
                continue

            # 진행 중 부작업 → 완료 처리 제안 (커밋 + description 기반 구체적 결과)
            if sst == "in_progress":
                sid += 1
                related = _match_commits_for(stitle, key)
                summary = _summarize_commits(related)
                # Get description from local sprint_tasks.json
                desc = sub.get("description", "")
                if not desc:
                    local_task = local_sprint.get(key, {})
                    for ls in local_task.get("subtasks", []):
                        if ls.get("title") == stitle:
                            desc = ls.get("description", "")
                            break
                if desc:
                    text = f"{stitle} 완료. {desc}"
                else:
                    text = f"{stitle} 완료."
                suggestions.append({
                    "id": f"s{sid}",
                    "task_key": skey,
                    "type": "complete",
                    "title": f"  └ {stitle} — 완료 처리",
                    "suggested_text": text,
                    "reason": f"{key} 하위작업, 현재 진행 중",
                    "confidence": "medium",
                    "status": "pending",
                })

            # pending 부작업 + 상위 시작일 도래 → 시작 제안
            elif sst == "pending" and t_start:
                try:
                    if date.fromisoformat(t_start) <= today:
                        sid += 1
                        suggestions.append({
                            "id": f"s{sid}",
                            "task_key": skey,
                            "type": "transition",
                            "title": f"  └ {stitle} — 작업 시작",
                            "suggested_text": f"상위 작업({key}) 시작일 도래. 작업을 시작합니다.",
                            "reason": f"시작일 {t_start} ≤ 오늘",
                            "confidence": "low",
                            "status": "pending",
                        })
                except ValueError:
                    pass

        # ── 상위 작업 제안 ──

        # Rule 1: 부작업 전부 done → 상위 완료 보고
        if subtasks and len(done_subs) == len(subtasks) and is_in_progress:
            sid += 1
            sub_lines = []
            for s in done_subs:
                local_task = local_sprint.get(key, {})
                desc = ""
                for ls in local_task.get("subtasks", []):
                    if ls.get("title") == s.get("title"):
                        desc = ls.get("description", "")
                        break
                sub_lines.append(f"- {s.get('title', '')}: {desc or '완료'}")
            sub_results = "\n".join(sub_lines)
            suggestions.append({
                "id": f"s{sid}",
                "task_key": key,
                "type": "complete",
                "title": f"{title} — 전체 완료 보고",
                "suggested_text": f"전체 하위작업 완료.\n{sub_results}\n종료 요청합니다.",
                "reason": f"부작업 {len(done_subs)}/{len(subtasks)} 완료",
                "confidence": "high",
                "status": "pending",
            })
            continue

        # Rule 2: 기한 초과 → 완료 처리 제안
        if t_end and is_in_progress:
            try:
                end_date = date.fromisoformat(t_end)
                if end_date < today:
                    sid += 1
                    days_over = (today - end_date).days
                    sub_status_lines = []
                    for s in subtasks:
                        st_label = {"done": "완료", "in_progress": "진행 중", "pending": "대기"}.get(s.get("status", ""), "?")
                        local_task = local_sprint.get(key, {})
                        detail = ""
                        for ls in local_task.get("subtasks", []):
                            if ls.get("title") == s.get("title") and ls.get("description"):
                                detail = f" ({ls['description'][:50]})"
                                break
                        sub_status_lines.append(f"- {s.get('title', '')}: {st_label}{detail}")
                    sub_report = "\n".join(sub_status_lines) if sub_status_lines else ""
                    suggestions.append({
                        "id": f"s{sid}",
                        "task_key": key,
                        "type": "complete",
                        "title": f"{title} — 기한 초과 ({days_over}일), 완료 처리",
                        "suggested_text": f"기한({t_end}) 대비 {days_over}일 경과.\n{sub_report}\n종료 요청합니다.",
                        "reason": f"종료일 {t_end} 경과 ({len(done_subs)}/{len(subtasks)} 부작업 완료)",
                        "confidence": "high",
                        "status": "pending",
                    })
                    continue
            except ValueError:
                pass

        # Rule 3: 시작일 도래 + pending → 진행 중 전환
        if is_pending and t_start:
            try:
                if date.fromisoformat(t_start) <= today:
                    sid += 1
                    suggestions.append({
                        "id": f"s{sid}",
                        "task_key": key,
                        "type": "transition",
                        "title": f"{title} — 작업 시작",
                        "suggested_text": f"시작일({t_start}) 도래. 작업을 시작합니다.",
                        "reason": f"시작일 {t_start} ≤ 오늘 {today.isoformat()}",
                        "confidence": "high",
                        "status": "pending",
                    })
                    continue
            except ValueError:
                pass

    # ── 커밋 기반 새 작업/하위작업 추가 제안 ──
    # Collect all keywords from all tasks
    all_keywords: set[str] = set()
    for lt in local_sprint.values():
        for kw in lt.get("keywords", []):
            w = kw.get("word", "").lower()
            if w:
                all_keywords.add(w)
        for st in lt.get("subtasks", []):
            for w in st.get("title", "").lower().split():
                if len(w) >= 3 and w not in ("및", "위한", "통한"):
                    all_keywords.add(w)

    # Find feature/fix commits that don't match any existing task keyword
    unmatched_commits: list[str] = []
    for subj in all_commits:
        if _is_noise_commit(subj):
            continue
        subj_lower = subj.lower()
        if not any(kw in subj_lower for kw in all_keywords):
            unmatched_commits.append(subj)

    # Group unmatched commits and suggest subtask additions
    if unmatched_commits and len(suggestions) < max_suggestions:
        # Find best parent: in_progress task closest to today
        best_parent = None
        for task in sprint_tasks:
            if task.get("status") in ("진행 중", "in_progress"):
                best_parent = task
                break
        if not best_parent:
            for task in sprint_tasks:
                if task.get("status") in ("예정", "pending"):
                    best_parent = task
                    break

        # Also suggest for commits that DO match a task but NOT any subtask
        # → suggests adding a new subtask for that specific area
        for commit_subj in unmatched_commits[:3]:
            if len(suggestions) >= max_suggestions:
                break
            # Clean commit message: remove conventional commit prefix
            clean = commit_subj
            for prefix in ("feat: ", "fix: ", "refactor: ", "test: ", "chore: ", "docs: "):
                if clean.lower().startswith(prefix):
                    clean = clean[len(prefix):]
                    break
            clean = clean[:60]

            if best_parent:
                sid += 1
                suggestions.append({
                    "id": f"s{sid}",
                    "task_key": best_parent["key"],
                    "type": "add_subtask",
                    "title": f"{best_parent['title']} — 하위작업 추가",
                    "suggested_text": clean,
                    "reason": f"커밋 \"{commit_subj[:50]}\" 이 기존 태스크에 매칭되지 않음",
                    "confidence": "low",
                    "status": "pending",
                })

    # Find commits matching a parent task but not covered by any subtask
    for task in sprint_tasks:
        if len(suggestions) >= max_suggestions:
            break
        tkey = task.get("key", "")
        ttitle = task.get("title", "")
        if task.get("status") in ("완료", "done"):
            continue
        task_commits = _match_commits_for(ttitle, tkey)
        if not task_commits:
            continue
        # Check which commits are NOT covered by any subtask title
        existing_sub_words = set()
        for s in task.get("subtasks", []):
            for w in s.get("title", "").lower().split():
                if len(w) >= 3:
                    existing_sub_words.add(w)
        for tc in task_commits[:2]:
            tc_words = set(w.lower() for w in tc.split() if len(w) >= 3)
            # If commit has significant words not in any subtask → new area
            new_words = tc_words - existing_sub_words - {"feat", "fix", "chore", "test", "refactor"}
            if len(new_words) >= 2:
                clean = tc
                for prefix in ("feat: ", "fix: ", "refactor: ", "test: ", "chore: ", "docs: "):
                    if clean.lower().startswith(prefix):
                        clean = clean[len(prefix):]
                        break
                sid += 1
                suggestions.append({
                    "id": f"s{sid}",
                    "task_key": tkey,
                    "type": "add_subtask",
                    "title": f"{ttitle} — 하위작업 추가",
                    "suggested_text": clean[:60],
                    "reason": f"커밋이 {tkey} 매칭되나 기존 부작업에 없는 영역",
                    "confidence": "medium",
                    "status": "pending",
                })
                break

    return suggestions[:max_suggestions]


def build_fallback_jira_doc(doc_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    areas = payload["top_areas"]
    commits = payload["recent_commits"]
    work_type = work_type_label(payload["work_type"])
    source_insights = list(payload.get("source_insights") or [])
    sprint_tasks = list(payload.get("sprint_tasks") or [])

    # Sprint task 기반 completed / in_progress / remaining 분류
    completed_tasks = []
    in_progress_tasks = []
    remaining_tasks = []
    for task in sprint_tasks:
        entry = f"[{task['key']}] {task['title']}"
        if task.get("related_commits"):
            entry += f" — {', '.join(task['related_commits'][:2])}"
        if task["status"] == "완료":
            # 완료 상세 내역 추가
            done_subtasks = [s for s in task.get("subtasks", []) if s.get("status") == "done"]
            completed_tasks.append(entry)
            for s in done_subtasks:
                completed_tasks.append(f"  ✅ {s['title']}: {s.get('description', '')}")
        elif task["status"] == "진행 중":
            if task["hit_count"] > 0:
                in_progress_tasks.append(entry)
            else:
                remaining_tasks.append(entry)
        else:
            remaining_tasks.append(entry)

    # 커밋 기반 보충
    if not completed_tasks and not in_progress_tasks:
        completed_tasks = [c["subject"] for c in commits[:5]] or ["집계된 완료 항목이 없습니다."]

    # task_board: 작업별 하위작업 구조
    task_board = []
    for task in sprint_tasks:
        subtasks = task.get("subtasks", [])
        task_board.append({
            "key": task["key"],
            "title": task["title"],
            "status": task["status"],
            "period": f"{task['start']} ~ {task['end']}",
            "subtasks": subtasks,
            "subtask_progress": task.get("subtask_progress", f"0/{len(subtasks)}"),
            "related_commits": task.get("related_commits", []),
        })

    return {
        "title": f"[{work_type}] {payload['today']} 스프린트 현황",
        "summary": source_insights[0] if source_insights else f"{work_type} 유형 작업에 대한 스프린트 현황입니다.",
        "task_name": f"{payload['repository']} {work_type} 작업",
        "task_goal": "Jira 스프린트 작업 기반으로 계획, 진행, 완료 현황을 추적합니다.",
        "scope": [f"[{t['key']}] {t['title']}" for t in sprint_tasks if t["status"] == "진행 중"][:4] or ["스프린트 작업 범위 확인 필요"],
        "completed": completed_tasks or ["완료된 작업이 없습니다."],
        "in_progress": in_progress_tasks or ["진행 중인 작업이 없습니다."],
        "remaining": remaining_tasks or ["잔여 작업이 없습니다."],
        "task_board": task_board,
        "validation": ["커밋-작업 매핑 확인", "하위작업 완료 여부 점검"],
        "risks": ["미커밋 변경이 남아 있으면 결과 정리에 추가 확인이 필요합니다."] if payload["uncommitted_count"] else ["즉시 보이는 로컬 미커밋 변경은 없습니다."],
        "links": [item.get("html_url", "") for item in payload.get("github", {}).get("commits", [])[:5] if item.get("html_url")] or [],
        "status_summary": {
            "completed_count": len(completed_tasks),
            "in_progress_count": len(in_progress_tasks),
            "remaining_count": len(remaining_tasks),
        },
    }

def format_top_file_signal(payload: dict[str, Any], limit: int = 3) -> list[str]:
    diff_summary = payload.get("diff_summary") or {}
    items = diff_summary.get("top_files") or []
    return [
        f"{item.get('path', '')} (+{int(item.get('added', 0))}/-{int(item.get('deleted', 0))})"
        for item in items[:limit]
    ]


def format_area_signal(payload: dict[str, Any], limit: int = 3) -> list[str]:
    return [f"{item['area']} ({item['count']} files)" for item in (payload.get("top_areas") or [])[:limit]]


def build_fallback_sections(report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    commits = payload["recent_commits"]
    areas = payload["top_areas"]
    uncommitted_count = payload["uncommitted_count"]
    work_type = work_type_label(payload["work_type"])
    diff = payload.get("diff_summary") or {}
    top_files = format_top_file_signal(payload, limit=3)
    area_signals = format_area_signal(payload, limit=3)
    commit_count = int(payload.get("commit_count", 0))
    changed_count = int(payload.get("changed_file_count", 0))
    total_added = int(diff.get("total_added", 0))
    total_deleted = int(diff.get("total_deleted", 0))
    source_insights = list(payload.get("source_insights") or [])

    if report_type == "daily":
        return {
            "title": f"데일리 리포트 - {payload['today']}",
            "summary": [
                f"{payload['window_start']}~{payload['window_end']} 기준 커밋 {commit_count}건, 변경 파일 {changed_count}건, 라인 변화 +{total_added}/-{total_deleted}가 확인됐습니다.",
                f"이번 작업은 {work_type} 유형으로 분류되며 중심 변경 영역은 {', '.join(area_signals) if area_signals else '주요 변경 영역 없음'} 입니다.",
                *source_insights[:2],
                f"가장 영향이 큰 파일은 {top_files[0]} 입니다." if top_files else "상위 영향 파일은 아직 집계되지 않았습니다.",
                *(f"최근 커밋: {entry['subject']}" for entry in commits[:2]),
            ][:6],
            "completed": [
                *source_insights[:2],
                *(f"{entry['subject']} · {entry['author']} · {entry['time']}" for entry in commits[:5]),
                *(f"핵심 영향 파일: {item}" for item in top_files[:2]),
            ][:7] or ["집계 구간 내 완료된 변경이 없습니다."],
            "focus": [
                *(f"소스 근거 확인: {item}" for item in source_insights[:2]),
                *(f"{item['area']} 영역 점검 및 후속 검증 정리 ({item['count']} files)" for item in areas[:3]),
                *(f"우선 검토 파일: {item}" for item in top_files[:2]),
            ][:6] or ["오늘 집중할 핵심 변경 영역을 다시 정리해야 합니다."],
            "risks": [
                (f"미커밋 변경 {uncommitted_count}건이 남아 있어 결과 정리와 Jira 반영 전에 추가 확인이 필요합니다." if uncommitted_count else "즉시 보이는 로컬 미커밋 변경 리스크는 없습니다."),
                (f"상위 영향 파일 {top_files[0]} 중심 회귀 검증이 필요합니다." if top_files else "영향 파일 기준의 추가 검증 포인트는 제한적입니다."),
                (f"변경이 {', '.join(area_signals[:2])}에 집중돼 있어 연관 기능 회귀 가능성을 점검해야 합니다." if len(area_signals) >= 2 else "주요 변경 영역에 대한 기본 점검은 계속 필요합니다."),
            ][:4],
            "next_actions": [
                *(f"{item['area']} 영역 검증 결과와 후속 조치를 정리합니다." for item in areas[:3]),
                ("미커밋 변경을 정리한 뒤 Jira 계획과 결과 문서를 갱신합니다." if uncommitted_count else "Jira 계획과 결과 문서를 최신 기준으로 유지합니다."),
                *(f"핵심 파일 리뷰 마감: {item}" for item in top_files[:1]),
            ][:5],
        }
    if report_type == "plan":
        return {
            "title": f"진행 계획서 - {payload['today']}",
            "summary": [
                f"최근 변경 이력을 기준으로 {work_type} 유형 작업 계획 초안을 작성했습니다.",
                f"우선 점검 대상 영역은 {', '.join(area_signals) if area_signals else '주요 변경 영역 없음'} 입니다.",
                *source_insights[:2],
                (f"핵심 영향 파일 {top_files[0]} 기준으로 검토 순서를 잡는 것이 좋습니다." if top_files else "영향 파일 기준 추가 정리가 필요합니다."),
            ],
            "priority_actions": [
                ("미커밋 변경을 먼저 정리하고 커밋 단위를 분리합니다." if uncommitted_count else "최근 변경사항의 검증 범위와 결과를 먼저 확정합니다."),
                *(f"핵심 변경 해석 반영: {item}" for item in source_insights[:2]),
                *(f"{item['area']} 영역 작업 범위와 Jira 하위작업을 정리합니다." for item in areas[:3]),
                *(f"우선 리뷰 파일: {item}" for item in top_files[:2]),
            ][:6],
            "mid_term_actions": [f"{item['area']} 영역 문서, 테스트, 검증 기록을 보강합니다." for item in areas[:3]] or ["다음 요구사항 후보와 중기 작업을 재정리합니다."],
            "risks": [
                f"변경 파일 {changed_count}건 규모이므로 검증 누락 없이 영역별 점검이 필요합니다.",
                ("미커밋 변경이 남아 있어 작업 경계가 흐려질 수 있습니다." if uncommitted_count else "현재 기준 큰 작업 경계 이슈는 보이지 않습니다."),
            ],
            "notes": [
                "자동 생성 초안이므로 실제 Jira 우선순위와 담당 범위에 맞춰 조정해야 합니다.",
                (f"상위 영향 파일: {', '.join(top_files[:2])}" if top_files else "상위 영향 파일 정보는 아직 제한적입니다."),
            ],
        }
    if report_type == "weekly":
        result = {
            "title": f"주간 리포트 - {payload['window_start']} to {payload['window_end']}",
            "summary": [
                f"주간 기준 커밋 {commit_count}건, 변경 파일 {changed_count}건, 라인 변화 +{total_added}/-{total_deleted}가 누적되었습니다.",
                f"이번 주 중심 영역은 {', '.join(area_signals) if area_signals else '주요 변경 영역 없음'} 입니다.",
                *source_insights[:2],
            ],
            "highlights": [*source_insights[:2], *[f"{entry['subject']} · {entry['author']}" for entry in commits[:5]]] or ["이번 주 집계된 커밋이 없습니다."],
            "areas": [*source_insights[:3], *[f"{item['area']} {item['count']}건 변경 · 상위 파일 리뷰 필요" for item in areas[:5]]] or ["주요 변경 영역이 없습니다."],
            "risks": [
                (f"상위 영향 파일 {top_files[0]} 중심 회귀 검증이 필요합니다." if top_files else "상위 영향 파일 기준 추가 검증 포인트는 제한적입니다."),
                "다음 주 초반에는 이번 주 누적 변경의 결과 정리와 검증 마감이 필요합니다.",
            ],
            "next_week": [f"{item['area']} 영역 일정 정리 및 검증 완료" for item in areas[:3]] or ["다음 주 우선순위와 검증 계획을 다시 정리합니다."],
        }
        result["sprint_summary"] = _build_sprint_summary(payload)
        return result
    result = {
        "title": f"월간 리포트 - {payload['window_start']} to {payload['window_end']}",
        "summary": [
            f"월간 기준 커밋 {commit_count}건, 변경 파일 {changed_count}건, 라인 변화 +{total_added}/-{total_deleted}가 누적되었습니다.",
            f"월간 중심 영역은 {', '.join(area_signals) if area_signals else '주요 변경 영역 없음'} 입니다.",
            *source_insights[:2],
        ],
        "highlights": [*source_insights[:2], *[f"{entry['subject']} · {entry['author']}" for entry in commits[:6]]] or ["이번 달 집계된 커밋이 없습니다."],
        "areas": [*source_insights[:3], *[f"{item['area']} {item['count']}건 변경 · 구조 점검 필요" for item in areas[:6]]] or ["주요 변경 영역이 없습니다."],
        "risks": [
            "반복 변경 영역은 설계 문서와 구조 점검을 함께 보강해야 합니다.",
            (f"상위 영향 파일 {top_files[0]} 기준으로 다음 달 우선순위를 정리해야 합니다." if top_files else "상위 영향 파일 기준 다음 달 우선순위 정리가 필요합니다."),
        ],
        "next_month": [f"{item['area']} 영역 구조 안정화와 검증 계획을 수립합니다." for item in areas[:3]] or ["다음 달 우선순위와 검증 계획을 다시 정리합니다."],
    }
    result["sprint_summary"] = _build_sprint_summary(payload)
    return result

def ask_gemini_for_sections(report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    cfg = choose_gemini_config()
    if not cfg:
        raise RuntimeError("No Gemini config available")
    adapter = get_adapter(cfg)
    schemas = {
        "daily": '{"title": str, "summary": [str], "completed": [str], "focus": [str], "risks": [str], "next_actions": [str]}',
        "plan": '{"title": str, "summary": [str], "priority_actions": [str], "mid_term_actions": [str], "risks": [str], "notes": [str]}',
        "weekly": '{"title": str, "summary": [str], "highlights": [str], "areas": [str], "risks": [str], "next_week": [str]}',
        "monthly": '{"title": str, "summary": [str], "highlights": [str], "areas": [str], "risks": [str], "next_month": [str]}',
        "jira": '{"title": str, "summary": str, "task_name": str, "task_goal": str, "scope": [str], "completed": [str], "in_progress": [str], "remaining": [str], "task_board": [{"key": str, "title": str, "status": str, "period": str, "subtasks": [str], "related_commits": [str]}], "validation": [str], "risks": [str], "links": [str], "status_summary": {"completed_count": int, "in_progress_count": int, "remaining_count": int}}',
    }
    system = (
        "You are an engineering reporting assistant. "
        "Write concise Korean project-management text based only on the provided context. "
        "Do not invent facts. Return JSON only."
    )
    user = (
        f"Generate a {report_type} document in Korean.\n"
        f"Required schema: {schemas[report_type]}\n"
        "Rules:\n"
        "- Use short, practical business language.\n"
        "- Reflect GitHub commit URLs or PRs when available.\n"
        "- Keep the work type framing consistent.\n"
        f"- Domain profile: {payload.get('domain_profile_name', '')}\n"
        f"- Domain focus: {', '.join(payload.get('domain_focus') or [])}\n"
        "- For jira, use the sprint_tasks data to map commits to APPL-xxx task keys. Show each task with its subtasks, status (완료/진행 중/예정), and related commits. Structure as task_board entries.\n"
        "- No markdown fence, JSON only.\n\n"
        f"Context JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    result = adapter.generate(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.3,
        max_tokens=4096,
        timeout=180.0,
    )
    data = json.loads(clean_json_block(result.get("output", "")))
    if not isinstance(data, dict):
        raise ValueError("LLM output is not a JSON object")
    return data


def build_fallback_ai_team_analysis(payload: dict[str, Any]) -> dict[str, list[str]]:
    source_insights = list(payload.get("source_insights") or [])
    areas = list(payload.get("top_areas") or [])
    diff = payload.get("diff_summary") or {}
    top_files = diff.get("top_files") or []
    return {
        "structure": [
            *(
                source_insights[:1]
                or [f"{payload.get('domain_profile_name', '프로젝트')} 기준 핵심 구조 변경을 상위 변경 영역과 영향 파일 기준으로 다시 정리해야 합니다."]
            ),
            *(f"{item['area']} 영역이 구조 변경 중심 축입니다. ({item['count']} files)" for item in areas[:2]),
        ][:3],
        "quality": [
            *(
                source_insights[1:2]
                or [f"{payload.get('domain_profile_name', '프로젝트')} 관점에서 품질, validation, coverage 관련 파일을 우선 검토해야 합니다."]
            ),
            f"{payload.get('domain_profile_name', '프로젝트')} 기준 검증 완료 조건을 정리합니다.",
            *(f"품질 영향 파일: {item.get('path', '')}" for item in top_files[:1]),
        ][:3],
        "feature": [
            *(
                source_insights[2:3]
                or [f"{payload.get('domain_profile_name', '프로젝트')} 기준 기능 영향은 화면/API/워크플로우 변경 파일과 연결해서 정리해야 합니다."]
            ),
            f"{payload.get('domain_profile_name', '프로젝트')} 사용자 흐름 또는 작업 흐름 변화가 있으면 영향도를 요약합니다.",
            *(f"기능 영향 파일: {item.get('path', '')}" for item in top_files[1:2]),
        ][:3],
        "jira_strategy": [
            f"상위 작업은 {payload.get('domain_profile_name', '프로젝트')}의 큰 변경 흐름 1개로 유지합니다.",
            *(f"{item['area']} 영역을 하위작업 단위로 정리합니다." for item in areas[:3]),
        ][:4],
    }


def ask_gemini_for_team_analysis(payload: dict[str, Any]) -> dict[str, list[str]]:
    cfg = choose_gemini_config()
    if not cfg:
        raise RuntimeError("No Gemini config available")
    adapter = get_adapter(cfg)
    system = (
        "You are a Gemini-based engineering reporting team. "
        "Act as four roles: structure analyst, quality analyst, feature analyst, and Jira planner. "
        "Use only the supplied context. Return JSON only."
    )
    user = (
        "Analyze the repository context in Korean.\n"
        'Required schema: {"structure":[str], "quality":[str], "feature":[str], "jira_strategy":[str]}\n'
        "Rules:\n"
        "- structure: explain how the source/code structure changed.\n"
        "- quality: explain how quality, validation, test, or coverage improved.\n"
        "- feature: explain user-facing or workflow-facing impact.\n"
        "- jira_strategy: explain parent task framing and grouped subtasks.\n"
        "- Keep each list to 2-4 concise bullets.\n"
        "- Mention concrete modules or paths when evidence is strong.\n"
        f"- Domain profile: {payload.get('domain_profile_name', '')}\n"
        f"- Domain focus: {', '.join(payload.get('domain_focus') or [])}\n"
        "- No markdown fence, JSON only.\n\n"
        f"Context JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    result = adapter.generate(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=3072,
        timeout=180.0,
    )
    data = json.loads(clean_json_block(result.get("output", "")))
    if not isinstance(data, dict):
        raise ValueError("LLM team analysis output is not a JSON object")
    return {
        "structure": [str(item) for item in (data.get("structure") or []) if str(item).strip()][:4],
        "quality": [str(item) for item in (data.get("quality") or []) if str(item).strip()][:4],
        "feature": [str(item) for item in (data.get("feature") or []) if str(item).strip()][:4],
        "jira_strategy": [str(item) for item in (data.get("jira_strategy") or []) if str(item).strip()][:4],
    }


def build_auto_commit_status_items(status: dict[str, Any]) -> list[str]:
    if not status:
        return []
    result = [f"상태: {status.get('status', 'unknown')}"]
    if status.get("branch"):
        result.append(f"브랜치: {status.get('branch')}")
    if status.get("commit"):
        result.append(f"커밋: {status.get('commit')}")
    if status.get("message"):
        result.append(f"메시지: {status.get('message')}")
    if status.get("changed_files") is not None:
        result.append(f"변경 파일 수: {status.get('changed_files')}")
    if status.get("error"):
        result.append(f"오류: {status.get('error')}")
    return result[:5]


def _render_sprint_summary(lines: list[str], sections: dict[str, Any], period_label: str) -> None:
    """Render sprint task summary into weekly/monthly markdown."""
    sprint_summary = list(sections.get("sprint_summary") or [])
    if not sprint_summary:
        return
    # 완료/진행 중 필터
    completed = [t for t in sprint_summary if t["status"] == "완료"]
    in_progress = [t for t in sprint_summary if t["status"] == "진행 중"]
    lines.extend(["", f"## {period_label} 스프린트 작업 현황", ""])
    if completed:
        lines.append(f"### 완료된 작업 ({len(completed)}건)")
        lines.append("")
        for t in completed:
            lines.append(f"**[{t['key']}] {t['title']}** ({t['subtask_progress']})")
            if t.get("completion_details"):
                lines.append("- 완료 내역:")
                for detail in t["completion_details"]:
                    lines.append(f"  - ✅ {detail}")
            if t.get("related_commits"):
                lines.append("- 관련 커밋:")
                for rc in t["related_commits"][:3]:
                    lines.append(f"  - {rc}")
            lines.append("")
    if in_progress:
        lines.append(f"### 진행 중인 작업 ({len(in_progress)}건)")
        lines.append("")
        for t in in_progress:
            lines.append(f"**[{t['key']}] {t['title']}** ({t['subtask_progress']})")
            if t.get("completion_details"):
                lines.append("- 완료된 하위작업:")
                for detail in t["completion_details"]:
                    lines.append(f"  - ✅ {detail}")
            if t.get("in_progress_details"):
                lines.append("- 진행 중:")
                for detail in t["in_progress_details"]:
                    lines.append(f"  - 🔄 {detail}")
            if t.get("related_commits"):
                lines.append("- 관련 커밋:")
                for rc in t["related_commits"][:3]:
                    lines.append(f"  - {rc}")
            lines.append("")


def render_report_markdown(report_type: str, sections: dict[str, Any], payload: dict[str, Any], mode: str) -> str:
    lines = [str(sections.get("title") or report_type.title()), "", "## 기준 정보", ""]
    lines.extend(
        [
            f"- 저장소: `{payload['repository']}`",
            f"- 분석 프로필: `{payload.get('domain_profile_name', '')}`",
            f"- 브랜치: `{payload['branch']}`",
            f"- 원격: {payload['remote_url']}",
            f"- 집계 구간: `{payload['window_start']}` ~ `{payload['window_end']}`",
            f"- 작업 유형: `{work_type_label(payload['work_type'])}`",
            f"- 커밋 수: `{payload['commit_count']}`",
            f"- 변경 파일 수: `{payload['changed_file_count']}`",
            f"- 미커밋 변경 수: `{payload['uncommitted_count']}`",
            f"- 생성 방식: `{mode}`",
        ]
    )
    if mode == "fallback" and sections.get("_gemini_sections_error"):
        lines.append(f"- Gemini 문서 생성 실패: `{sections.get('_gemini_sections_error')}`")
    if sections.get("_ai_team_mode") == "fallback" and sections.get("_gemini_team_error"):
        lines.append(f"- Gemini 역할 분석 실패: `{sections.get('_gemini_team_error')}`")
    github_meta = payload.get("github") or {}
    if github_meta.get("enabled"):
        lines.append(f"- GitHub API 저장소: `{github_meta.get('repo', '')}`")
        lines.append(f"- GitHub API 커밋 수: `{github_meta.get('commit_count', 0)}`")
    lines.append("")
    auto_commit_items = build_auto_commit_status_items(payload.get("auto_commit_status") or {})
    if auto_commit_items:
        lines.extend(["## 자동 커밋/푸시 상태", ""])
        lines.extend(f"- {item}" for item in auto_commit_items)
        lines.append("")
    primary_facets = payload.get("primary_change_facets") or []
    supporting_facets = payload.get("supporting_change_facets") or []
    facets = payload.get("change_facets") or []
    if primary_facets or supporting_facets or facets:
        lines.extend(["## 변경 성격", ""])
        if primary_facets:
            lines.extend(["### 주요 변경 성격", ""])
            for item in primary_facets:
                lines.append(f"- `{item.get('name', '')}`: {item.get('reason', '')}")
            lines.append("")
        if supporting_facets:
            lines.extend(["### 보조 변경 성격", ""])
            for item in supporting_facets:
                lines.append(f"- `{item.get('name', '')}`: {item.get('reason', '')}")
            lines.append("")
        elif not primary_facets:
            for item in facets:
                lines.append(f"- `{item.get('name', '')}`: {item.get('reason', '')}")
            lines.append("")
    source_insights = list(payload.get("source_insights") or [])
    if source_insights:
        lines.extend(["## 소스 기반 핵심 변경", ""])
        lines.extend(f"- {item}" for item in source_insights)
        lines.append("")
    ai_team = sections.get("_ai_team") or {}
    ai_team_mode = str(sections.get("_ai_team_mode") or "fallback")
    if ai_team:
        lines.extend(["## Gemini 역할 분석", "", f"- 분석 방식: `{ai_team_mode}`", ""])
        role_map = [
            ("구조 분석", list(ai_team.get("structure") or [])),
            ("품질 분석", list(ai_team.get("quality") or [])),
            ("기능 영향", list(ai_team.get("feature") or [])),
            ("Jira 전략", list(ai_team.get("jira_strategy") or [])),
        ]
        for title, items in role_map:
            lines.append(f"### {title}")
            lines.append("")
            lines.extend(f"- {item}" for item in items) if items else lines.append("- 없음")
            lines.append("")

    def add_section(title: str, items: list[str]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- 없음")
        lines.append("")

    if report_type == "daily":
        add_section("핵심 요약", list(sections.get("summary") or []))
        add_section("완료/변경 내용", list(sections.get("completed") or []))
        add_section("오늘 집중할 항목", list(sections.get("focus") or []))
        add_section("리스크", list(sections.get("risks") or []))
        add_section("다음 액션", list(sections.get("next_actions") or []))
    elif report_type == "plan":
        add_section("계획 요약", list(sections.get("summary") or []))
        add_section("우선 작업", list(sections.get("priority_actions") or []))
        add_section("중기 작업", list(sections.get("mid_term_actions") or []))
        add_section("리스크", list(sections.get("risks") or []))
        add_section("메모", list(sections.get("notes") or []))
    elif report_type == "weekly":
        add_section("주간 요약", list(sections.get("summary") or []))
        add_section("주요 하이라이트", list(sections.get("highlights") or []))
        add_section("변경 영역", list(sections.get("areas") or []))
        _render_sprint_summary(lines, sections, "이번 주")
        add_section("리스크", list(sections.get("risks") or []))
        add_section("다음 주 초점", list(sections.get("next_week") or []))
    else:
        add_section("월간 요약", list(sections.get("summary") or []))
        add_section("주요 하이라이트", list(sections.get("highlights") or []))
        add_section("변경 영역", list(sections.get("areas") or []))
        _render_sprint_summary(lines, sections, "이번 달")
        add_section("리스크", list(sections.get("risks") or []))
        add_section("다음 달 초점", list(sections.get("next_month") or []))

    evidence = [f"- `{entry['hash']}` {entry['subject']} ({entry['author']}, {entry['time']})" for entry in payload["recent_commits"][:10]] or ["- 커밋 없음"]
    lines.extend(["## 근거 데이터", "", *evidence, ""])

    github_links = [f"- `{entry.get('sha', '')}` {entry.get('html_url', '')}" for entry in github_meta.get("commits", [])[:10] if entry.get("html_url")]
    if github_links:
        lines.extend(["## GitHub 링크", "", *github_links, ""])

    diff_summary = payload.get("diff_summary") or {}
    top_files = diff_summary.get("top_files") or []
    if diff_summary:
        lines.extend(
            [
                "## 변경 통계",
                "",
                f"- 추가 라인: `{diff_summary.get('total_added', 0)}`",
                f"- 삭제 라인: `{diff_summary.get('total_deleted', 0)}`",
                "",
            ]
        )
        if top_files:
            lines.append("## 파일별 영향")
            lines.append("")
            for item in top_files[:8]:
                lines.append(
                    f"- `{item.get('path', '')}`: +{item.get('added', 0)} / -{item.get('deleted', 0)}"
                )
            lines.append("")

    changed_docs = payload.get("changed_docs") or []
    if changed_docs:
        lines.extend(["## 문서 변경 흔적", ""])
        for path in changed_docs[:10]:
            lines.append(f"- `{path}`")
        lines.append("")

    areas = payload.get("top_areas") or []
    if areas:
        lines.extend(["## 설계 변화 다이어그램", "", "```mermaid", "flowchart LR"])
        first = areas[0]["area"]
        lines.append('    A["Repository"] --> B["Primary Change Area"]')
        lines.append(f'    B --> C["{first}"]')
        for index, item in enumerate(areas[1:4], start=1):
            lines.append(f'    C --> N{index}["{item["area"]}"]')
        lines.append('    C --> Z["Reports / Jira / Docs"]')
        lines.extend(["```", ""])

        lines.extend(["## 변경 영향 다이어그램", "", "```mermaid", "flowchart TD"])
        lines.append('    A["Changed Source Files"] --> B["Structure / Service Layer"]')
        lines.append('    A --> C["Validation / Tests"]')
        lines.append('    B --> D["User Flow / API / Document Output"]')
        lines.append('    C --> E["Quality Confidence"]')
        lines.append('    D --> F["Daily / Weekly / Monthly Report"]')
        lines.append('    E --> F["Daily / Weekly / Monthly Report"]')
        lines.extend(["```", ""])
    return "\n".join(lines)


def render_jira_markdown(doc_type: str, sections: dict[str, Any], payload: dict[str, Any], mode: str) -> str:
    lines = [f"# {sections.get('title', doc_type)}", "", "## Meta", ""]
    lines.extend(
        [
            f"- Work Type: `{work_type_label(payload['work_type'])}`",
            f"- Domain Profile: `{payload.get('domain_profile_name', '')}`",
            f"- Repo: `{payload['repository']}`",
            f"- Branch: `{payload['branch']}`",
            f"- Window: `{payload['window_start']}` ~ `{payload['window_end']}`",
            f"- Generation: `{mode}`",
        ]
    )
    if mode == "fallback" and sections.get("_gemini_sections_error"):
        lines.append(f"- Gemini doc failure: `{sections.get('_gemini_sections_error')}`")
    if sections.get("_ai_team_mode") == "fallback" and sections.get("_gemini_team_error"):
        lines.append(f"- Gemini team failure: `{sections.get('_gemini_team_error')}`")
    lines.extend(["", "## Summary", "", str(sections.get("summary") or "-"), ""])
    primary_facets = payload.get("primary_change_facets") or []
    supporting_facets = payload.get("supporting_change_facets") or []
    facets = payload.get("change_facets") or []
    if primary_facets or supporting_facets or facets:
        lines.extend(["## Change Facets", ""])
        if primary_facets:
            lines.extend(["### Primary Change Facets", ""])
            for item in primary_facets:
                lines.append(f"- {item.get('name', '')}: {item.get('reason', '')}")
            lines.append("")
        if supporting_facets:
            lines.extend(["### Supporting Change Facets", ""])
            for item in supporting_facets:
                lines.append(f"- {item.get('name', '')}: {item.get('reason', '')}")
            lines.append("")
        elif not primary_facets:
            for item in facets:
                lines.append(f"- {item.get('name', '')}: {item.get('reason', '')}")
            lines.append("")
    ai_team = sections.get("_ai_team") or {}
    ai_team_mode = str(sections.get("_ai_team_mode") or "fallback")
    if ai_team:
        lines.extend(["## Gemini Team Analysis", "", f"- Mode: `{ai_team_mode}`", ""])
        for title, key in [("Structure", "structure"), ("Quality", "quality"), ("Feature Impact", "feature"), ("Jira Strategy", "jira_strategy")]:
            lines.append(f"### {title}")
            lines.append("")
            items = list(ai_team.get(key) or [])
            lines.extend(f"- {item}" for item in items) if items else lines.append("- None")
            lines.append("")

    def add(title: str, items: list[str]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- None")
        lines.append("")

    status_summary = sections.get("status_summary") or {}
    add("상위 작업", [f"이름: {sections.get('task_name', '-')}", f"목표: {sections.get('task_goal', '-')}", *list(sections.get('scope') or [])])
    add(
        "스프린트 현황",
        [
            f"완료: {status_summary.get('completed_count', len(list(sections.get('completed') or [])))}",
            f"진행 중: {status_summary.get('in_progress_count', len(list(sections.get('in_progress') or [])))}",
            f"잔여: {status_summary.get('remaining_count', len(list(sections.get('remaining') or [])))}",
        ],
    )
    # Task board: 작업별 계획 + 완료 내역
    task_board = list(sections.get("task_board") or [])
    if task_board:
        lines.extend(["## 작업별 상세 현황", ""])
        for tb in task_board:
            status_icon = {"완료": "✅", "진행 중": "🔄", "예정": "⏳"}.get(tb["status"], "⏳")
            progress = tb.get("subtask_progress", "")
            progress_str = f" ({progress})" if progress else ""
            lines.append(f"### {status_icon} [{tb['key']}] {tb['title']}{progress_str}")
            lines.append(f"- 기간: `{tb['period']}`")
            lines.append(f"- 상태: **{tb['status']}**")
            if tb.get("subtasks"):
                lines.append("- 하위작업:")
                sub_icons = {"done": "✅", "in_progress": "🔄", "pending": "⏳"}
                for st in tb["subtasks"]:
                    if isinstance(st, dict):
                        si = sub_icons.get(st.get("status", "pending"), "⏳")
                        lines.append(f"  - {si} {st['title']}: {st.get('description', '')}")
                    else:
                        lines.append(f"  {st}")
            if tb.get("related_commits"):
                lines.append("- 관련 커밋:")
                for rc in tb["related_commits"][:3]:
                    lines.append(f"  - {rc}")
            lines.append("")

    add("완료된 작업", list(sections.get("completed") or []))
    add("진행 중인 작업", list(sections.get("in_progress") or []))
    add("남은 작업", list(sections.get("remaining") or []))
    add("완료 조건", list(sections.get("validation") or []))
    add("리스크 및 확인 필요 사항", list(sections.get("risks") or []))
    add("참고 링크", list(sections.get("links") or []))

    diff_summary = payload.get("diff_summary") or {}
    if diff_summary:
        lines.extend(
            [
                "## Change Metrics",
                "",
                f"- Added lines: `{diff_summary.get('total_added', 0)}`",
                f"- Deleted lines: `{diff_summary.get('total_deleted', 0)}`",
                "",
            ]
        )

    areas = payload.get("top_areas") or []
    if areas:
        lines.extend(["## Architecture Delta", "", "```mermaid", "flowchart LR"])
        lines.append('    A["Repository"] --> B["Primary Change Area"]')
        lines.append(f'    B --> C["{areas[0]["area"]}"]')
        if len(areas) > 1:
            lines.append(f'    C --> D["{areas[1]["area"]}"]')
        lines.append('    C --> E["Jira Status / Remaining Work"]')
        lines.extend(["```", ""])

        lines.extend(["## Change Impact", "", "```mermaid", "flowchart TD"])
        lines.append('    A["Changed Source Files"] --> B["Design / Structure Review"]')
        lines.append('    A --> C["Validation / Risk Review"]')
        lines.append('    B --> D["Jira Status"]')
        lines.append('    C --> D["Jira Status"]')
        lines.extend(["```", ""])
    return "\n".join(lines)


def generate_document(report_type: str, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    try:
        sections = ask_gemini_for_sections(report_type, payload)
        mode = "gemini"
        sections_error = ""
    except Exception as exc:
        sections = build_fallback_jira_doc(report_type, payload) if report_type.startswith("jira") else build_fallback_sections(report_type, payload)
        mode = "fallback"
        sections_error = f"{type(exc).__name__}: {exc}"

    try:
        ai_team = ask_gemini_for_team_analysis(payload)
        ai_team_mode = "gemini"
        ai_team_error = ""
    except Exception as exc:
        ai_team = build_fallback_ai_team_analysis(payload)
        ai_team_mode = "fallback"
        ai_team_error = f"{type(exc).__name__}: {exc}"

    sections["_ai_team"] = ai_team
    sections["_ai_team_mode"] = ai_team_mode
    sections["_gemini_sections_error"] = sections_error
    sections["_gemini_team_error"] = ai_team_error

    if report_type.startswith("jira"):
        return render_jira_markdown(report_type, sections, payload, mode), mode, sections
    title = str(sections.get("title") or "")
    if title and not title.startswith("# "):
        sections["title"] = f"# {title}"
    return render_report_markdown(report_type, sections, payload, mode), mode, sections


def render_detail_html(report_type: str, sections: dict[str, Any], payload: dict[str, Any], mode: str, markdown_path: Path) -> str:
    diff = payload.get("diff_summary") or {}
    commits = payload.get("recent_commits") or []
    areas = payload.get("top_areas") or []
    facets = payload.get("change_facets") or []
    primary_facets = payload.get("primary_change_facets") or []
    supporting_facets = payload.get("supporting_change_facets") or []
    changed_docs = payload.get("changed_docs") or []
    ai_team = sections.get("_ai_team") or {}
    ai_team_mode = str(sections.get("_ai_team_mode") or "fallback")
    title = str(sections.get("title") or report_type.title()).lstrip("# ").strip()
    repo_root = Path(str(payload.get("repo_root") or ".")).resolve()

    def list_block(title_text: str, items: list[str]) -> str:
        checkbox_mode = title_text in {"남은 작업", "Remaining"}
        item_class = ' class="checklist-item"' if checkbox_mode else ""
        body_items = []
        for item in items:
            item_text = str(item)
            if checkbox_mode:
                item_id = sha1(f"{markdown_path}:{title_text}:{item_text}".encode("utf-8")).hexdigest()[:16]
                body_items.append(
                    f'<li{item_class}><label class="check-label"><input class="check-input" type="checkbox" data-checklist-id="{item_id}"><span class="check-box" aria-hidden="true"></span><span class="check-text">{escape(item_text)}</span></label></li>'
                )
            else:
                body_items.append(f"<li>{escape(item_text)}</li>")
        body = "".join(body_items) or "<li>No items</li>"
        return f"""
<section class="detail-panel">
  <h3>{escape(title_text)}</h3>
  <ul>{body}</ul>
</section>
"""

    def file_link(path_str: str) -> str:
        raw = str(path_str or "").strip()
        if not raw:
            return ""
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        if not candidate.exists():
            return ""
        return f'<a href="{escape(candidate.as_uri())}"><code>{escape(str(candidate))}</code></a>'

    def area_checkpoints(area_name: str) -> list[str]:
        area = area_name.lower()
        if area.startswith("backend"):
            return ["API 응답 구조 확인", "예외 처리와 상태 코드 확인", "주요 라우터 회귀 점검"]
        if area.startswith("frontend") or area.startswith("stremlit"):
            return ["주요 화면 렌더링 확인", "사용자 흐름 점검", "스타일 깨짐 여부 확인"]
        if area.startswith("tests"):
            return ["실패 테스트 여부 확인", "신규 검증 범위 확인", "회귀 테스트 누락 점검"]
        if area.startswith("docs") or area.startswith("project_docs"):
            return ["문서와 구현 일치 여부 확인", "링크와 예시 최신화", "Jira/보고 문구 반영 확인"]
        if area.startswith("installer") or area.startswith("deploy") or area.startswith(".github"):
            return ["배포 산출물 제외 여부 확인", "파이프라인 설정 검토", "릴리스 절차 영향도 확인"]
        return ["핵심 변경 파일 리뷰", "입출력 영향 범위 확인", "후속 검증 필요 여부 점검"]

    def area_risks(area_name: str) -> list[str]:
        area = area_name.lower()
        if area.startswith("backend"):
            return ["API 계약 변경 누락 가능성", "숨은 예외 경로 미검증 가능성"]
        if area.startswith("frontend") or area.startswith("stremlit"):
            return ["UI 회귀 가능성", "브라우저/해상도별 편차 가능성"]
        if area.startswith("tests"):
            return ["실구현 대비 테스트 갭 가능성", "테스트 데이터 의존성 가능성"]
        if area.startswith("docs") or area.startswith("project_docs"):
            return ["설명과 실제 동작 불일치 가능성", "문서 반영 누락 가능성"]
        if area.startswith("installer") or area.startswith("deploy") or area.startswith(".github"):
            return ["배포 경로 오염 가능성", "불필요 산출물 커밋 가능성"]
        return ["영향 범위 과소평가 가능성", "후속 작업 누락 가능성"]

    def area_owner(area_name: str) -> str:
        area = area_name.lower()
        if area.startswith("backend"):
            return "Backend"
        if area.startswith("frontend") or area.startswith("stremlit"):
            return "Frontend"
        if area.startswith("tests"):
            return "QA"
        if area.startswith("docs") or area.startswith("project_docs"):
            return "Docs"
        if area.startswith("installer") or area.startswith("deploy") or area.startswith(".github"):
            return "DevOps"
        return "Owner"

    def score_priority(file_count: int, added: int, deleted: int) -> tuple[str, str, str]:
        volume = file_count + added + deleted
        if volume >= 2500:
            return "High", "High", "Review Now"
        if volume >= 700:
            return "Medium", "Medium", "Review Soon"
        return "Low", "Low", "Monitor"

    def build_area_inspection() -> str:
        area_map: dict[str, dict[str, Any]] = {}
        changed_files = [str(path) for path in payload.get("changed_files") or []]
        diff_top = [dict(item) for item in (diff.get("top_files") or [])]
        for path in changed_files:
            area = path.replace("\\", "/").split("/", 1)[0]
            bucket = area_map.setdefault(area, {"file_count": 0, "added": 0, "deleted": 0, "files": []})
            bucket["file_count"] += 1
        for item in diff_top:
            path = str(item.get("path", ""))
            area = path.replace("\\", "/").split("/", 1)[0]
            bucket = area_map.setdefault(area, {"file_count": 0, "added": 0, "deleted": 0, "files": []})
            bucket["added"] += int(item.get("added", 0))
            bucket["deleted"] += int(item.get("deleted", 0))
            bucket["files"].append(path)
        ranked = sorted(
            area_map.items(),
            key=lambda pair: (int(pair[1].get("file_count", 0)) + int(pair[1].get("added", 0)) + int(pair[1].get("deleted", 0))),
            reverse=True,
        )[:4]
        if not ranked:
            return ""
        cards = []
        for area_name, stats in ranked:
            file_count = int(stats.get("file_count", 0))
            added = int(stats.get("added", 0))
            deleted = int(stats.get("deleted", 0))
            priority, impact, status = score_priority(file_count, added, deleted)
            owner = area_owner(area_name)
            risk_level = "High" if priority == "High" else ("Medium" if priority == "Medium" else "Low")
            files = list(dict.fromkeys(stats.get("files") or []))[:3]
            files_html = "".join(f"<li><code>{escape(path)}</code></li>" for path in files) or "<li>No key files</li>"
            checks_html = "".join(f"<li>{escape(item)}</li>" for item in area_checkpoints(area_name))
            risks_html = "".join(f"<li>{escape(item)}</li>" for item in area_risks(area_name))
            cards.append(
                f"""
<section class="area-card">
  <div class="area-head">
    <h3>{escape(area_name)}</h3>
    <span>{file_count} files · +{added} / -{deleted}</span>
  </div>
  <div class="area-badges">
    <span class="mini-badge priority-{priority.lower()}">Priority {priority}</span>
    <span class="mini-badge impact-{impact.lower()}">Impact {impact}</span>
    <span class="mini-badge risk-{risk_level.lower()}">Risk {risk_level}</span>
    <span class="mini-badge owner">{owner}</span>
    <span class="mini-badge status">{status}</span>
  </div>
  <div class="area-grid">
    <div>
      <h4>Key Files</h4>
      <ul>{files_html}</ul>
    </div>
    <div>
      <h4>Inspection Points</h4>
      <ul>{checks_html}</ul>
    </div>
    <div>
      <h4>Risk Points</h4>
      <ul>{risks_html}</ul>
    </div>
  </div>
</section>
"""
            )
        return f"""
<section class="detail-panel area-section">
  <h3>Area Inspection</h3>
  <div class="area-stack">
    {"".join(cards)}
  </div>
</section>
"""

    def build_image_slots() -> str:
        top_files = [str(item.get("path", "")) for item in (diff.get("top_files") or [])[:3]]
        changed_docs_local = [str(path) for path in (payload.get("changed_docs") or [])[:3]]
        related_outputs = [
            ("상세 Markdown", markdown_path),
            ("상세 HTML", markdown_path.with_suffix(".html")),
            ("스타트업 대시보드", markdown_path.parents[1] / "dashboard" / markdown_path.name.replace("-jira-status.md", "-startup-dashboard.html")),
        ]

        def render_link_group(items: list[tuple[str, Path | str]]) -> str:
            rows = []
            for label, value in items:
                target = value if isinstance(value, Path) else Path(str(value))
                link_html = file_link(str(target))
                if link_html:
                    rows.append(f"<li>{escape(label)}: {link_html}</li>")
            return "".join(rows) or "<li>자동 링크 가능한 파일 없음</li>"

        evidence_specs = [
            {
                "title": "증빙 1. 변경 전후 화면",
                "purpose": "UI, 대시보드, 리포트 레이아웃 변경이 있으면 Before / After 비교 캡처를 붙입니다.",
                "targets": ["시작 대시보드", "리포트 HTML", "주요 화면 변경점"],
                "sources": changed_docs_local[:1] or top_files[:1] or ["관련 화면 또는 HTML 출력물"],
                "links": related_outputs[:2],
            },
            {
                "title": "증빙 2. 실행 결과 확인",
                "purpose": "스크립트 실행 성공, 자동화 성공, 핵심 산출물 생성 여부를 캡처합니다.",
                "targets": ["실행 로그", "생성된 Markdown/HTML", "자동 커밋 상태"],
                "sources": top_files[:2] or ["reports/dashboard", "reports/jira", "reports/automation_status"],
                "links": related_outputs,
            },
            {
                "title": "증빙 3. 구조 또는 흐름 증빙",
                "purpose": "아키텍처 변화나 작업 흐름이 핵심이면 흐름도, Mermaid, 파일 트리 캡처를 붙입니다.",
                "targets": ["Architecture Delta", "Change Impact Map", "Area Inspection"],
                "sources": top_files[1:3] or ["핵심 영향 파일", "주요 변경 영역"],
                "links": [(f"근거 파일 {idx + 1}", item) for idx, item in enumerate(top_files[:2])],
            },
        ]
        slots = []
        for spec in evidence_specs:
            target_html = "".join(f"<li>{escape(item)}</li>" for item in spec["targets"])
            source_html = "".join(f"<li><code>{escape(item)}</code></li>" for item in spec["sources"])
            link_html = render_link_group(list(spec.get("links") or []))
            slots.append(
                f"""
    <div class="image-slot">
      <span>{escape(spec['title'])}</span>
      <small>{escape(spec['purpose'])}</small>
      <div class="evidence-meta">
        <strong>추천 캡처 대상</strong>
        <ul>{target_html}</ul>
      </div>
      <div class="evidence-meta">
        <strong>근거 파일/영역</strong>
        <ul>{source_html}</ul>
      </div>
      <div class="evidence-meta">
        <strong>바로 열기 링크</strong>
        <ul>{link_html}</ul>
      </div>
      <div class="evidence-placeholder">여기에 스크린샷, 차트, 비교 이미지를 배치</div>
    </div>
"""
            )
        return """
<section class="detail-panel image-panel">
  <h3>시각 증빙 패널</h3>
  <p class="mini-meta">결과 보고서에 바로 붙일 수 있는 증빙 위치입니다. 아래 추천 대상을 기준으로 화면, 로그, 구조 변경 캡처를 채웁니다.</p>
  <div class="image-slots">
""" + "".join(slots) + """
  </div>
</section>
"""

    def build_jira_plan_extras() -> str:
        remaining = list(sections.get("remaining") or [])
        in_progress = list(sections.get("in_progress") or [])
        completed = list(sections.get("completed") or [])
        validations = list(sections.get("validation") or [])
        rows = []
        timeline = []
        owners = ["Backend", "Frontend", "QA", "Docs", "DevOps", "Owner"]
        work_items = [(item, "진행 중") for item in in_progress[:3]] + [(item, "잔여") for item in remaining[:3]] + [(item, "완료") for item in completed[:2]]
        for idx, (item, phase) in enumerate(work_items[:6]):
            priority = "High" if phase == "잔여" and idx == 0 else ("Medium" if phase in {"잔여", "진행 중"} else "Low")
            owner = owners[idx % len(owners)]
            dod = validations[idx % len(validations)] if validations else "Validation needed"
            timeline.append(
                f"""
<div class="timeline-step">
  <div class="timeline-marker">{idx + 1}</div>
  <div class="timeline-copy">
    <strong>{escape(str(item))}</strong>
    <span>{escape(phase)} · {priority} priority · {escape(str(dod))}</span>
  </div>
</div>
"""
            )
            rows.append(
                f"""
<tr>
  <td>{idx + 1}</td>
  <td>{escape(str(item))}</td>
  <td>{escape(phase)}</td>
  <td>{priority}</td>
  <td>{owner}</td>
  <td>{escape(str(dod))}</td>
</tr>
"""
            )
        if not rows:
            return ""
        return f"""
<section class="detail-panel">
  <h3>실행 흐름 타임라인</h3>
  <div class="timeline">{"".join(timeline)}</div>
</section>
<section class="detail-panel">
  <h3>작업 할당 및 상태 표</h3>
  <div class="table-wrap">
    <table>
      <thead>
        <tr><th>#</th><th>Work Item</th><th>Status</th><th>Priority</th><th>Owner</th><th>Definition of Done</th></tr>
      </thead>
      <tbody>
        {"".join(rows)}
      </tbody>
    </table>
  </div>
</section>
"""

    section_blocks: list[str] = []
    if ai_team:
        section_blocks.extend(
            [
                list_block(f"Gemini 구조 분석 ({ai_team_mode})", list(ai_team.get("structure") or [])),
                list_block(f"Gemini 품질 분석 ({ai_team_mode})", list(ai_team.get("quality") or [])),
                list_block(f"Gemini 기능 영향 ({ai_team_mode})", list(ai_team.get("feature") or [])),
                list_block(f"Gemini Jira 전략 ({ai_team_mode})", list(ai_team.get("jira_strategy") or [])),
            ]
        )
    if report_type == "daily":
        section_blocks.extend(
            [
                list_block("요약", list(sections.get("summary") or [])),
                list_block("완료 및 변경", list(sections.get("completed") or [])),
                list_block("오늘 집중", list(sections.get("focus") or [])),
                list_block("리스크", list(sections.get("risks") or [])),
                list_block("다음 액션", list(sections.get("next_actions") or [])),
            ]
        )
    elif report_type == "plan":
        section_blocks.extend(
            [
                list_block("계획 요약", list(sections.get("summary") or [])),
                list_block("우선 작업", list(sections.get("priority_actions") or [])),
                list_block("중기 작업", list(sections.get("mid_term_actions") or [])),
                list_block("리스크", list(sections.get("risks") or [])),
                list_block("메모", list(sections.get("notes") or [])),
            ]
        )
    elif report_type == "weekly":
        section_blocks.extend(
            [
                list_block("주간 요약", list(sections.get("summary") or [])),
                list_block("하이라이트", list(sections.get("highlights") or [])),
                list_block("변경 영역", list(sections.get("areas") or [])),
                list_block("리스크", list(sections.get("risks") or [])),
                list_block("다음 주", list(sections.get("next_week") or [])),
            ]
        )
    elif report_type == "monthly":
        section_blocks.extend(
            [
                list_block("월간 요약", list(sections.get("summary") or [])),
                list_block("하이라이트", list(sections.get("highlights") or [])),
                list_block("변경 영역", list(sections.get("areas") or [])),
                list_block("리스크", list(sections.get("risks") or [])),
                list_block("다음 달", list(sections.get("next_month") or [])),
            ]
        )
    elif report_type == "jira":
        section_blocks.extend(
            [
                list_block("상위 작업", [f"이름: {sections.get('task_name', '-')}", f"목표: {sections.get('task_goal', '-')}", *list(sections.get("scope") or [])]),
                list_block(
                    "스프린트 현황",
                    [
                        f"완료: {(sections.get('status_summary') or {}).get('completed_count', len(list(sections.get('completed') or [])))}",
                        f"진행 중: {(sections.get('status_summary') or {}).get('in_progress_count', len(list(sections.get('in_progress') or [])))}",
                        f"잔여: {(sections.get('status_summary') or {}).get('remaining_count', len(list(sections.get('remaining') or [])))}",
                    ],
                ),
                list_block("완료된 작업", list(sections.get("completed") or [])),
                list_block("진행 중인 작업", list(sections.get("in_progress") or [])),
                list_block("남은 작업", list(sections.get("remaining") or [])),
                list_block("완료 조건", list(sections.get("validation") or [])),
                list_block("리스크 및 확인 필요 사항", list(sections.get("risks") or [])),
            ]
        )

    source_insights = list(payload.get("source_insights") or [])
    if source_insights:
        section_blocks.insert(0, list_block("소스 기반 핵심 변경", source_insights))
    auto_commit_items = build_auto_commit_status_items(payload.get("auto_commit_status") or {})
    if auto_commit_items:
        section_blocks.insert(1 if source_insights else 0, list_block("자동 커밋/푸시 상태", auto_commit_items))

    primary_facet_html = "".join(
        f'<span class="facet facet-primary"><strong>{escape(str(item.get("name", "")))}</strong><em>{escape(str(item.get("reason", "")))}</em></span>'
        for item in primary_facets
    )
    supporting_facet_html = "".join(
        f'<span class="facet facet-support"><strong>{escape(str(item.get("name", "")))}</strong><em>{escape(str(item.get("reason", "")))}</em></span>'
        for item in supporting_facets
    )
    if not primary_facet_html and not supporting_facet_html:
        supporting_facet_html = "".join(
            f'<span class="facet facet-support"><strong>{escape(str(item.get("name", "")))}</strong><em>{escape(str(item.get("reason", "")))}</em></span>'
            for item in facets
        ) or '<span class="facet facet-support"><strong>유지보수</strong><em>추가 분류 근거가 부족해 기본 태그를 사용했습니다.</em></span>'
    commit_html = "".join(
        f"<li><code>{escape(str(item.get('hash', '')))}</code> {escape(str(item.get('subject', '')))} <span>{escape(str(item.get('author', '')))}</span></li>"
        for item in commits[:10]
    ) or "<li>No commits</li>"
    docs_html = "".join(f"<li><code>{escape(str(path))}</code></li>" for path in changed_docs[:10]) or "<li>No changed docs</li>"
    top_file_html = "".join(
        f"<li><code>{escape(str(item.get('path', '')))}</code><span>+{int(item.get('added', 0))} / -{int(item.get('deleted', 0))}</span></li>"
        for item in (diff.get("top_files") or [])[:8]
    ) or "<li>No impacted files</li>"
    extra_html = build_image_slots()
    extra_html += build_area_inspection()
    if report_type == "jira":
        extra_html += build_jira_plan_extras()

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
{DESIGN_CSS}
    /* --- Detail-report overrides --- */
    .grid {{ grid-template-columns:1.2fr .8fr; }}
    .detail-panel li.checklist-item {{ list-style:none; margin-left:-20px; }}
    .detail-panel li span {{ color:var(--muted); margin-left:8px; font-size:12px; }}
  </style>
</head>
<body>
  <a href="#main-content" class="skip-link">Skip to content</a>
  <div class="wrap" id="main-content">
    <section class="hero">
      <h1>{escape(title)}</h1>
      <p>{escape(str(sections.get("summary") if isinstance(sections.get("summary"), str) else "Git, GitHub, 변경 통계, Jira 구조, 시각화를 포함한 상세 HTML 리포트입니다."))}</p>
      <div class="meta">
        <div><span>Repository</span><strong>{escape(str(payload.get("repository", "")))}</strong></div>
        <div><span>Profile</span><strong>{escape(str(payload.get("domain_profile_name", "")))}</strong></div>
        <div><span>Work Type</span><strong>{escape(work_type_label(str(payload.get("work_type", ""))))}</strong></div>
        <div><span>Commits</span><strong>{int(payload.get("commit_count", 0))}</strong></div>
        <div><span>Files</span><strong>{int(payload.get("changed_file_count", 0))}</strong></div>
        <div><span>Mode</span><strong>{escape(mode)}</strong></div>
      </div>
    </section>
    <div class="actions">
      <a href="{escape(markdown_path.as_uri())}">Source Markdown</a>
    </div>
    <div class="facet-groups">
      <section class="facet-group">
        <h3>Primary Change Facets</h3>
        <div class="facet-strip">{primary_facet_html or '<span class="facet facet-primary"><strong>핵심 변경</strong><em>주요 변경 성격이 아직 분리되지 않았습니다.</em></span>'}</div>
      </section>
      <section class="facet-group">
        <h3>Supporting Change Facets</h3>
        <div class="facet-strip">{supporting_facet_html or '<span class="facet facet-support"><strong>보조 변경 없음</strong><em>이번 리포트는 핵심 변경 중심으로 정리되었습니다.</em></span>'}</div>
      </section>
    </div>
    <div class="chart-grid">
      <section class="chart-wrap chart-large">
        <h3>Top Change Areas</h3>
        {svg_area_bars(areas[:5])}
      </section>
      <section class="chart-wrap chart-large">
        <h3>Architecture Delta</h3>
        {svg_architecture_delta(areas[:4], diff.get("top_files") or [])}
      </section>
      <section class="chart-wrap chart-large">
        <h3>Code Structure Map</h3>
        {svg_structure_map(diff.get("top_files") or [])}
      </section>
      <section class="chart-wrap chart-large">
        <h3>Change Impact Map</h3>
        {svg_change_impact_map(areas[:4], diff.get("top_files") or [], commits[:4])}
      </section>
    </div>
    <div class="content-stack">
      {"".join(section_blocks)}
    </div>
    <div class="grid">
      <section class="detail-panel">
        <h3>Recent Commits</h3>
        <ul>{commit_html}</ul>
      </section>
      <section class="detail-panel">
        <h3>Change Metrics</h3>
        <ul>
          <li>Added Lines <span>{int(diff.get("total_added", 0))}</span></li>
          <li>Deleted Lines <span>{int(diff.get("total_deleted", 0))}</span></li>
        </ul>
      </section>
    </div>
    <div class="grid">
      <section class="detail-panel">
        <h3>Top Impacted Files</h3>
        <ul>{top_file_html}</ul>
      </section>
      <section class="detail-panel">
        <h3>Documentation Footprint</h3>
        <ul>{docs_html}</ul>
      </section>
    </div>
    {extra_html}
  </div>
  <script>
    (function () {{
      const storagePrefix = "jira-checklist:";
      document.querySelectorAll(".check-input[data-checklist-id]").forEach((input) => {{
        const key = storagePrefix + input.dataset.checklistId;
        try {{
          input.checked = window.localStorage.getItem(key) === "1";
        }} catch (error) {{}}
        input.addEventListener("change", () => {{
          try {{
            window.localStorage.setItem(key, input.checked ? "1" : "0");
          }} catch (error) {{}}
        }});
      }});
    }})();
  </script>
</body>
</html>"""


def svg_area_bars(areas: list[dict[str, Any]]) -> str:
    if not areas:
        return "<p>No area data</p>"
    width = 960
    bar_height = 34
    gap = 18
    max_count = max(int(item.get("count", 0)) for item in areas) or 1
    height = len(areas) * (bar_height + gap) + 36
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Area chart"><title>Area distribution chart</title>']
    y = 12
    for idx, item in enumerate(areas[:6]):
        label = escape(str(item.get("area", "")))
        count = int(item.get("count", 0))
        bar_width = int((count / max_count) * 620)
        color = SVG_PALETTE[idx % len(SVG_PALETTE)]
        parts.append(f'<text x="0" y="{y + 22}" font-size="16" fill="{SVG_TEXT_DARK}">{label}</text>')
        parts.append(f'<rect x="240" y="{y}" rx="10" ry="10" width="{bar_width}" height="{bar_height}" fill="{color}"></rect>')
        parts.append(f'<text x="{250 + bar_width}" y="{y + 22}" font-size="14" fill="{SVG_TEXT_DARKER}">{count}</text>')
        y += bar_height + gap
    parts.append("</svg>")
    return "".join(parts)


def svg_flow(areas: list[dict[str, Any]]) -> str:
    primary = escape(str(areas[0]["area"])) if areas else "Core Area"
    secondary = escape(str(areas[1]["area"])) if len(areas) > 1 else "Support Area"
    p = SVG_PALETTE
    return f"""
<svg viewBox="0 0 1100 240" class="flow" role="img" aria-label="Change flow">
  <title>Change flow diagram</title>
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto">
      <path d="M0,0 L0,6 L9,3 z" fill="{SVG_STROKE}"></path>
    </marker>
  </defs>
  <rect x="20" y="90" width="210" height="60" rx="16" fill="{p[0]}"></rect>
  <text x="125" y="126" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="18">Git / GitHub Activity</text>
  <rect x="320" y="24" width="220" height="60" rx="16" fill="{p[1]}"></rect>
  <text x="430" y="60" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="18">{primary}</text>
  <rect x="320" y="146" width="220" height="60" rx="16" fill="{p[2]}"></rect>
  <text x="430" y="182" text-anchor="middle" fill="{SVG_TEXT_DARK}" font-size="18">{secondary}</text>
  <rect x="650" y="90" width="190" height="60" rx="16" fill="{p[3]}"></rect>
  <text x="745" y="126" text-anchor="middle" fill="{SVG_TEXT_DARK}" font-size="18">Analysis / AI</text>
  <rect x="910" y="90" width="170" height="60" rx="16" fill="{p[4]}"></rect>
  <text x="995" y="126" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="18">Reports</text>
  <line x1="230" y1="120" x2="320" y2="54" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arrow)"></line>
  <line x1="230" y1="120" x2="320" y2="176" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arrow)"></line>
  <line x1="540" y1="54" x2="650" y2="120" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arrow)"></line>
  <line x1="540" y1="176" x2="650" y2="120" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arrow)"></line>
  <line x1="840" y1="120" x2="910" y2="120" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arrow)"></line>
</svg>
"""



def svg_structure_map(top_files: list[dict[str, Any]]) -> str:
    if not top_files:
        return "<p>No structure data</p>"
    roots = []
    for item in top_files[:6]:
        path = str(item.get("path", "")).replace("\\", "/")
        roots.append(path.split("/"))
    width = 1040
    height = 90 + len(roots) * 36
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Structure map"><title>Repository structure map</title>']
    parts.append(f'<rect x="20" y="20" width="180" height="48" rx="14" fill="{SVG_BG_DARK}"></rect>')
    parts.append(f'<text x="110" y="50" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="18">Repository</text>')
    y = 56
    for idx, parts_list in enumerate(roots, start=1):
        area = escape(parts_list[0] if parts_list else "root")
        leaf = escape("/".join(parts_list[1:]) if len(parts_list) > 1 else area)
        box_y = y + (idx - 1) * 36
        parts.append(f'<line x1="200" y1="44" x2="320" y2="{box_y}" stroke="{SVG_STROKE}" stroke-width="2.5"></line>')
        parts.append(f'<rect x="320" y="{box_y-18}" width="190" height="30" rx="10" fill="{SVG_PALETTE[1]}"></rect>')
        parts.append(f'<text x="415" y="{box_y+2}" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="14">{area}</text>')
        parts.append(f'<line x1="510" y1="{box_y-3}" x2="600" y2="{box_y-3}" stroke="{SVG_STROKE}" stroke-width="2.5"></line>')
        parts.append(f'<rect x="600" y="{box_y-18}" width="380" height="30" rx="10" fill="{SVG_PALETTE[3]}"></rect>')
        parts.append(f'<text x="790" y="{box_y+2}" text-anchor="middle" fill="{SVG_TEXT_DARK}" font-size="13">{leaf}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def svg_action_roadmap(areas: list[dict[str, Any]], commits: list[dict[str, Any]]) -> str:
    steps = []
    for idx, item in enumerate(areas[:4], start=1):
        steps.append((f'{item.get("area", "area")} 점검', f'{int(item.get("count", 0))} files'))
    if not steps:
        for idx, item in enumerate(commits[:4], start=1):
            steps.append((f'Commit {idx}', str(item.get('subject', ''))))
    width = 1040
    height = 190
    parts = [f'<svg viewBox="0 0 {width} {height}" class="flow" role="img" aria-label="Action roadmap"><title>Action roadmap</title>']
    x = 30
    for idx, (title, subtitle) in enumerate(steps):
        color = SVG_PALETTE[idx % len(SVG_PALETTE)]
        text_c = svg_text_color_for(color)
        parts.append(f'<rect x="{x}" y="56" width="210" height="74" rx="18" fill="{color}"></rect>')
        parts.append(f'<text x="{x+105}" y="87" text-anchor="middle" fill="{text_c}" font-size="18">{escape(title)}</text>')
        parts.append(f'<text x="{x+105}" y="110" text-anchor="middle" fill="{text_c}" font-size="12">{escape(subtitle)}</text>')
        if idx < len(steps) - 1:
            parts.append(f'<line x1="{x+210}" y1="93" x2="{x+250}" y2="93" stroke="{SVG_STROKE}" stroke-width="4"></line>')
            parts.append(f'<polygon points="{x+250},93 {x+238},86 {x+238},100" fill="{SVG_STROKE}"></polygon>')
        x += 250
    parts.append('</svg>')
    return ''.join(parts)


def svg_architecture_delta(areas: list[dict[str, Any]], top_files: list[dict[str, Any]]) -> str:
    primary = escape(str(areas[0]["area"])) if areas else "Primary Area"
    secondary = escape(str(areas[1]["area"])) if len(areas) > 1 else "Secondary Area"
    tertiary = escape(str(areas[2]["area"])) if len(areas) > 2 else "Output Area"
    lead_file = escape(str((top_files[0] or {}).get("path", "core/module.py"))) if top_files else "core/module.py"
    support_file = escape(str((top_files[1] or {}).get("path", "support/module.py"))) if len(top_files) > 1 else "support/module.py"
    p, t = SVG_PALETTE, SVG_SUBTITLE_TINTS
    return f"""
<svg viewBox="0 0 1100 320" class="flow" role="img" aria-label="Architecture delta">
  <title>Architecture delta</title>
  <defs>
    <marker id="arch-arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto">
      <path d="M0,0 L0,6 L9,3 z" fill="{SVG_STROKE}"></path>
    </marker>
  </defs>
  <rect x="30" y="120" width="180" height="72" rx="18" fill="{SVG_BG_DARK}"></rect>
  <text x="120" y="150" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="18">Repository</text>
  <text x="120" y="174" text-anchor="middle" fill="{t[1]}" font-size="13">Change Set</text>
  <rect x="290" y="28" width="250" height="80" rx="18" fill="{p[1]}"></rect>
  <text x="415" y="60" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="20">{primary}</text>
  <text x="415" y="84" text-anchor="middle" fill="{t[1]}" font-size="12">{lead_file}</text>
  <rect x="290" y="120" width="250" height="80" rx="18" fill="{p[2]}"></rect>
  <text x="415" y="152" text-anchor="middle" fill="{SVG_TEXT_DARK}" font-size="20">{secondary}</text>
  <text x="415" y="176" text-anchor="middle" fill="{SVG_TEXT_MUTED}" font-size="12">{support_file}</text>
  <rect x="290" y="212" width="250" height="80" rx="18" fill="{p[3]}"></rect>
  <text x="415" y="244" text-anchor="middle" fill="{SVG_TEXT_DARK}" font-size="20">{tertiary}</text>
  <text x="415" y="268" text-anchor="middle" fill="{SVG_TEXT_ACCENT}" font-size="12">Derived output / docs / UI</text>
  <rect x="640" y="74" width="190" height="78" rx="18" fill="{p[5]}"></rect>
  <text x="735" y="106" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="18">Structure</text>
  <text x="735" y="130" text-anchor="middle" fill="{t[5]}" font-size="13">Module boundary</text>
  <rect x="640" y="178" width="190" height="78" rx="18" fill="{p[4]}"></rect>
  <text x="735" y="210" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="18">Validation</text>
  <text x="735" y="234" text-anchor="middle" fill="{t[4]}" font-size="13">Risk / quality check</text>
  <rect x="900" y="120" width="160" height="72" rx="18" fill="{p[0]}"></rect>
  <text x="980" y="150" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="18">Report Pack</text>
  <text x="980" y="174" text-anchor="middle" fill="{SVG_SUB_ON_DARK}" font-size="12">Daily / Weekly / Jira</text>
  <line x1="210" y1="156" x2="290" y2="68" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="210" y1="156" x2="290" y2="160" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="210" y1="156" x2="290" y2="252" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="540" y1="68" x2="640" y2="113" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="540" y1="160" x2="640" y2="113" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="540" y1="252" x2="640" y2="217" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="830" y1="113" x2="900" y2="156" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="830" y1="217" x2="900" y2="156" stroke="{SVG_STROKE}" stroke-width="4" marker-end="url(#arch-arrow)"></line>
</svg>
"""


def svg_change_impact_map(areas: list[dict[str, Any]], top_files: list[dict[str, Any]], commits: list[dict[str, Any]]) -> str:
    if not areas and not top_files and not commits:
        return "<p>No impact data</p>"
    nodes = []
    for idx, item in enumerate(areas[:3], start=1):
        nodes.append((f"Area {idx}", str(item.get("area", "area")), f'{int(item.get("count", 0))} files'))
    for idx, item in enumerate(top_files[:2], start=len(nodes) + 1):
        nodes.append((f"File {idx}", str(item.get("path", "")), f'+{int(item.get("added", 0))} / -{int(item.get("deleted", 0))}'))
    width = 1100
    height = 320
    _is = SVG_IMPACT_STROKE
    imp_colors = [SVG_PALETTE[1], SVG_PALETTE[2], SVG_PALETTE[3], SVG_PALETTE[0], SVG_PALETTE[5]]
    parts = [f'<svg viewBox="0 0 {width} {height}" class="flow" role="img" aria-label="Change impact map"><title>Change impact map</title>']
    parts.append(f'<defs><marker id="impact-arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="{_is}"></path></marker></defs>')
    parts.append(f'<rect x="30" y="126" width="190" height="70" rx="18" fill="{_is}"></rect>')
    parts.append(f'<text x="125" y="156" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="18">Changed Sources</text>')
    parts.append(f'<text x="125" y="178" text-anchor="middle" fill="{SVG_SUB_ON_WARM}" font-size="12">Evidence from git diff</text>')
    x_positions = [320, 320, 320, 630, 630]
    y_positions = [24, 116, 208, 70, 186]
    for idx, node in enumerate(nodes[:5]):
        title, label, meta = node
        x = x_positions[idx]
        y = y_positions[idx]
        fill = imp_colors[idx % len(imp_colors)]
        text_fill = svg_text_color_for(fill)
        meta_fill = SVG_META_LIGHT if text_fill == SVG_TEXT_LIGHT else SVG_TEXT_MUTED
        parts.append(f'<rect x="{x}" y="{y}" width="230" height="74" rx="18" fill="{fill}"></rect>')
        parts.append(f'<text x="{x+115}" y="{y+28}" text-anchor="middle" fill="{text_fill}" font-size="16">{escape(title)}</text>')
        parts.append(f'<text x="{x+115}" y="{y+48}" text-anchor="middle" fill="{text_fill}" font-size="13">{escape(label[:34])}</text>')
        parts.append(f'<text x="{x+115}" y="{y+64}" text-anchor="middle" fill="{meta_fill}" font-size="11">{escape(meta)}</text>')
    parts.append(f'<rect x="920" y="126" width="150" height="70" rx="18" fill="{SVG_BG_DARK}"></rect>')
    parts.append(f'<text x="995" y="156" text-anchor="middle" fill="{SVG_TEXT_LIGHT}" font-size="18">Impacted Output</text>')
    parts.append(f'<text x="995" y="178" text-anchor="middle" fill="{SVG_SUB_ON_DARK}" font-size="12">UI / API / Docs / Jira</text>')
    for target_y in (61, 153, 245):
        parts.append(f'<line x1="220" y1="161" x2="320" y2="{target_y}" stroke="{_is}" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append(f'<line x1="550" y1="61" x2="630" y2="107" stroke="{_is}" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append(f'<line x1="550" y1="153" x2="630" y2="107" stroke="{_is}" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append(f'<line x1="550" y1="245" x2="630" y2="223" stroke="{_is}" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append(f'<line x1="860" y1="107" x2="920" y2="161" stroke="{_is}" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append(f'<line x1="860" y1="223" x2="920" y2="161" stroke="{_is}" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append('</svg>')
    return ''.join(parts)

def svg_sprint_gantt(tasks: list[dict[str, Any]], sprint: dict[str, Any], today: date | None = None) -> str:
    """Render a Gantt chart SVG for sprint tasks."""
    if today is None:
        today = date.today()

    # Only parent tasks with dates
    gantt_tasks = [t for t in tasks if t.get("start") and t.get("end")]
    if not gantt_tasks:
        return ""

    # Sort by start date
    gantt_tasks.sort(key=lambda t: t.get("start", ""))

    # Determine time range from actual task dates (tighter than full sprint)
    task_dates = []
    for t in gantt_tasks:
        try:
            task_dates.append(date.fromisoformat(t["start"]))
            task_dates.append(date.fromisoformat(t["end"]))
        except (ValueError, TypeError):
            pass
    if task_dates:
        range_start = min(task_dates) - timedelta(days=3)
        range_end = max(max(task_dates), today) + timedelta(days=7)
    else:
        range_start = today - timedelta(days=14)
        range_end = today + timedelta(days=30)

    total_days = max((range_end - range_start).days, 1)

    row_h = 32
    header_h = 40
    left_margin = 320
    right_margin = 20
    chart_w = 1000
    bar_area = chart_w - left_margin - right_margin
    svg_h = header_h + len(gantt_tasks) * row_h + 20

    parts = [f'<div class="gantt-wrap"><svg viewBox="0 0 {chart_w} {svg_h}" role="img" aria-label="Sprint Gantt Chart">']
    parts.append(f'<title>Sprint Gantt Chart</title>')

    # Header: weekly markers only
    current = range_start
    while current <= range_end:
        x = left_margin + int(((current - range_start).days / total_days) * bar_area)
        if current.weekday() == 0:  # Monday
            parts.append(f'<text x="{x}" y="16" class="gantt-header">{current.strftime("%m/%d")}</text>')
            parts.append(f'<line x1="{x}" y1="22" x2="{x}" y2="{svg_h - 10}" stroke="var(--line)" stroke-width="0.5"/>')
        current += timedelta(days=1)

    # Today line
    if range_start <= today <= range_end:
        today_x = left_margin + int(((today - range_start).days / total_days) * bar_area)
        parts.append(f'<line x1="{today_x}" y1="22" x2="{today_x}" y2="{svg_h - 10}" class="gantt-today"/>')
        parts.append(f'<text x="{today_x}" y="{svg_h - 2}" class="gantt-date" text-anchor="middle">Today</text>')

    # Task bars
    status_cls_map = {"done": "done", "in_progress": "in-progress", "pending": "pending"}
    for idx, task in enumerate(gantt_tasks):
        y = header_h + idx * row_h
        key = escape(task.get("key", ""))
        title = escape(task.get("title", ""))[:28]

        try:
            t_start = date.fromisoformat(task["start"])
            t_end = date.fromisoformat(task["end"])
        except (ValueError, TypeError):
            continue

        st = task.get("status", "pending")
        cls = status_cls_map.get(st, "pending")
        # Overdue check
        if t_end < today and st != "done":
            cls = "overdue"

        bar_x = left_margin + max(0, int(((t_start - range_start).days / total_days) * bar_area))
        bar_w = max(8, int(((t_end - t_start).days / total_days) * bar_area))

        # Label
        parts.append(f'<text x="{left_margin - 8}" y="{y + 18}" class="gantt-label" text-anchor="end">{key} {title}</text>')
        # Bar
        parts.append(f'<rect x="{bar_x}" y="{y + 6}" width="{bar_w}" height="{row_h - 12}" class="gantt-bar {cls}"/>')
        # Date text on bar
        parts.append(f'<text x="{bar_x + 4}" y="{y + 20}" class="gantt-date">{task["start"][5:]} ~ {task["end"][5:]}</text>')

    parts.append('</svg></div>')
    return ''.join(parts)


def html_jira_live_board(project_config: dict[str, Any] | None = None) -> str:
    """Render an interactive Jira task board with action buttons.

    Fetches live data from Jira if configured, otherwise returns empty string.
    The board includes comment, complete, and add-subtask buttons that call
    the local jira_proxy server (localhost:18923).
    """
    try:
        from workflow.task_provider import get_task_provider
        provider = get_task_provider(project_config)
        if not hasattr(provider, 'add_comment'):
            return ""
        data = provider.get_tasks()
    except Exception:
        return ""

    tasks = data.get("tasks", [])
    if not tasks:
        return ""

    sprint = data.get("sprint", {})
    sprint_name = escape(sprint.get("name", ""))
    sprint_period = ""
    if sprint.get("start") and sprint.get("end"):
        sprint_period = f'{sprint["start"]} ~ {sprint["end"]}'

    status_cls = {"done": "done", "in_progress": "in-progress", "pending": "pending"}
    status_label = {"done": "Done", "in_progress": "In Progress", "pending": "To Do"}

    today = date.today()
    rows = []
    for task in tasks:
        key = escape(task.get("key", ""))
        title = escape(task.get("title", ""))
        st = task.get("status", "pending")
        cls = status_cls.get(st, "pending")
        label = status_label.get(st, "To Do")
        t_start = task.get("start", "")
        t_end = task.get("end", "")

        # Date display with overdue check
        date_html = ""
        if t_start and t_end:
            overdue_cls = ""
            try:
                if date.fromisoformat(t_end) < today and st != "done":
                    overdue_cls = " overdue"
            except ValueError:
                pass
            date_html = f'<span class="jira-date{overdue_cls}">{t_start[5:]} ~ {t_end[5:]}</span>'

        actions = ""
        if st == "in_progress":
            actions = f'''<button class="jira-btn complete" onclick="jiraComplete('{key}')">Complete</button>'''
        actions += f'''<button class="jira-btn" onclick="jiraComment('{key}')">Comment</button>'''
        actions += f'''<button class="jira-btn add" onclick="jiraAddSub('{key}')">+ Sub</button>'''

        rows.append(f'''<div class="jira-task" data-key="{key}">
  <span class="jira-key">{key}</span>
  <span class="jira-summary">{title}</span>
  {date_html}
  <span class="jira-task-actions">
    <span class="jira-status {cls}">{label}</span>
    {actions}
  </span>
</div>''')

        for sub in task.get("subtasks", []):
            skey = escape(sub.get("key", ""))
            stitle = escape(sub.get("title", ""))
            sst = sub.get("status", "pending")
            scls = status_cls.get(sst, "pending")
            slabel = status_label.get(sst, "To Do")

            sub_actions = ""
            if sst == "in_progress":
                sub_actions = f'''<button class="jira-btn complete" onclick="jiraComplete('{skey}')">Complete</button>'''
            sub_actions += f'''<button class="jira-btn" onclick="jiraComment('{skey}')">Comment</button>'''

            rows.append(f'''<div class="jira-task subtask" data-key="{skey}">
  <span class="jira-key">{skey}</span>
  <span class="jira-summary">{stitle}</span>
  <span class="jira-task-actions">
    <span class="jira-status {scls}">{slabel}</span>
    {sub_actions}
  </span>
</div>''')

    # Count all items including subtasks
    all_items = []
    for t in tasks:
        all_items.append(t)
        all_items.extend(t.get("subtasks", []))
    done_count = sum(1 for t in all_items if t.get("status") == "done")
    in_prog = sum(1 for t in all_items if t.get("status") == "in_progress")
    pending = sum(1 for t in all_items if t.get("status") == "pending")

    gantt_html = svg_sprint_gantt(tasks, sprint, today)

    return f'''
{gantt_html}
<div class="jira-board" id="jira-live-board">
  <div class="jira-board-header">
    <div>
      <h3>Jira Sprint Board</h3>
      <span class="sprint-meta">{sprint_name} &middot; {sprint_period}</span>
    </div>
    <div class="jira-board-actions">
      <span class="jira-status done">{done_count} Done</span>
      <span class="jira-status in-progress">{in_prog} In Progress</span>
      <span class="jira-status pending">{pending} To Do</span>
    </div>
  </div>
  {"".join(rows)}
  <div class="jira-board-footer">
    <button class="jira-btn" onclick="jiraRefresh()">Refresh</button>
  </div>
</div>
<div id="jira-toast" class="jira-toast"></div>
'''


JIRA_BOARD_SCRIPT = """
<script>
(function() {
  const API = 'http://localhost:18923';

  window.jiraToast = function(msg) {
    const el = document.getElementById('jira-toast');
    el.textContent = msg;
    el.classList.add('show');
    setTimeout(() => el.classList.remove('show'), 2500);
  };

  window.jiraComplete = function(key) {
    const overlay = document.createElement('div');
    overlay.className = 'jira-modal-overlay';
    overlay.innerHTML = `
      <div class="jira-modal">
        <h4>${key} - 완료 처리</h4>
        <textarea id="jira-modal-text" rows="3" placeholder="완료 내용을 간단히 정리하세요..."></textarea>
        <div class="modal-actions">
          <button class="jira-btn" onclick="this.closest('.jira-modal-overlay').remove()">취소</button>
          <button class="jira-btn complete" onclick="jiraDoComplete('${key}', this)">종료 요청</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('textarea').focus();
  };

  window.jiraDoComplete = function(key, btn) {
    const text = document.getElementById('jira-modal-text').value;
    if (!text.trim()) { jiraToast('댓글을 입력하세요'); return; }
    btn.disabled = true;
    btn.textContent = '처리 중...';
    fetch(API + '/api/issue/' + key + '/complete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({comment: text})
    }).then(r => r.json()).then(d => {
      btn.closest('.jira-modal-overlay').remove();
      if (d.ok) {
        jiraToast(key + ' → 종료 요청 완료');
        const row = document.querySelector('[data-key="' + key + '"]');
        if (row) {
          const st = row.querySelector('.jira-status');
          if (st) { st.className = 'jira-status done'; st.textContent = 'Done'; }
        }
      } else { jiraToast('실패: ' + (d.error || 'unknown')); }
    }).catch(e => { btn.closest('.jira-modal-overlay').remove(); jiraToast('프록시 서버 미실행 (python scripts/jira_proxy.py) (localhost:18923)'); });
  };

  window.jiraComment = function(key) {
    const overlay = document.createElement('div');
    overlay.className = 'jira-modal-overlay';
    overlay.innerHTML = `
      <div class="jira-modal">
        <h4>${key} - 댓글 작성</h4>
        <textarea id="jira-modal-text" rows="3" placeholder="진행 상황이나 메모를 남기세요..."></textarea>
        <div class="modal-actions">
          <button class="jira-btn" onclick="this.closest('.jira-modal-overlay').remove()">취소</button>
          <button class="jira-btn" onclick="jiraDoComment('${key}', this)">작성</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('textarea').focus();
  };

  window.jiraDoComment = function(key, btn) {
    const text = document.getElementById('jira-modal-text').value;
    if (!text.trim()) { jiraToast('댓글을 입력하세요'); return; }
    btn.disabled = true;
    fetch(API + '/api/issue/' + key + '/comment', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({comment: text})
    }).then(r => r.json()).then(d => {
      btn.closest('.jira-modal-overlay').remove();
      jiraToast(d.ok ? key + ' 댓글 작성 완료' : '실패');
    }).catch(() => { btn.closest('.jira-modal-overlay').remove(); jiraToast('프록시 서버 미실행 (python scripts/jira_proxy.py)'); });
  };

  window.jiraAddSub = function(parentKey) {
    const overlay = document.createElement('div');
    overlay.className = 'jira-modal-overlay';
    overlay.innerHTML = `
      <div class="jira-modal">
        <h4>${parentKey} - 부작업 추가</h4>
        <input id="jira-modal-text" type="text" placeholder="부작업 제목을 입력하세요...">
        <div class="modal-actions">
          <button class="jira-btn" onclick="this.closest('.jira-modal-overlay').remove()">취소</button>
          <button class="jira-btn add" onclick="jiraDoAdd('${parentKey}', this)">생성</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('input').focus();
  };

  window.jiraDoAdd = function(parentKey, btn) {
    const text = document.getElementById('jira-modal-text').value;
    if (!text.trim()) { jiraToast('제목을 입력하세요'); return; }
    btn.disabled = true;
    fetch(API + '/api/issue/create', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({parent_key: parentKey, summary: text})
    }).then(r => r.json()).then(d => {
      btn.closest('.jira-modal-overlay').remove();
      if (d.ok) { jiraToast(d.key + ' 생성 완료 (' + parentKey + ' 하위)'); }
      else { jiraToast('실패: ' + (d.error || 'unknown')); }
    }).catch(() => { btn.closest('.jira-modal-overlay').remove(); jiraToast('프록시 서버 미실행 (python scripts/jira_proxy.py)'); });
  };

  window.jiraRefresh = function() { location.reload(); };
})();
</script>
"""


def html_jira_suggestions_panel(suggestions: list[dict[str, Any]]) -> str:
    """Render an interactive suggestion review panel for auto-generated Jira actions."""
    if not suggestions:
        return ""

    pending = [s for s in suggestions if s.get("status") == "pending"]
    if not pending:
        return ""

    high_count = sum(1 for s in pending if s.get("confidence") == "high")
    type_icons = {"comment": "Comment", "complete": "Complete", "add_subtask": "+ Sub", "transition": "Start"}

    rows = []
    for s in pending:
        sid = escape(s.get("id", ""))
        key = escape(s.get("task_key", ""))
        stype = s.get("type", "comment")
        title = escape(s.get("title", ""))
        text = escape(s.get("suggested_text", ""))
        reason = escape(s.get("reason", ""))
        conf = s.get("confidence", "medium")
        icon_label = type_icons.get(stype, "Action")
        collapsed = ' suggestion-collapsed' if conf == "low" else ""

        rows.append(f'''<div class="jira-suggestion{collapsed}" data-sid="{sid}" data-key="{key}" data-type="{stype}">
  <div class="suggestion-head">
    <span class="jira-key">{key}</span>
    <span class="suggestion-type {stype}">{icon_label}</span>
    <span class="confidence {conf}">{conf}</span>
    <span style="flex:1"></span>
    <strong style="font-size:13px">{title}</strong>
  </div>
  <div class="suggestion-reason">{reason}</div>
  <textarea class="suggestion-text" id="text-{sid}">{text}</textarea>
  <div class="suggestion-actions">
    <button class="jira-btn approve" onclick="suggApprove('{sid}')">승인</button>
    <button class="jira-btn reject" onclick="suggReject('{sid}')">거절</button>
  </div>
</div>''')

    low_count = sum(1 for s in pending if s.get("confidence") == "low")
    toggle = ""
    if low_count:
        toggle = f'<button class="suggestion-toggle" onclick="suggToggleLow()">낮은 확신 {low_count}건 더 보기</button>'

    return f'''
<div class="jira-suggestions" id="jira-suggestions-panel">
  <div class="jira-suggestions-header">
    <div>
      <h3>Jira 제안 리뷰</h3>
      <span class="sprint-meta">{len(pending)}건 대기 &middot; {high_count}건 높은 확신</span>
    </div>
    <div class="jira-board-actions">
      <button class="jira-btn approve" onclick="suggBatchApprove()">전체 승인</button>
    </div>
  </div>
  {"".join(rows)}
  {toggle}
</div>
'''


JIRA_SUGGESTIONS_SCRIPT = """
<script>
(function() {
  const API = 'http://localhost:18923';

  window.suggApprove = function(sid) {
    const card = document.querySelector('[data-sid="' + sid + '"]');
    if (!card) return;
    const key = card.dataset.key;
    const type = card.dataset.type;
    const text = document.getElementById('text-' + sid).value;
    const btn = card.querySelector('.approve');
    btn.disabled = true;
    btn.textContent = '처리 중...';

    fetch(API + '/api/suggestions/' + sid + '/approve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_key: key, type: type, text: text})
    }).then(r => r.json()).then(d => {
      if (d.ok) {
        card.classList.add('applied');
        btn.textContent = '완료';
        jiraToast(key + ' 제안 승인 완료');
      } else {
        btn.textContent = '실패';
        jiraToast('실패: ' + (d.error || 'unknown'));
      }
    }).catch(() => { btn.textContent = '연결 실패'; jiraToast('프록시 서버 미실행 (python scripts/jira_proxy.py)'); });
  };

  window.suggReject = function(sid) {
    const card = document.querySelector('[data-sid="' + sid + '"]');
    if (!card) return;
    fetch(API + '/api/suggestions/' + sid + '/reject', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    }).then(r => r.json()).then(d => {
      card.classList.add('applied');
      card.querySelector('.reject').textContent = '거절됨';
      jiraToast(card.dataset.key + ' 제안 거절');
    }).catch(() => { jiraToast('프록시 서버 미실행 (python scripts/jira_proxy.py)'); });
  };

  window.suggBatchApprove = function() {
    const cards = document.querySelectorAll('.jira-suggestion:not(.applied):not(.suggestion-collapsed)');
    cards.forEach(card => {
      const sid = card.dataset.sid;
      suggApprove(sid);
    });
  };

  window.suggToggleLow = function() {
    document.querySelectorAll('.suggestion-collapsed').forEach(el => {
      el.classList.remove('suggestion-collapsed');
    });
    const btn = document.querySelector('.suggestion-toggle');
    if (btn) btn.remove();
  };
})();
</script>
"""


def html_task_board(plan_sections: dict[str, Any], result_sections: dict[str, Any]) -> str:
    task_name = escape(str(plan_sections.get("task_name") or result_sections.get("task_name") or "-"))
    task_goal = escape(str(plan_sections.get("task_goal") or "-"))
    subtasks = list(plan_sections.get("subtasks") or [])
    subtask_results = list(result_sections.get("subtask_results") or [])
    done_items = list(result_sections.get("done_items") or [])
    validations = list(plan_sections.get("validation") or result_sections.get("validation") or [])

    columns = []
    columns.append(
        f"""
<div class="task-box parent">
  <div class="task-label">Parent Task</div>
  <h4>{task_name}</h4>
  <p>{task_goal}</p>
</div>
"""
    )

    if subtasks:
        done_count = min(len(subtask_results) if subtask_results else len(done_items), len(subtasks))
        rows = []
        for idx, item in enumerate(subtasks[:6]):
            priority = "High" if idx == 0 else ("Medium" if idx < 3 else "Low")
            status = "Done" if idx < done_count else "Planned"
            dod = validations[idx % len(validations)] if validations else "Validation needed"
            rows.append(
                f"""
<li>
  <div class="subtask-row">
    <span class="check {'done' if status == 'Done' else ''}">{'✓' if status == 'Done' else '○'}</span>
    <div class="subtask-copy">
      <strong>{escape(str(item))}</strong>
      <span>Priority: {priority} / DoD: {escape(str(dod))}</span>
    </div>
    <span class="state {status.lower()}">{status}</span>
  </div>
</li>
"""
            )
        items = "".join(rows)
        columns.append(
            f"""
<div class="task-box child">
  <div class="task-label">Subtasks</div>
  <ul>{items}</ul>
</div>
"""
        )

    if subtask_results or done_items:
        result_items = subtask_results[:6] if subtask_results else done_items[:6]
        items = "".join(f"<li>{escape(str(item))}</li>" for item in result_items)
        columns.append(
            f"""
<div class="task-box result">
  <div class="task-label">Result</div>
  <ul>{items}</ul>
</div>
"""
        )

    return f"""
<div class="task-board">
  {''.join(columns)}
</div>
"""


def render_html_dashboard(today: date, cards: list[dict[str, Any]], project_configs: list[dict[str, Any]] | None = None, jira_suggestions: list[dict[str, Any]] | None = None) -> str:
    total_commits = sum(int(card["payload"].get("commit_count", 0)) for card in cards)
    total_files = sum(int(card["payload"].get("changed_file_count", 0)) for card in cards)
    total_added = sum(int((card["payload"].get("diff_summary") or {}).get("total_added", 0)) for card in cards)
    total_deleted = sum(int((card["payload"].get("diff_summary") or {}).get("total_deleted", 0)) for card in cards)
    # Build Jira live boards for projects that have jira config
    jira_boards_html = ""
    if project_configs:
        for pc in project_configs:
            if isinstance(pc.get("jira"), dict):
                jira_boards_html += html_jira_live_board(pc)
    # Add suggestions panel below Jira board
    suggestions_html = html_jira_suggestions_panel(jira_suggestions or [])
    jira_boards_html += suggestions_html
    card_html = []
    for card in cards:
        payload = card["payload"]
        sections = card.get("sections") or {}
        areas = payload.get("top_areas") or []
        commits = payload.get("recent_commits") or []
        changed_docs = payload.get("changed_docs") or []
        facets = payload.get("change_facets") or []
        primary_facets = payload.get("primary_change_facets") or []
        supporting_facets = payload.get("supporting_change_facets") or []
        diff = payload.get("diff_summary") or {}
        ai_team = sections.get("_ai_team") or {}
        ai_mode = str(sections.get("_ai_team_mode") or "fallback")
        primary_facet_html = "".join(
            f'<span class="facet-badge facet-badge-primary"><strong>{escape(str(item.get("name", "")))}</strong><em>{escape(str(item.get("reason", "")))}</em></span>'
            for item in primary_facets
        )
        supporting_facet_html = "".join(
            f'<span class="facet-badge facet-badge-support"><strong>{escape(str(item.get("name", "")))}</strong><em>{escape(str(item.get("reason", "")))}</em></span>'
            for item in supporting_facets
        )
        if not primary_facet_html and not supporting_facet_html:
            supporting_facet_html = "".join(
                f'<span class="facet-badge facet-badge-support"><strong>{escape(str(item.get("name", "")))}</strong><em>{escape(str(item.get("reason", "")))}</em></span>'
                for item in facets
            ) or '<span class="facet-badge facet-badge-support"><strong>유지보수</strong><em>추가 분류 근거가 부족해 기본 태그를 사용했습니다.</em></span>'
        ai_panel = ""
        if ai_team:
            ai_panel = f"""
  <div class="grid">
    <div class="panel">
      <h3>Gemini Structure / Quality</h3>
      <p class="mini-meta">Mode: {escape(ai_mode)}</p>
      <ul>{"".join(f"<li>{escape(str(x))}</li>" for x in (list(ai_team.get('structure') or [])[:2] + list(ai_team.get('quality') or [])[:2])) or "<li>No AI analysis</li>"}</ul>
    </div>
    <div class="panel">
      <h3>Gemini Feature / Jira</h3>
      <ul>{"".join(f"<li>{escape(str(x))}</li>" for x in (list(ai_team.get('feature') or [])[:2] + list(ai_team.get('jira_strategy') or [])[:2])) or "<li>No AI analysis</li>"}</ul>
    </div>
  </div>
"""
        tone = {
            "daily": "tone-daily",
            "plan": "tone-plan",
            "jira": "tone-jira",
            "weekly": "tone-weekly",
            "monthly": "tone-monthly",
        }.get(card["report_type"], "tone-default")
        is_jira_plan = card["report_type"] == "jira"
        is_jira_result = False
        board_html = ""
        if is_jira_plan:
            board_html = html_task_board(sections, sections)
        card_html.append(
            f"""
<section class="card {tone}">
  <div class="card-head">
    <div>
      <h2>{escape(card['title'])}</h2>
      <p class="meta">{escape(card['report_type']).upper()} · {escape(card['mode']).upper()} · {escape(work_type_label(str(payload.get('work_type', ''))))}</p>
    </div>
    <a class="file-link" href="{escape(card.get('html_path', card['path']).as_uri())}">Open Detail Report</a>
  </div>
  <div class="stats">
    <div><span>Commits</span><strong>{payload.get('commit_count', 0)}</strong></div>
    <div><span>Changed Files</span><strong>{payload.get('changed_file_count', 0)}</strong></div>
    <div><span>Added Lines</span><strong>{diff.get('total_added', 0)}</strong></div>
    <div><span>Deleted Lines</span><strong>{diff.get('total_deleted', 0)}</strong></div>
  </div>
  <div class="facet-group-inline">
    <div class="facet-strip">{primary_facet_html or '<span class="facet-badge facet-badge-primary"><strong>핵심 변경</strong><em>주요 변경 성격이 아직 분리되지 않았습니다.</em></span>'}</div>
    <div class="facet-strip">{supporting_facet_html or '<span class="facet-badge facet-badge-support"><strong>보조 변경 없음</strong><em>이번 리포트는 핵심 변경 중심으로 정리되었습니다.</em></span>'}</div>
  </div>
  {ai_panel}
  {board_html}
  <div class="grid">
    <div class="panel">
      <h3>Top Change Areas</h3>
      {svg_area_bars(areas[:5])}
    </div>
    <div class="panel">
      <h3>Architecture Delta</h3>
      {svg_architecture_delta(areas[:4], (payload.get("diff_summary") or {}).get("top_files") or [])}
    </div>
  </div>
  <div class="grid">
    <div class="panel">
      <h3>Recent Commits</h3>
      <ul>{"".join(f"<li><code>{escape(str(c.get('hash','')))}</code> {escape(str(c.get('subject','')))}</li>" for c in commits[:6]) or "<li>No commits</li>"}</ul>
    </div>
    <div class="panel">
      <h3>Documentation Footprint</h3>
      <ul>{"".join(f"<li><code>{escape(str(d))}</code></li>" for d in changed_docs[:6]) or "<li>No markdown docs changed</li>"}</ul>
    </div>
  </div>
</section>
"""
        )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Startup Reports {today.isoformat()}</title>
  <style>
{DESIGN_CSS}
  </style>
</head>
  <body>
  <a href="#main-content" class="skip-link">Skip to content</a>
  <button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()">Light</button>
  <div class="wrap" id="main-content">
    <header class="hero">
      <div class="hero-grid">
        <div>
          <div class="eyebrow">Executive Report View</div>
          <h1>Startup Report Dashboard</h1>
          <p>{today.isoformat()} generated reports with Git evidence, GitHub metadata, AI or fallback summaries, and presentation-ready visuals.</p>
        </div>
        <div class="hero-kpis">
          <div class="hero-kpi"><span>Total Commits</span><strong>{total_commits}</strong></div>
          <div class="hero-kpi"><span>Total Files</span><strong>{total_files}</strong></div>
          <div class="hero-kpi"><span>Added Lines</span><strong>{total_added}</strong></div>
          <div class="hero-kpi"><span>Deleted Lines</span><strong>{total_deleted}</strong></div>
        </div>
      </div>
    </header>
    {jira_boards_html}
    {"".join(card_html)}
  </div>
{JIRA_BOARD_SCRIPT if jira_boards_html else ""}
{JIRA_SUGGESTIONS_SCRIPT if suggestions_html else ""}
<script>
(function() {{
  const btn = document.getElementById('theme-toggle');
  const saved = localStorage.getItem('dashboard-theme');
  if (saved) {{
    document.documentElement.setAttribute('data-theme', saved);
    btn.textContent = saved === 'dark' ? 'Light' : 'Dark';
  }} else {{
    const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    btn.textContent = isDark ? 'Light' : 'Dark';
  }}
  window.toggleTheme = function() {{
    const current = document.documentElement.getAttribute('data-theme');
    const isDark = current === 'dark' || (!current && window.matchMedia('(prefers-color-scheme: dark)').matches);
    const next = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('dashboard-theme', next);
    btn.textContent = next === 'dark' ? 'Light' : 'Dark';
  }};
}})();
</script>
</body>
</html>"""


def make_payload(
    *,
    today: date,
    report_type: str,
    window: ReportWindow,
    repo_root: Path,
    branch: str,
    remote_url: str,
    upstream: str | None,
    sync_state: tuple[int, int] | None,
    commits: list[Commit],
    changed_files: list[str],
    uncommitted: list[str],
    profile_name: str,
) -> dict[str, Any]:
    github_meta = fetch_github_metadata(remote_url, branch, window, commits)
    return build_context_payload(
        today=today,
        report_type=report_type,
        window=window,
        repo_root=repo_root,
        branch=branch,
        remote_url=remote_url,
        upstream=upstream,
        sync_state=sync_state,
        commits=commits,
        changed_files=changed_files,
        uncommitted=uncommitted,
        github_meta=github_meta,
        profile_name=profile_name,
    )


def build_week_window(today: date) -> ReportWindow:
    monday = today - timedelta(days=today.weekday())
    end = previous_business_day(today)
    # On Monday, end becomes previous Friday — use previous week's window
    if end < monday:
        monday = monday - timedelta(days=7)
    return ReportWindow(start=monday, end=end, label=f"{monday.isoformat()}_to_{end.isoformat()}")


def build_previous_month_window(today: date) -> ReportWindow:
    start, end = previous_month(today)
    return ReportWindow(start=start, end=end, label=start.strftime("%Y-%m"))


def default_domain_profile(repo_name: str) -> str:
    name = repo_name.lower()
    if name == "260105":
        return "uds_quality"
    if "greencore" in name:
        return "desktop_app"
    if "autoreport" in name:
        return "reporting_automation"
    return "general_software"


def get_domain_profile(profile_name: str) -> dict[str, Any]:
    profiles = {
        "uds_quality": {
            "name": "UDS 품질 분석",
            "focus": [
                "UDS 생성 흐름",
                "품질 게이트와 validation",
                "테스트/회귀 검증",
                "소스 파싱 및 영향 분석",
            ],
        },
        "desktop_app": {
            "name": "데스크톱 애플리케이션 분석",
            "focus": [
                "기능 동작 변화",
                "UI/UX 흐름",
                "앱 구조와 배포 영향",
                "사용자 시나리오 검증",
            ],
        },
        "reporting_automation": {
            "name": "리포팅 자동화 분석",
            "focus": [
                "자동화 스케줄링",
                "리포트 생성 파이프라인",
                "HTML/Markdown 렌더링",
                "운영 안정성과 재시도",
            ],
        },
        "general_software": {
            "name": "일반 소프트웨어 분석",
            "focus": [
                "기능 변화",
                "구조 변경",
                "품질/테스트 영향",
                "작업 계획과 Jira 정리",
            ],
        },
    }
    return profiles.get(profile_name, profiles["general_software"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate startup daily/weekly/monthly reports.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--date", default=None, help="Reference date YYYY-MM-DD")
    parser.add_argument("--output-root", default=None, help="Optional output root directory")
    parser.add_argument("--profile", default=None, help="Optional domain profile for AI analysis")
    return parser.parse_args()


def main() -> int:
    # Load .env for Jira credentials and other config
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    args = parse_args()
    repo_root = detect_repo_root(Path(args.repo).resolve())
    output_root = Path(args.output_root).resolve() if args.output_root else repo_root
    today = date.fromisoformat(args.date) if args.date else date.today()
    profile_name = str(args.profile or default_domain_profile(repo_root.name))
    branch = detect_branch(repo_root)
    remote_url = detect_remote_url(repo_root)
    upstream = detect_upstream(repo_root)
    sync_state = ahead_behind(repo_root, upstream)
    generated: list[Path] = []

    last_business_day = previous_business_day(today)
    daily_window = ReportWindow(last_business_day, last_business_day, last_business_day.isoformat())
    daily_commits = get_commits(repo_root, branch, daily_window.start, daily_window.end)
    daily_files = [path for path in get_changed_files(repo_root, branch, daily_window.start, daily_window.end) if is_relevant_path(path)]
    uncommitted = get_uncommitted(repo_root)

    base_payload = make_payload(
        today=today,
        report_type="daily",
        window=daily_window,
        repo_root=repo_root,
        branch=branch,
        remote_url=remote_url,
        upstream=upstream,
        sync_state=sync_state,
        commits=daily_commits,
        changed_files=daily_files,
        uncommitted=uncommitted,
        profile_name=profile_name,
    )

    cleanup_legacy_jira_outputs(output_root, today)

    output_specs = [
        ("daily", output_root / "reports" / "daily_brief" / f"{today.isoformat()}-daily-report.md"),
        ("plan", output_root / "reports" / "plans" / f"{today.isoformat()}-next-plan.md"),
        ("jira", output_root / "reports" / "jira" / f"{today.isoformat()}-jira-status.md"),
    ]
    dashboard_cards: list[dict[str, Any]] = []
    for report_type, path in output_specs:
        payload = dict(base_payload)
        payload["report_type"] = report_type
        text, mode, sections = generate_document(report_type, payload)
        write_text(path, text)
        html_path = path.with_suffix(".html")
        write_text(html_path, render_detail_html(report_type, sections, payload, mode, path))
        generated.append(path)
        generated.append(html_path)
        dashboard_cards.append({"report_type": report_type, "title": text.splitlines()[0].lstrip("# ").strip(), "path": path, "html_path": html_path, "payload": payload, "mode": mode, "sections": sections})

    if should_generate_weekly(today):
        week_window = build_week_window(today)
        weekly_dir = output_root / "reports" / "weekly_brief"
        monday = today - timedelta(days=today.weekday())
        week_already_exists = any(
            weekly_dir.glob(f"{(monday + timedelta(days=d)).isoformat()}-weekly-report.md")
            for d in range(7)
        ) if weekly_dir.exists() else False
        if not week_already_exists:
            weekly_commits = get_commits(repo_root, branch, week_window.start, week_window.end)
            weekly_files = [path for path in get_changed_files(repo_root, branch, week_window.start, week_window.end) if is_relevant_path(path)]
            weekly_payload = make_payload(
                today=today,
                report_type="weekly",
                window=week_window,
                repo_root=repo_root,
                branch=branch,
                remote_url=remote_url,
                upstream=upstream,
                sync_state=sync_state,
                commits=weekly_commits,
                changed_files=weekly_files,
                uncommitted=uncommitted,
                profile_name=profile_name,
            )
            weekly_path = output_root / "reports" / "weekly_brief" / f"{today.isoformat()}-weekly-report.md"
            weekly_text, weekly_mode, weekly_sections = generate_document("weekly", weekly_payload)
            write_text(weekly_path, weekly_text)
            weekly_html_path = weekly_path.with_suffix(".html")
            write_text(weekly_html_path, render_detail_html("weekly", weekly_sections, weekly_payload, weekly_mode, weekly_path))
            generated.append(weekly_path)
            generated.append(weekly_html_path)
            dashboard_cards.append({"report_type": "weekly", "title": weekly_text.splitlines()[0].lstrip("# ").strip(), "path": weekly_path, "html_path": weekly_html_path, "payload": weekly_payload, "mode": weekly_mode, "sections": weekly_sections})

    if should_generate_monthly(today):
        month_window = build_previous_month_window(today)
        monthly_path = output_root / "reports" / "monthly_brief" / f"{month_window.label}-monthly-report.md"
        if not monthly_path.exists():
            monthly_commits = get_commits(repo_root, branch, month_window.start, month_window.end)
            monthly_files = [path for path in get_changed_files(repo_root, branch, month_window.start, month_window.end) if is_relevant_path(path)]
            monthly_payload = make_payload(
                today=today,
                report_type="monthly",
                window=month_window,
                repo_root=repo_root,
                branch=branch,
                remote_url=remote_url,
                upstream=upstream,
                sync_state=sync_state,
                commits=monthly_commits,
                changed_files=monthly_files,
                uncommitted=uncommitted,
                profile_name=profile_name,
            )
            monthly_text, monthly_mode, monthly_sections = generate_document("monthly", monthly_payload)
            write_text(monthly_path, monthly_text)
            monthly_html_path = monthly_path.with_suffix(".html")
            write_text(monthly_html_path, render_detail_html("monthly", monthly_sections, monthly_payload, monthly_mode, monthly_path))
            generated.append(monthly_path)
            generated.append(monthly_html_path)
            dashboard_cards.append({"report_type": "monthly", "title": monthly_text.splitlines()[0].lstrip("# ").strip(), "path": monthly_path, "html_path": monthly_html_path, "payload": monthly_payload, "mode": monthly_mode, "sections": monthly_sections})

    # Auto-start Jira proxy server if not running
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:18923/api/sprint/tasks", timeout=2)
    except Exception:
        try:
            import subprocess
            proxy_script = Path(__file__).resolve().parent / "jira_proxy.py"
            if proxy_script.exists():
                subprocess.Popen(
                    [sys.executable, str(proxy_script)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                print("Started jira_proxy.py (http://localhost:18923)")
        except Exception:
            pass

    # Load project configs for Jira board integration
    _project_configs = []
    try:
        _sp_path = Path(__file__).resolve().parent / "startup_projects.json"
        if _sp_path.exists():
            with open(_sp_path, encoding="utf-8") as _f:
                _project_configs = json.load(_f).get("projects", [])
    except Exception:
        pass

    # Generate Jira suggestions from matched tasks + AI analysis
    _jira_suggestions: list[dict[str, Any]] = []
    for card in dashboard_cards:
        if card["report_type"] == "jira":
            _jira_suggestions = generate_jira_suggestions(
                card["payload"],
                card.get("sections"),
            )
            if _jira_suggestions:
                sugg_path = output_root / "reports" / "jira" / f"{today.isoformat()}-jira-suggestions.json"
                write_text(sugg_path, json.dumps(
                    {"date": today.isoformat(), "suggestions": _jira_suggestions},
                    ensure_ascii=False, indent=2,
                ))
                generated.append(sugg_path)
            break

    dashboard_path = output_root / "reports" / "dashboard" / f"{today.isoformat()}-startup-dashboard.html"
    write_text(dashboard_path, render_html_dashboard(today, dashboard_cards, _project_configs, _jira_suggestions))
    generated.append(dashboard_path)

    print("Generated reports:")
    for path in generated:
        print(path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"Failed to generate periodic reports: {exc}", file=sys.stderr)
        raise SystemExit(1)
