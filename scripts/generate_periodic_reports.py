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
        counts[normalized.split("/", 1)[0]] += 1
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


def infer_work_type(changed_files: list[str], commits: list[Commit]) -> str:
    text = " ".join(commit.subject.lower() for commit in commits)
    if any(word in text for word in ("fix", "bug", "error", "hotfix")):
        return "bugfix"
    if any(word in text for word in ("refactor", "cleanup")):
        return "refactor"
    if any(word in text for word in ("test", "qa")):
        return "test"
    if any(path.endswith(".md") or path.startswith("docs/") or path.startswith("project_docs/") for path in changed_files):
        return "documentation"
    if any(path.startswith("frontend/") or path.startswith("backend/") for path in changed_files):
        return "feature"
    return "maintenance"


def infer_change_facets(changed_files: list[str], commits: list[Commit], diff_summary: dict[str, Any]) -> list[dict[str, str]]:
    text = " ".join(commit.subject.lower() for commit in commits)
    normalized_paths = [path.replace("\\", "/").lower() for path in changed_files]
    top_files = [str(item.get("path", "")).replace("\\", "/").lower() for item in (diff_summary.get("top_files") or [])]
    all_paths = normalized_paths + top_files

    facets: list[dict[str, str]] = []

    def add(name: str, reason: str) -> None:
        if any(item["name"] == name for item in facets):
            return
        facets.append({"name": name, "reason": reason})

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
    return facets[:5]


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
) -> dict[str, Any]:
    diff_summary = summarize_diff_stats(
        get_diff_numstat(repo_root, branch, window.start, window.end)
    )
    change_facets = infer_change_facets(changed_files, commits, diff_summary)
    return {
        "today": today.isoformat(),
        "report_type": report_type,
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        "repository": repo_root.name,
        "branch": branch,
        "remote_url": remote_url,
        "upstream": upstream or "",
        "sync_status": {"ahead": sync_state[0] if sync_state else 0, "behind": sync_state[1] if sync_state else 0},
        "commit_count": len(commits),
        "changed_file_count": len(changed_files),
        "uncommitted_count": len(uncommitted),
        "work_type": infer_work_type(changed_files, commits),
        "change_facets": change_facets,
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
    work_type = payload["work_type"]
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
    work_type = payload["work_type"]
    if doc_type == "jira_plan":
        return {
            "title": f"[{work_type}] {payload['today']} 작업",
            "summary": f"{work_type} 유형 작업에 대한 Jira 상위 작업 초안입니다.",
            "task_name": f"{payload['repository']} {work_type} 작업",
            "task_goal": "최근 변경 이력 기반으로 후속 작업과 검증 범위를 정리합니다.",
            "scope": [f"{item['area']} 영역 후속 작업" for item in areas[:4]] or ["후속 작업 범위 재정의"],
            "subtasks": [f"{entry['subject']} 관련 검증 및 마무리" for entry in commits[:4]] or ["하위작업 정의 필요"],
            "validation": ["기능 검증", "관련 테스트 확인", "문서 반영 확인"],
            "risks": ["자동 분류 결과이므로 실제 Jira 이슈 타입과 비교가 필요합니다."],
        }
    return {
        "title": f"[{work_type}] {payload['today']} 작업 결과",
        "summary": f"{work_type} 유형 작업에 대한 Jira 작업 결과 초안입니다.",
        "task_name": f"{payload['repository']} {work_type} 작업",
        "done_items": [entry["subject"] for entry in commits[:5]] or ["완료 커밋 없음"],
        "subtask_results": [f"{item['area']} {item['count']}개 파일 반영" for item in areas[:4]] or ["하위작업 결과 정리 필요"],
        "validation": ["커밋 이력 확인", "변경 파일 검토", "추가 검증 필요 여부 확인"],
        "issues": ["미커밋 변경이 남아 있으면 결과 정리가 추가로 필요합니다."] if payload["uncommitted_count"] else ["즉시 보이는 잔여 로컬 변경은 없습니다."],
        "links": [item.get("html_url", "") for item in payload.get("github", {}).get("commits", [])[:5] if item.get("html_url")] or [],
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


def render_report_markdown(report_type: str, sections: dict[str, Any], payload: dict[str, Any], mode: str) -> str:
    lines = [str(sections.get("title") or report_type.title()), "", "## 기준 정보", ""]
    lines.extend(
        [
            f"- 저장소: `{payload['repository']}`",
            f"- 브랜치: `{payload['branch']}`",
            f"- 원격: {payload['remote_url']}",
            f"- 집계 구간: `{payload['window_start']}` ~ `{payload['window_end']}`",
            f"- 작업 유형: `{payload['work_type']}`",
            f"- 커밋 수: `{payload['commit_count']}`",
            f"- 변경 파일 수: `{payload['changed_file_count']}`",
            f"- 미커밋 변경 수: `{payload['uncommitted_count']}`",
            f"- 생성 방식: `{mode}`",
        ]
    )
    github_meta = payload.get("github") or {}
    if github_meta.get("enabled"):
        lines.append(f"- GitHub API 저장소: `{github_meta.get('repo', '')}`")
        lines.append(f"- GitHub API 커밋 수: `{github_meta.get('commit_count', 0)}`")
    lines.append("")
    facets = payload.get("change_facets") or []
    if facets:
        lines.extend(["## 변경 성격", ""])
        for item in facets:
            lines.append(f"- `{item.get('name', '')}`: {item.get('reason', '')}")
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
        lines.extend(["## 변경 흐름도", "", "```mermaid", "flowchart LR"])
        first = areas[0]["area"]
        lines.append(f'    A["GitHub / Git Activity"] --> B["{first}"]')
        for index, item in enumerate(areas[1:4], start=1):
            prev = "B" if index == 1 else f"N{index-1}"
            current = f"N{index}"
            lines.append(f'    {prev} --> {current}["{item["area"]}"]')
        lines.append('    B --> Z["Report / Plan / Jira Docs"]')
        if len(areas) > 1:
            tail = f'N{min(len(areas)-1, 3)}'
            lines.append(f'    {tail} --> Z["Report / Plan / Jira Docs"]')
        lines.extend(["```", ""])
    return "\n".join(lines)


def render_jira_markdown(doc_type: str, sections: dict[str, Any], payload: dict[str, Any], mode: str) -> str:
    lines = [f"# {sections.get('title', doc_type)}", "", "## Meta", ""]
    lines.extend(
        [
            f"- Work Type: `{payload['work_type']}`",
            f"- Repo: `{payload['repository']}`",
            f"- Branch: `{payload['branch']}`",
            f"- Window: `{payload['window_start']}` ~ `{payload['window_end']}`",
            f"- Generation: `{mode}`",
        ]
    )
    lines.extend(["", "## Summary", "", str(sections.get("summary") or "-"), ""])
    facets = payload.get("change_facets") or []
    if facets:
        lines.extend(["## Change Facets", ""])
        for item in facets:
            lines.append(f"- {item.get('name', '')}: {item.get('reason', '')}")
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
        lines.extend(["## Flow", "", "```mermaid", "flowchart TD"])
        lines.append('    A["Input: Git / GitHub"] --> B["Analyze Change Evidence"]')
        lines.append(f'    B --> C["Primary Area: {areas[0]["area"]}"]')
        lines.append('    C --> D["Generate Jira Draft"]')
        lines.append('    D --> E["Manual Upload To Jira"]')
        lines.extend(["```", ""])
    return "\n".join(lines)


def generate_document(report_type: str, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    try:
        sections = ask_gemini_for_sections(report_type, payload)
        mode = "gemini"
    except Exception:
        sections = build_fallback_jira_doc(report_type, payload) if report_type.startswith("jira_") else build_fallback_sections(report_type, payload)
        mode = "fallback"

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
    changed_docs = payload.get("changed_docs") or []
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

    facet_html = "".join(
        f'<span class="facet"><strong>{escape(str(item.get("name", "")))}</strong><em>{escape(str(item.get("reason", "")))}</em></span>'
        for item in facets
    ) or '<span class="facet"><strong>유지보수</strong><em>추가 분류 근거가 부족해 기본 태그를 사용했습니다.</em></span>'
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
    .facet-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin:0 0 18px; }}
    .facet {{ display:inline-flex; flex-direction:column; gap:4px; min-width:180px; padding:12px 14px; border-radius:18px; border:1px solid var(--line); background:linear-gradient(180deg,#fff8ee,#f8eddc); }}
    .facet strong {{ font-size:13px; text-transform:uppercase; color:#7c2d12; }}
    .facet em {{ font-style:normal; color:var(--muted); font-size:12px; line-height:1.45; }}
    .grid {{ display:grid; grid-template-columns:1.2fr .8fr; gap:16px; margin-bottom:16px; }}
    .stack {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
    .detail-panel {{ background:linear-gradient(180deg,rgba(255,255,255,.94),rgba(255,250,241,.98)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 42px rgba(23,33,43,.08); }}
    .detail-panel h3 {{ margin:0 0 12px; font-size:20px; }}
    .detail-panel ul {{ margin:0; padding-left:20px; }}
    .detail-panel li {{ margin-bottom:8px; line-height:1.5; }}
    .detail-panel li span {{ color:var(--muted); margin-left:8px; font-size:12px; }}
    .chart-wrap {{ background:linear-gradient(180deg,#fffdfa,#f9f3e9); border:1px solid var(--line); border-radius:24px; padding:22px; }}
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
        <div><span>Work Type</span><strong>{escape(str(payload.get("work_type", "")))}</strong></div>
        <div><span>Commits</span><strong>{int(payload.get("commit_count", 0))}</strong></div>
        <div><span>Files</span><strong>{int(payload.get("changed_file_count", 0))}</strong></div>
        <div><span>Mode</span><strong>{escape(mode)}</strong></div>
      </div>
    </section>
    <div class="actions">
      <a href="{escape(markdown_path.as_uri())}">Source Markdown</a>
    </div>
    <div class="facet-strip">{facet_html}</div>
    <div class="grid">
      <div class="stack">
        {"".join(section_blocks)}
      </div>
      <div class="stack">
        <section class="chart-wrap">
          <h3>Top Change Areas</h3>
          {svg_area_bars(areas[:5])}
        </section>
        <section class="chart-wrap">
          <h3>Execution Flow</h3>
          {svg_flow(areas[:4])}
        </section>
      </div>
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
    width = 720
    bar_height = 24
    gap = 14
    max_count = max(int(item.get("count", 0)) for item in areas) or 1
    height = len(areas) * (bar_height + gap) + 24
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Area chart">']
    y = 10
    colors = ["#264653", "#2a9d8f", "#e9c46a", "#f4a261", "#e76f51", "#6d597a"]
    for idx, item in enumerate(areas[:6]):
        label = escape(str(item.get("area", "")))
        count = int(item.get("count", 0))
        bar_width = int((count / max_count) * 430)
        color = colors[idx % len(colors)]
        parts.append(f'<text x="0" y="{y + 16}" font-size="13" fill="#1f2937">{label}</text>')
        parts.append(f'<rect x="180" y="{y}" rx="8" ry="8" width="{bar_width}" height="{bar_height}" fill="{color}"></rect>')
        parts.append(f'<text x="{190 + bar_width}" y="{y + 16}" font-size="12" fill="#111827">{count}</text>')
        y += bar_height + gap
    parts.append("</svg>")
    return "".join(parts)


def svg_flow(areas: list[dict[str, Any]]) -> str:
    primary = escape(str(areas[0]["area"])) if areas else "Core Area"
    secondary = escape(str(areas[1]["area"])) if len(areas) > 1 else "Support Area"
    return f"""
<svg viewBox="0 0 860 180" class="flow" role="img" aria-label="Change flow">
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto">
      <path d="M0,0 L0,6 L9,3 z" fill="#264653"></path>
    </marker>
  </defs>
  <rect x="20" y="60" width="170" height="52" rx="14" fill="#264653"></rect>
  <text x="105" y="91" text-anchor="middle" fill="#fff" font-size="14">Git / GitHub Activity</text>
  <rect x="250" y="24" width="170" height="52" rx="14" fill="#2a9d8f"></rect>
  <text x="335" y="55" text-anchor="middle" fill="#fff" font-size="14">{primary}</text>
  <rect x="250" y="104" width="170" height="52" rx="14" fill="#e9c46a"></rect>
  <text x="335" y="135" text-anchor="middle" fill="#1f2937" font-size="14">{secondary}</text>
  <rect x="500" y="60" width="150" height="52" rx="14" fill="#f4a261"></rect>
  <text x="575" y="91" text-anchor="middle" fill="#1f2937" font-size="14">Analysis / AI</text>
  <rect x="700" y="60" width="140" height="52" rx="14" fill="#e76f51"></rect>
  <text x="770" y="91" text-anchor="middle" fill="#fff" font-size="14">Reports</text>
  <line x1="190" y1="86" x2="250" y2="50" stroke="#264653" stroke-width="3" marker-end="url(#arrow)"></line>
  <line x1="190" y1="86" x2="250" y2="130" stroke="#264653" stroke-width="3" marker-end="url(#arrow)"></line>
  <line x1="420" y1="50" x2="500" y2="86" stroke="#264653" stroke-width="3" marker-end="url(#arrow)"></line>
  <line x1="420" y1="130" x2="500" y2="86" stroke="#264653" stroke-width="3" marker-end="url(#arrow)"></line>
  <line x1="650" y1="86" x2="700" y2="86" stroke="#264653" stroke-width="3" marker-end="url(#arrow)"></line>
</svg>
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
        diff = payload.get("diff_summary") or {}
        facet_html = "".join(
            f'<span class="facet-badge"><strong>{escape(str(item.get("name", "")))}</strong><em>{escape(str(item.get("reason", "")))}</em></span>'
            for item in facets
        ) or '<span class="facet-badge"><strong>유지보수</strong><em>추가 분류 근거가 부족해 기본 태그를 사용했습니다.</em></span>'
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
      <p class="meta">{escape(card['report_type']).upper()} · {escape(card['mode']).upper()} · {escape(payload.get('work_type', '')).upper()}</p>
    </div>
    <a class="file-link" href="{escape(card.get('html_path', card['path']).as_uri())}">Open Detail Report</a>
  </div>
  <div class="stats">
    <div><span>Commits</span><strong>{payload.get('commit_count', 0)}</strong></div>
    <div><span>Changed Files</span><strong>{payload.get('changed_file_count', 0)}</strong></div>
    <div><span>Added Lines</span><strong>{diff.get('total_added', 0)}</strong></div>
    <div><span>Deleted Lines</span><strong>{diff.get('total_deleted', 0)}</strong></div>
  </div>
  <div class="facet-strip">{facet_html}</div>
  {board_html}
  <div class="grid">
    <div class="panel">
      <h3>Top Change Areas</h3>
      {svg_area_bars(areas[:5])}
    </div>
    <div class="panel">
      <h3>Execution Flow</h3>
      {svg_flow(areas[:4])}
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
    .facet-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin: 0 0 18px; }}
    .facet-badge {{ display:inline-flex; flex-direction:column; gap:4px; padding:12px 14px; border-radius:18px; background:linear-gradient(180deg,#fff8ee,#f8eddc); border:1px solid var(--line); min-width:180px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.7); }}
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
    )


def build_week_window(today: date) -> ReportWindow:
    monday = today - timedelta(days=today.weekday())
    return ReportWindow(start=monday, end=today, label=f"{monday.isoformat()}_to_{today.isoformat()}")


def build_previous_month_window(today: date) -> ReportWindow:
    start, end = previous_month(today)
    return ReportWindow(start=start, end=end, label=start.strftime("%Y-%m"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate startup daily/weekly/monthly reports.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--date", default=None, help="Reference date YYYY-MM-DD")
    parser.add_argument("--output-root", default=None, help="Optional output root directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = detect_repo_root(Path(args.repo).resolve())
    output_root = Path(args.output_root).resolve() if args.output_root else repo_root
    today = date.fromisoformat(args.date) if args.date else date.today()
    branch = detect_branch(repo_root)
    remote_url = detect_remote_url(repo_root)
    upstream = detect_upstream(repo_root)
    sync_state = ahead_behind(repo_root, upstream)
    generated: list[Path] = []

    yesterday = today - timedelta(days=1)
    daily_window = ReportWindow(yesterday, today, today.isoformat())
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
