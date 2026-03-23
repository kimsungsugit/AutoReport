from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
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


def load_get_adapter():
    module_path = REPO_ROOT / "workflow" / "llm_adapters.py"
    spec = importlib.util.spec_from_file_location("workflow_llm_adapters", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load adapter module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.get_adapter


get_adapter = load_get_adapter()

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
    return today.weekday() == 4


def should_generate_monthly(today: date) -> bool:
    if today.weekday() != 0:
        return False
    _, prev_month_end = previous_month(today)
    return today > prev_month_end


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    path.write_text(text, encoding="utf-8")


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
    }


def build_fallback_sections(report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    commits = payload["recent_commits"]
    areas = payload["top_areas"]
    uncommitted_count = payload["uncommitted_count"]
    work_type = work_type_label(payload["work_type"])
    if report_type == "daily":
        return {
            "title": f"데일리 리포트 - {payload['today']}",
            "summary": [f"작업 유형은 {work_type}로 분류했습니다.", "전일 변경 이력을 기준으로 자동 생성한 요약입니다.", *(entry["subject"] for entry in commits[:2])][:4],
            "completed": [entry["subject"] for entry in commits[:5]] or ["집계 구간 내 커밋이 없습니다."],
            "focus": [f"{item['area']} 영역 점검" for item in areas[:3]] or ["신규 작업 우선순위 확인"],
            "risks": ["미커밋 변경이 남아 있습니다."] if uncommitted_count else ["즉시 보이는 로컬 변경 리스크는 없습니다."],
            "next_actions": [f"{item['area']} 후속 검증 진행" for item in areas[:3]] or ["다음 작업 후보를 정리합니다."],
        }
    if report_type == "plan":
        return {
            "title": f"진행 계획서 - {payload['today']}",
            "summary": [f"작업 유형은 {work_type}이며 최근 변경을 기준으로 계획 초안을 생성했습니다."],
            "priority_actions": [("미커밋 변경을 정리하고 커밋 단위를 명확히 합니다." if uncommitted_count else "최근 변경사항 검증을 우선 수행합니다."), *[f"{item['area']} 영역 테스트 및 마무리 작업" for item in areas[:3]]][:4],
            "mid_term_actions": [f"{item['area']} 관련 문서와 테스트를 보강합니다." for item in areas[:3]] or ["다음 요구사항 후보를 정리합니다."],
            "risks": ["작업 범위가 넓어 문서 반영 누락 가능성이 있습니다."],
            "notes": ["자동 생성 초안이므로 실제 우선순위와 비교해 조정이 필요합니다."],
        }
    if report_type == "weekly":
        return {
            "title": f"주간 리포트 - {payload['window_start']} to {payload['window_end']}",
            "summary": [f"이번 주 작업 유형 중심은 {work_type} 입니다."],
            "highlights": [entry["subject"] for entry in commits[:5]] or ["이번 주 커밋이 없습니다."],
            "areas": [f"{item['area']} {item['count']}개 파일 변경" for item in areas[:5]] or ["주요 변경 영역이 없습니다."],
            "risks": ["다음 주 초반 안정화 작업이 필요할 수 있습니다."],
            "next_week": [f"{item['area']} 안정화 및 검증" for item in areas[:3]] or ["다음 주 우선순위 재정의"],
        }
    return {
        "title": f"월간 리포트 - {payload['window_start']} to {payload['window_end']}",
        "summary": [f"이번 달 작업 유형 중심은 {work_type} 입니다."],
        "highlights": [entry["subject"] for entry in commits[:6]] or ["이번 달 커밋이 없습니다."],
        "areas": [f"{item['area']} {item['count']}개 파일 변경" for item in areas[:6]] or ["주요 변경 영역이 없습니다."],
        "risks": ["반복 변경 영역은 설계 문서 보강이 필요할 수 있습니다."],
        "next_month": [f"{item['area']} 구조 안정화 및 테스트 보강" for item in areas[:3]] or ["다음 달 우선순위 정리"],
    }


def build_fallback_jira_doc(doc_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    areas = payload["top_areas"]
    commits = payload["recent_commits"]
    work_type = work_type_label(payload["work_type"])
    diff = payload.get("diff_summary") or {}
    source_insights = list(payload.get("source_insights") or [])
    top_files = [str(item.get("path", "")) for item in (diff.get("top_files") or [])[:4]]
    area_items = [str(item.get("area", "")) for item in areas[:4]]
    area_subtasks = [
        f"{area} 영역 변경 검토 및 검증 정리"
        for area in area_items[:3]
        if area
    ]
    if top_files:
        area_subtasks.append(f"핵심 영향 파일 리뷰 및 결과 반영 ({', '.join(top_files[:2])})")
    area_subtasks = area_subtasks[:4] or ["핵심 변경 영역 검토 및 결과 정리"]
    if doc_type == "jira_plan":
        return {
            "title": f"[{work_type}] {payload['today']} 작업",
            "summary": source_insights[0] if source_insights else f"{work_type} 유형 작업에 대한 Jira 상위 작업 초안입니다.",
            "task_name": f"{payload['repository']} {work_type} 작업",
            "task_goal": source_insights[1] if len(source_insights) > 1 else "최근 변경 이력과 영향 파일을 기준으로 큰 단위의 후속 작업과 검증 범위를 정리합니다.",
            "scope": [*source_insights[:2], *[f"{item['area']} 영역 후속 작업" for item in areas[:4]]][:4] or ["주요 변경 영역 후속 작업"],
            "subtasks": area_subtasks,
            "validation": ["기능 검증", "관련 테스트 확인", "문서 및 Jira 결과 반영 확인"],
            "risks": ["자동 분류 결과이므로 실제 Jira 이슈 타입과 우선순위 비교가 필요합니다."],
        }
    done_items = [entry["subject"] for entry in commits[:5]] or ["집계된 완료 항목이 없습니다."]
    subtask_results = [
        f"{area} 영역 변경 검토 결과와 검증 포인트를 정리했습니다."
        for area in area_items[:3]
        if area
    ]
    if top_files:
        subtask_results.append(f"핵심 영향 파일 검토 결과를 반영했습니다. ({', '.join(top_files[:2])})")
    return {
        "title": f"[{work_type}] {payload['today']} 작업 결과",
        "summary": source_insights[0] if source_insights else f"{work_type} 유형 작업에 대한 Jira 작업 결과 초안입니다.",
        "task_name": f"{payload['repository']} {work_type} 작업",
        "done_items": done_items,
        "subtask_results": subtask_results[:4] or ["정리된 하위 작업 결과가 없습니다."],
        "validation": ["커밋 이력 확인", "변경 파일 검토", "추가 검증 필요 여부 확인"],
        "issues": ["미커밋 변경이 남아 있으면 결과 정리에 추가 확인이 필요합니다."] if payload["uncommitted_count"] else ["즉시 보이는 로컬 미커밋 변경은 없습니다."],
        "links": [item.get("html_url", "") for item in payload.get("github", {}).get("commits", [])[:5] if item.get("html_url")] or [],
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
        return {
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
    return {
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
        "jira_plan": '{"title": str, "summary": str, "task_name": str, "task_goal": str, "scope": [str], "subtasks": [str], "validation": [str], "risks": [str]}',
        "jira_result": '{"title": str, "summary": str, "task_name": str, "done_items": [str], "subtask_results": [str], "validation": [str], "issues": [str], "links": [str]}',
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
        "- For jira_plan, structure the content as one parent task plus subtasks.\n"
        "- For jira_result, structure the content as one parent task result plus subtask results.\n"
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
        add_section("리스크", list(sections.get("risks") or []))
        add_section("다음 주 초점", list(sections.get("next_week") or []))
    else:
        add_section("월간 요약", list(sections.get("summary") or []))
        add_section("주요 하이라이트", list(sections.get("highlights") or []))
        add_section("변경 영역", list(sections.get("areas") or []))
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

    if doc_type == "jira_plan":
        add("Task", [f"Name: {sections.get('task_name', '-')}", f"Goal: {sections.get('task_goal', '-')}", *list(sections.get('scope') or [])])
        add("Subtasks", list(sections.get("subtasks") or []))
        add("Validation", list(sections.get("validation") or []))
        add("Risks", list(sections.get("risks") or []))
    else:
        add("Task", [f"Name: {sections.get('task_name', '-')}", *list(sections.get("done_items") or [])])
        add("Subtask Results", list(sections.get("subtask_results") or []))
        add("Validation", list(sections.get("validation") or []))
        add("Issues", list(sections.get("issues") or []))
        add("Links", list(sections.get("links") or []))

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
        lines.append('    C --> E["Jira Task / Subtasks"]')
        lines.extend(["```", ""])

        lines.extend(["## Change Impact", "", "```mermaid", "flowchart TD"])
        lines.append('    A["Changed Source Files"] --> B["Design / Structure Review"]')
        lines.append('    A --> C["Validation / Risk Review"]')
        lines.append('    B --> D["Jira Plan"]')
        lines.append('    C --> D["Jira Plan"]')
        lines.extend(["```", ""])
    return "\n".join(lines)


def generate_document(report_type: str, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    try:
        sections = ask_gemini_for_sections(report_type, payload)
        mode = "gemini"
        sections_error = ""
    except Exception as exc:
        sections = build_fallback_jira_doc(report_type, payload) if report_type.startswith("jira_") else build_fallback_sections(report_type, payload)
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

    if report_type.startswith("jira_"):
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

    def list_block(title_text: str, items: list[str]) -> str:
        body = "".join(f"<li>{escape(str(item))}</li>" for item in items) or "<li>No items</li>"
        return f"""
<section class="detail-panel">
  <h3>{escape(title_text)}</h3>
  <ul>{body}</ul>
</section>
"""

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
        return """
<section class="detail-panel image-panel">
  <h3>Visual Evidence Slots</h3>
  <div class="image-slots">
    <div class="image-slot"><span>Screen / Before</span><small>Paste screenshot or exported chart here for reporting.</small></div>
    <div class="image-slot"><span>Screen / After</span><small>Use for UI diff, diagram image, or stakeholder-ready capture.</small></div>
    <div class="image-slot"><span>Architecture / Flow</span><small>Use for sequence, component, or data-flow visual.</small></div>
  </div>
</section>
"""

    def build_jira_plan_extras() -> str:
        subtasks = list(sections.get("subtasks") or [])
        validations = list(sections.get("validation") or [])
        rows = []
        timeline = []
        owners = ["Backend", "Frontend", "QA", "Docs", "DevOps", "Owner"]
        for idx, item in enumerate(subtasks[:6]):
            priority = "High" if idx == 0 else ("Medium" if idx < 3 else "Low")
            owner = owners[idx % len(owners)]
            dod = validations[idx % len(validations)] if validations else "Validation needed"
            timeline.append(
                f"""
<div class="timeline-step">
  <div class="timeline-marker">{idx + 1}</div>
  <div class="timeline-copy">
    <strong>{escape(str(item))}</strong>
    <span>{priority} priority · {escape(str(dod))}</span>
  </div>
</div>
"""
            )
            rows.append(
                f"""
<tr>
  <td>{idx + 1}</td>
  <td>{escape(str(item))}</td>
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
  <h3>Jira Execution Timeline</h3>
  <div class="timeline">{"".join(timeline)}</div>
</section>
<section class="detail-panel">
  <h3>Assignment Table</h3>
  <div class="table-wrap">
    <table>
      <thead>
        <tr><th>#</th><th>Subtask</th><th>Priority</th><th>Owner</th><th>Definition of Done</th></tr>
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
    elif report_type == "jira_plan":
        section_blocks.extend(
            [
                list_block("Task", [f"Name: {sections.get('task_name', '-')}", f"Goal: {sections.get('task_goal', '-')}", *list(sections.get("scope") or [])]),
                list_block("Subtasks", list(sections.get("subtasks") or [])),
                list_block("Validation", list(sections.get("validation") or [])),
                list_block("Risks", list(sections.get("risks") or [])),
            ]
        )
    elif report_type == "jira_result":
        section_blocks.extend(
            [
                list_block("Task Result", [f"Name: {sections.get('task_name', '-')}", *list(sections.get("done_items") or [])]),
                list_block("Subtask Results", list(sections.get("subtask_results") or [])),
                list_block("Validation", list(sections.get("validation") or [])),
                list_block("Issues", list(sections.get("issues") or [])),
                list_block("Links", list(sections.get("links") or [])),
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
    if report_type == "jira_plan":
        extra_html += build_jira_plan_extras()

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg:#f5efe4; --paper:#fffdf9; --ink:#17212b; --muted:#5f6b76; --line:#ddd2c1;
      --accent:#0f4c5c; --accent2:#d17a22; --hero-a:#12343b; --hero-b:#2c6e63;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:"Segoe UI","Noto Sans KR",sans-serif; color:var(--ink); background:
      radial-gradient(circle at top right, rgba(209,122,34,.14), transparent 24%),
      linear-gradient(180deg,#f8f3e9 0%, var(--bg) 100%); }}
    .wrap {{ max-width:1320px; margin:0 auto; padding:32px 28px 48px; }}
    .hero {{ background:linear-gradient(135deg,var(--hero-a),var(--hero-b)); color:#fff; border-radius:28px; padding:32px; box-shadow:0 24px 60px rgba(18,52,59,.24); margin-bottom:22px; }}
    .hero h1 {{ margin:0 0 10px; font-size:38px; line-height:1.08; }}
    .hero p {{ margin:0; max-width:820px; opacity:.92; }}
    .meta {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; margin:20px 0 0; }}
    .meta div {{ background:rgba(255,255,255,.1); border:1px solid rgba(255,255,255,.14); border-radius:18px; padding:14px; }}
    .meta span {{ display:block; font-size:11px; text-transform:uppercase; letter-spacing:.08em; opacity:.8; margin-bottom:8px; }}
    .meta strong {{ font-size:22px; }}
    .actions {{ display:flex; gap:12px; flex-wrap:wrap; margin:0 0 18px; }}
    .actions a {{ text-decoration:none; color:var(--accent); background:#f5ede1; border:1px solid var(--line); padding:10px 14px; border-radius:999px; font-weight:700; }}
    .facet-groups {{ display:grid; gap:14px; margin:0 0 18px; }}
    .facet-group h3 {{ margin:0 0 8px; font-size:13px; letter-spacing:.08em; text-transform:uppercase; color:#6b7280; }}
    .facet-strip {{ display:flex; gap:10px; flex-wrap:wrap; }}
    .facet {{ display:inline-flex; flex-direction:column; gap:4px; min-width:180px; padding:12px 14px; border-radius:18px; border:1px solid var(--line); background:linear-gradient(180deg,#fff8ee,#f8eddc); }}
    .facet-primary {{ background:linear-gradient(180deg,#fff1dd,#ffe3b8); border-color:#f59e0b; }}
    .facet-support {{ background:linear-gradient(180deg,#fffaf2,#f5efe5); border-color:#d6c6aa; }}
    .facet strong {{ font-size:13px; text-transform:uppercase; color:#7c2d12; }}
    .facet em {{ font-style:normal; color:var(--muted); font-size:12px; line-height:1.45; }}
    .grid {{ display:grid; grid-template-columns:1.2fr .8fr; gap:16px; margin-bottom:16px; }}
    .stack {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
    .chart-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }}
    .content-stack {{ display:grid; gap:16px; margin-bottom:16px; }}
    .detail-panel {{ background:linear-gradient(180deg,rgba(255,255,255,.94),rgba(255,250,241,.98)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 42px rgba(23,33,43,.08); }}
    .detail-panel h3 {{ margin:0 0 12px; font-size:20px; }}
    .detail-panel ul {{ margin:0; padding-left:20px; }}
    .detail-panel li {{ margin-bottom:8px; line-height:1.5; }}
    .detail-panel li span {{ color:var(--muted); margin-left:8px; font-size:12px; }}
    .chart-wrap {{ background:linear-gradient(180deg,#fffdfa,#f9f3e9); border:1px solid var(--line); border-radius:24px; padding:22px; }}
    .chart-large {{ min-height: 320px; }}
    .chart-wrap h3 {{ margin:0 0 12px; font-size:20px; }}
    .image-panel {{ grid-column:1 / -1; }}
    .image-slots {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }}
    .image-slot {{ min-height:150px; border:2px dashed #d8ccb7; border-radius:20px; padding:18px; background:linear-gradient(180deg,#fffdfa,#f8f1e4); display:flex; flex-direction:column; justify-content:flex-end; }}
    .image-slot span {{ display:block; font-size:16px; font-weight:700; margin-bottom:8px; }}
    .image-slot small {{ color:var(--muted); line-height:1.5; }}
    .timeline {{ display:grid; gap:14px; }}
    .timeline-step {{ display:grid; grid-template-columns:auto 1fr; gap:14px; align-items:start; }}
    .timeline-marker {{ width:34px; height:34px; border-radius:50%; background:#12343b; color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; }}
    .timeline-copy strong {{ display:block; margin-bottom:4px; }}
    .timeline-copy span {{ color:var(--muted); font-size:12px; }}
    .area-section {{ margin-top:16px; }}
    .area-stack {{ display:grid; gap:14px; }}
    .area-card {{ border:1px solid var(--line); border-radius:20px; padding:18px; background:linear-gradient(180deg,#fffdfa,#f8f1e4); }}
    .area-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px; }}
    .area-head h3 {{ margin:0; font-size:20px; }}
    .area-head span {{ color:var(--muted); font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.08em; }}
    .area-badges {{ display:flex; gap:8px; flex-wrap:wrap; margin:0 0 12px; }}
    .mini-badge {{ display:inline-flex; align-items:center; padding:7px 10px; border-radius:999px; font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; border:1px solid var(--line); background:#fff; }}
    .priority-high, .impact-high, .risk-high {{ background:#fee2e2; color:#991b1b; border-color:#fecaca; }}
    .priority-medium, .impact-medium, .risk-medium {{ background:#fef3c7; color:#92400e; border-color:#fde68a; }}
    .priority-low, .impact-low, .risk-low {{ background:#dcfce7; color:#166534; border-color:#bbf7d0; }}
    .owner {{ background:#e0f2fe; color:#075985; border-color:#bae6fd; }}
    .status {{ background:#ede9fe; color:#5b21b6; border-color:#ddd6fe; }}
    .area-grid {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:14px; }}
    .area-grid h4 {{ margin:0 0 8px; font-size:14px; color:#7c2d12; text-transform:uppercase; letter-spacing:.06em; }}
    .area-grid ul {{ margin:0; padding-left:18px; }}
    .area-grid li {{ margin-bottom:6px; line-height:1.45; }}
    .table-wrap {{ overflow:auto; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ padding:12px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }}
    code {{ background:#efe6d8; padding:2px 7px; border-radius:8px; }}
    @media print {{
      body {{ background:#fff; }}
      .wrap {{ max-width:none; padding:0; }}
      .hero, .detail-panel, .chart-wrap {{ box-shadow:none; break-inside:avoid; }}
      .actions a {{ border-color:#bbb; }}
    }}
    @media (max-width:960px) {{
      .meta {{ grid-template-columns:1fr 1fr; }}
      .grid {{ grid-template-columns:1fr; }}
      .stack {{ grid-template-columns:1fr; }}
      .chart-grid {{ grid-template-columns:1fr; }}
      .image-slots {{ grid-template-columns:1fr; }}
      .area-grid {{ grid-template-columns:1fr; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
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
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Area chart">']
    y = 12
    colors = ["#264653", "#2a9d8f", "#e9c46a", "#f4a261", "#e76f51", "#6d597a"]
    for idx, item in enumerate(areas[:6]):
        label = escape(str(item.get("area", "")))
        count = int(item.get("count", 0))
        bar_width = int((count / max_count) * 620)
        color = colors[idx % len(colors)]
        parts.append(f'<text x="0" y="{y + 22}" font-size="16" fill="#1f2937">{label}</text>')
        parts.append(f'<rect x="240" y="{y}" rx="10" ry="10" width="{bar_width}" height="{bar_height}" fill="{color}"></rect>')
        parts.append(f'<text x="{250 + bar_width}" y="{y + 22}" font-size="14" fill="#111827">{count}</text>')
        y += bar_height + gap
    parts.append("</svg>")
    return "".join(parts)


def svg_flow(areas: list[dict[str, Any]]) -> str:
    primary = escape(str(areas[0]["area"])) if areas else "Core Area"
    secondary = escape(str(areas[1]["area"])) if len(areas) > 1 else "Support Area"
    return f"""
<svg viewBox="0 0 1100 240" class="flow" role="img" aria-label="Change flow">
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto">
      <path d="M0,0 L0,6 L9,3 z" fill="#264653"></path>
    </marker>
  </defs>
  <rect x="20" y="90" width="210" height="60" rx="16" fill="#264653"></rect>
  <text x="125" y="126" text-anchor="middle" fill="#fff" font-size="18">Git / GitHub Activity</text>
  <rect x="320" y="24" width="220" height="60" rx="16" fill="#2a9d8f"></rect>
  <text x="430" y="60" text-anchor="middle" fill="#fff" font-size="18">{primary}</text>
  <rect x="320" y="146" width="220" height="60" rx="16" fill="#e9c46a"></rect>
  <text x="430" y="182" text-anchor="middle" fill="#1f2937" font-size="18">{secondary}</text>
  <rect x="650" y="90" width="190" height="60" rx="16" fill="#f4a261"></rect>
  <text x="745" y="126" text-anchor="middle" fill="#1f2937" font-size="18">Analysis / AI</text>
  <rect x="910" y="90" width="170" height="60" rx="16" fill="#e76f51"></rect>
  <text x="995" y="126" text-anchor="middle" fill="#fff" font-size="18">Reports</text>
  <line x1="230" y1="120" x2="320" y2="54" stroke="#264653" stroke-width="4" marker-end="url(#arrow)"></line>
  <line x1="230" y1="120" x2="320" y2="176" stroke="#264653" stroke-width="4" marker-end="url(#arrow)"></line>
  <line x1="540" y1="54" x2="650" y2="120" stroke="#264653" stroke-width="4" marker-end="url(#arrow)"></line>
  <line x1="540" y1="176" x2="650" y2="120" stroke="#264653" stroke-width="4" marker-end="url(#arrow)"></line>
  <line x1="840" y1="120" x2="910" y2="120" stroke="#264653" stroke-width="4" marker-end="url(#arrow)"></line>
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
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Structure map">']
    parts.append('<rect x="20" y="20" width="180" height="48" rx="14" fill="#12343b"></rect>')
    parts.append('<text x="110" y="50" text-anchor="middle" fill="#fff" font-size="18">Repository</text>')
    y = 56
    for idx, parts_list in enumerate(roots, start=1):
        area = escape(parts_list[0] if parts_list else "root")
        leaf = escape("/".join(parts_list[1:]) if len(parts_list) > 1 else area)
        box_y = y + (idx - 1) * 36
        parts.append(f'<line x1="200" y1="44" x2="320" y2="{box_y}" stroke="#264653" stroke-width="2.5"></line>')
        parts.append(f'<rect x="320" y="{box_y-18}" width="190" height="30" rx="10" fill="#2a9d8f"></rect>')
        parts.append(f'<text x="415" y="{box_y+2}" text-anchor="middle" fill="#fff" font-size="14">{area}</text>')
        parts.append(f'<line x1="510" y1="{box_y-3}" x2="600" y2="{box_y-3}" stroke="#264653" stroke-width="2.5"></line>')
        parts.append(f'<rect x="600" y="{box_y-18}" width="380" height="30" rx="10" fill="#f4a261"></rect>')
        parts.append(f'<text x="790" y="{box_y+2}" text-anchor="middle" fill="#1f2937" font-size="13">{leaf}</text>')
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
    parts = [f'<svg viewBox="0 0 {width} {height}" class="flow" role="img" aria-label="Action roadmap">']
    x = 30
    colors = ["#264653", "#2a9d8f", "#e9c46a", "#f4a261"]
    for idx, (title, subtitle) in enumerate(steps):
        color = colors[idx % len(colors)]
        parts.append(f'<rect x="{x}" y="56" width="210" height="74" rx="18" fill="{color}"></rect>')
        parts.append(f'<text x="{x+105}" y="87" text-anchor="middle" fill="#fff" font-size="18">{escape(title)}</text>')
        parts.append(f'<text x="{x+105}" y="110" text-anchor="middle" fill="#fff" font-size="12">{escape(subtitle)}</text>')
        if idx < len(steps) - 1:
            parts.append(f'<line x1="{x+210}" y1="93" x2="{x+250}" y2="93" stroke="#264653" stroke-width="4"></line>')
            parts.append(f'<polygon points="{x+250},93 {x+238},86 {x+238},100" fill="#264653"></polygon>')
        x += 250
    parts.append('</svg>')
    return ''.join(parts)


def svg_architecture_delta(areas: list[dict[str, Any]], top_files: list[dict[str, Any]]) -> str:
    primary = escape(str(areas[0]["area"])) if areas else "Primary Area"
    secondary = escape(str(areas[1]["area"])) if len(areas) > 1 else "Secondary Area"
    tertiary = escape(str(areas[2]["area"])) if len(areas) > 2 else "Output Area"
    lead_file = escape(str((top_files[0] or {}).get("path", "core/module.py"))) if top_files else "core/module.py"
    support_file = escape(str((top_files[1] or {}).get("path", "support/module.py"))) if len(top_files) > 1 else "support/module.py"
    return f"""
<svg viewBox="0 0 1100 320" class="flow" role="img" aria-label="Architecture delta">
  <defs>
    <marker id="arch-arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto">
      <path d="M0,0 L0,6 L9,3 z" fill="#264653"></path>
    </marker>
  </defs>
  <rect x="30" y="120" width="180" height="72" rx="18" fill="#12343b"></rect>
  <text x="120" y="150" text-anchor="middle" fill="#fff" font-size="18">Repository</text>
  <text x="120" y="174" text-anchor="middle" fill="#d8f3dc" font-size="13">Change Set</text>
  <rect x="290" y="28" width="250" height="80" rx="18" fill="#2a9d8f"></rect>
  <text x="415" y="60" text-anchor="middle" fill="#fff" font-size="20">{primary}</text>
  <text x="415" y="84" text-anchor="middle" fill="#e6fffb" font-size="12">{lead_file}</text>
  <rect x="290" y="120" width="250" height="80" rx="18" fill="#e9c46a"></rect>
  <text x="415" y="152" text-anchor="middle" fill="#1f2937" font-size="20">{secondary}</text>
  <text x="415" y="176" text-anchor="middle" fill="#4b5563" font-size="12">{support_file}</text>
  <rect x="290" y="212" width="250" height="80" rx="18" fill="#f4a261"></rect>
  <text x="415" y="244" text-anchor="middle" fill="#1f2937" font-size="20">{tertiary}</text>
  <text x="415" y="268" text-anchor="middle" fill="#7c2d12" font-size="12">Derived output / docs / UI</text>
  <rect x="640" y="74" width="190" height="78" rx="18" fill="#6d597a"></rect>
  <text x="735" y="106" text-anchor="middle" fill="#fff" font-size="18">Structure</text>
  <text x="735" y="130" text-anchor="middle" fill="#f3e8ff" font-size="13">Module boundary</text>
  <rect x="640" y="178" width="190" height="78" rx="18" fill="#e76f51"></rect>
  <text x="735" y="210" text-anchor="middle" fill="#fff" font-size="18">Validation</text>
  <text x="735" y="234" text-anchor="middle" fill="#fee2e2" font-size="13">Risk / quality check</text>
  <rect x="900" y="120" width="160" height="72" rx="18" fill="#264653"></rect>
  <text x="980" y="150" text-anchor="middle" fill="#fff" font-size="18">Report Pack</text>
  <text x="980" y="174" text-anchor="middle" fill="#dbeafe" font-size="12">Daily / Weekly / Jira</text>
  <line x1="210" y1="156" x2="290" y2="68" stroke="#264653" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="210" y1="156" x2="290" y2="160" stroke="#264653" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="210" y1="156" x2="290" y2="252" stroke="#264653" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="540" y1="68" x2="640" y2="113" stroke="#264653" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="540" y1="160" x2="640" y2="113" stroke="#264653" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="540" y1="252" x2="640" y2="217" stroke="#264653" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="830" y1="113" x2="900" y2="156" stroke="#264653" stroke-width="4" marker-end="url(#arch-arrow)"></line>
  <line x1="830" y1="217" x2="900" y2="156" stroke="#264653" stroke-width="4" marker-end="url(#arch-arrow)"></line>
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
    parts = [f'<svg viewBox="0 0 {width} {height}" class="flow" role="img" aria-label="Change impact map">']
    parts.append('<defs><marker id="impact-arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#7c2d12"></path></marker></defs>')
    parts.append('<rect x="30" y="126" width="190" height="70" rx="18" fill="#7c2d12"></rect>')
    parts.append('<text x="125" y="156" text-anchor="middle" fill="#fff" font-size="18">Changed Sources</text>')
    parts.append('<text x="125" y="178" text-anchor="middle" fill="#ffedd5" font-size="12">Evidence from git diff</text>')
    x_positions = [320, 320, 320, 630, 630]
    y_positions = [24, 116, 208, 70, 186]
    colors = ["#2a9d8f", "#e9c46a", "#f4a261", "#264653", "#6d597a"]
    for idx, node in enumerate(nodes[:5]):
        title, label, meta = node
        x = x_positions[idx]
        y = y_positions[idx]
        fill = colors[idx % len(colors)]
        text_fill = "#fff" if idx in (0, 3, 4) else "#1f2937"
        meta_fill = "#e5e7eb" if idx in (0, 3, 4) else "#4b5563"
        parts.append(f'<rect x="{x}" y="{y}" width="230" height="74" rx="18" fill="{fill}"></rect>')
        parts.append(f'<text x="{x+115}" y="{y+28}" text-anchor="middle" fill="{text_fill}" font-size="16">{escape(title)}</text>')
        parts.append(f'<text x="{x+115}" y="{y+48}" text-anchor="middle" fill="{text_fill}" font-size="13">{escape(label[:34])}</text>')
        parts.append(f'<text x="{x+115}" y="{y+64}" text-anchor="middle" fill="{meta_fill}" font-size="11">{escape(meta)}</text>')
    parts.append('<rect x="920" y="126" width="150" height="70" rx="18" fill="#12343b"></rect>')
    parts.append('<text x="995" y="156" text-anchor="middle" fill="#fff" font-size="18">Impacted Output</text>')
    parts.append('<text x="995" y="178" text-anchor="middle" fill="#dbeafe" font-size="12">UI / API / Docs / Jira</text>')
    for target_y in (61, 153, 245):
        parts.append(f'<line x1="220" y1="161" x2="320" y2="{target_y}" stroke="#7c2d12" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append('<line x1="550" y1="61" x2="630" y2="107" stroke="#7c2d12" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append('<line x1="550" y1="153" x2="630" y2="107" stroke="#7c2d12" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append('<line x1="550" y1="245" x2="630" y2="223" stroke="#7c2d12" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append('<line x1="860" y1="107" x2="920" y2="161" stroke="#7c2d12" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append('<line x1="860" y1="223" x2="920" y2="161" stroke="#7c2d12" stroke-width="4" marker-end="url(#impact-arrow)"></line>')
    parts.append('</svg>')
    return ''.join(parts)

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


def render_html_dashboard(today: date, cards: list[dict[str, Any]]) -> str:
    total_commits = sum(int(card["payload"].get("commit_count", 0)) for card in cards)
    total_files = sum(int(card["payload"].get("changed_file_count", 0)) for card in cards)
    total_added = sum(int((card["payload"].get("diff_summary") or {}).get("total_added", 0)) for card in cards)
    total_deleted = sum(int((card["payload"].get("diff_summary") or {}).get("total_deleted", 0)) for card in cards)
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
            "jira_plan": "tone-jira",
            "jira_result": "tone-jira2",
            "weekly": "tone-weekly",
            "monthly": "tone-monthly",
        }.get(card["report_type"], "tone-default")
        is_jira_plan = card["report_type"] == "jira_plan"
        is_jira_result = card["report_type"] == "jira_result"
        board_html = ""
        if is_jira_plan:
            result_sections = {}
            for other in cards:
                if other["report_type"] == "jira_result":
                    result_sections = other.get("sections") or {}
                    break
            board_html = html_task_board(sections, result_sections)
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
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Startup Reports {today.isoformat()}</title>
  <style>
    :root {{
      --bg: #f4efe4;
      --paper: #fffdf9;
      --ink: #17212b;
      --muted: #5f6b76;
      --accent: #0f4c5c;
      --accent-2: #d17a22;
      --line: #ddd2c1;
      --hero-a: #12343b;
      --hero-b: #2c6e63;
      --glow: rgba(209, 122, 34, 0.18);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "Segoe UI", "Noto Sans KR", sans-serif; background:
      radial-gradient(circle at top right, rgba(209,122,34,0.14), transparent 24%),
      radial-gradient(circle at top left, rgba(44,110,99,0.22), transparent 28%),
      linear-gradient(180deg, #f8f3e9 0%, var(--bg) 100%); color: var(--ink); }}
    .wrap {{ max-width: 1380px; margin: 0 auto; padding: 32px 28px 48px; }}
    .hero {{ position: relative; overflow: hidden; margin-bottom: 26px; padding: 34px; background: linear-gradient(135deg, var(--hero-a), var(--hero-b)); color: #fff; border-radius: 30px; box-shadow: 0 24px 60px rgba(18,52,59,0.24); }}
    .hero::after {{ content: ""; position: absolute; inset: auto -60px -80px auto; width: 260px; height: 260px; border-radius: 50%; background: radial-gradient(circle, rgba(255,255,255,0.18), transparent 60%); }}
    .eyebrow {{ display: inline-block; font-size: 12px; letter-spacing: 0.18em; text-transform: uppercase; padding: 8px 12px; border: 1px solid rgba(255,255,255,0.22); border-radius: 999px; margin-bottom: 14px; }}
    .hero h1 {{ margin: 0 0 10px; font-size: 40px; line-height: 1.05; }}
    .hero p {{ margin: 0; opacity: 0.92; max-width: 760px; font-size: 15px; }}
    .hero-grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 18px; align-items: end; }}
    .hero-kpis {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .hero-kpi {{ background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.12); border-radius: 18px; padding: 16px; backdrop-filter: blur(8px); }}
    .hero-kpi span {{ display: block; opacity: 0.8; font-size: 12px; margin-bottom: 8px; }}
    .hero-kpi strong {{ font-size: 28px; }}
    .card {{ position: relative; background: linear-gradient(180deg, rgba(255,255,255,0.94), rgba(255,250,241,0.98)); border: 1px solid var(--line); border-radius: 28px; padding: 24px; margin-bottom: 22px; box-shadow: 0 18px 42px rgba(23,33,43,0.08); }}
    .card::before {{ content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 6px; border-radius: 28px 0 0 28px; background: var(--card-accent, var(--accent)); }}
    .tone-daily {{ --card-accent: #0f4c5c; }}
    .tone-plan {{ --card-accent: #d17a22; }}
    .tone-jira {{ --card-accent: #6c5ce7; }}
    .tone-jira2 {{ --card-accent: #b56576; }}
    .tone-weekly {{ --card-accent: #2a9d8f; }}
    .tone-monthly {{ --card-accent: #8f5f3f; }}
      .card-head {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 18px; }}
    .card h2 {{ margin: 0 0 8px; font-size: 28px; line-height: 1.1; }}
    .meta {{ margin: 0; color: var(--muted); font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase; }}
    .file-link {{ color: var(--accent); text-decoration: none; font-weight: 700; padding: 10px 14px; border-radius: 999px; background: #f5ede1; border: 1px solid var(--line); white-space: nowrap; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 12px; margin-bottom: 18px; }}
    .stats div {{ background: linear-gradient(180deg, #fff9ef, #f7f0e5); border-radius: 18px; padding: 16px; border: 1px solid var(--line); box-shadow: inset 0 1px 0 rgba(255,255,255,0.6); }}
    .stats span {{ display: block; color: var(--muted); font-size: 11px; margin-bottom: 8px; letter-spacing: 0.08em; text-transform: uppercase; }}
    .stats strong {{ font-size: 30px; line-height: 1; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }}
    .facet-group-inline {{ display:grid; gap:8px; margin: 0 0 18px; }}
    .facet-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin: 0; }}
    .facet-badge {{ display:inline-flex; flex-direction:column; gap:4px; padding:12px 14px; border-radius:18px; background:linear-gradient(180deg,#fff8ee,#f8eddc); border:1px solid var(--line); min-width:180px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.7); }}
    .facet-badge-primary {{ background:linear-gradient(180deg,#fff1dd,#ffe3b8); border-color:#f59e0b; }}
    .facet-badge-support {{ background:linear-gradient(180deg,#fffaf2,#f5efe5); border-color:#d6c6aa; }}
    .facet-badge strong {{ font-size:13px; letter-spacing:.04em; text-transform:uppercase; color:#7c2d12; }}
    .facet-badge em {{ font-style:normal; color:var(--muted); font-size:12px; line-height:1.45; }}
    .task-board {{ display:grid; grid-template-columns: 1.1fr 1fr 1fr; gap:14px; margin: 0 0 18px; }}
    .task-box {{ position:relative; border-radius:22px; padding:18px; border:1px solid var(--line); background:linear-gradient(180deg,#fffdfa,#f7f1e5); box-shadow: inset 0 1px 0 rgba(255,255,255,0.72); }}
    .task-box.parent {{ background:linear-gradient(180deg,#f3fbfb,#edf7f5); }}
    .task-box.child {{ background:linear-gradient(180deg,#fff9ef,#fbf2de); }}
    .task-box.result {{ background:linear-gradient(180deg,#fff4f1,#faece8); }}
    .task-box h4 {{ margin:0 0 10px; font-size:20px; line-height:1.2; }}
    .task-box p {{ margin:0; color:var(--muted); line-height:1.45; }}
    .task-box ul {{ margin:0; padding-left:20px; }}
    .task-box li {{ margin-bottom:8px; line-height:1.45; }}
    .task-label {{ display:inline-block; margin-bottom:10px; font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); background:rgba(255,255,255,.6); border:1px solid var(--line); border-radius:999px; padding:6px 10px; }}
    .subtask-row {{ display:grid; grid-template-columns:auto 1fr auto; gap:12px; align-items:start; }}
    .subtask-copy strong {{ display:block; margin-bottom:4px; font-size:14px; }}
    .subtask-copy span {{ display:block; color:var(--muted); font-size:12px; }}
    .check {{ width:26px; height:26px; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; border:1px solid var(--line); background:#fff; color:#9ca3af; font-weight:700; }}
    .check.done {{ background:#1f7a5c; border-color:#1f7a5c; color:#fff; }}
    .state {{ display:inline-flex; align-items:center; border-radius:999px; padding:6px 10px; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.08em; }}
    .state.done {{ background:#d8f3dc; color:#1b4332; }}
    .state.planned {{ background:#fef3c7; color:#92400e; }}
    .panel {{ border: 1px solid var(--line); border-radius: 22px; padding: 18px; background: linear-gradient(180deg, #fffdfa, #f9f3e9); overflow: auto; box-shadow: inset 0 1px 0 rgba(255,255,255,0.75); }}
    .panel h3 {{ margin-top: 0; margin-bottom: 12px; font-size: 18px; letter-spacing: 0.01em; }}
    .mini-meta {{ margin: -4px 0 10px; color: var(--muted); font-size: 12px; }}
    .panel ul {{ margin: 0; padding-left: 20px; }}
    .panel li {{ margin-bottom: 8px; line-height: 1.45; }}
    .chart, .flow {{ width: 100%; height: auto; }}
    code {{ background: #efe6d8; padding: 2px 7px; border-radius: 8px; }}
    @media (max-width: 900px) {{
      .hero-grid {{ grid-template-columns: 1fr; }}
      .task-board {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: 1fr 1fr; }}
      .card-head {{ flex-direction: column; align-items: flex-start; }}
    }}
  </style>
</head>
  <body>
  <div class="wrap">
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
    {"".join(card_html)}
  </div>
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

    output_specs = [
        ("daily", output_root / "reports" / "daily_brief" / f"{today.isoformat()}-daily-report.md"),
        ("plan", output_root / "reports" / "plans" / f"{today.isoformat()}-next-plan.md"),
        ("jira_plan", output_root / "reports" / "jira" / f"{today.isoformat()}-jira-plan.md"),
        ("jira_result", output_root / "reports" / "jira" / f"{today.isoformat()}-jira-result.md"),
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

    dashboard_path = output_root / "reports" / "dashboard" / f"{today.isoformat()}-startup-dashboard.html"
    write_text(dashboard_path, render_html_dashboard(today, dashboard_cards))
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
