"""Pure Jira planning helpers.

This module turns commit evidence and report plan sections into reviewable Jira
*create-task* proposals.  It deliberately has no Jira client, filesystem access,
or status-transition behavior; callers decide whether and how to persist an
approved proposal.

Public entry points build proposals, split validation evidence from future
verification, and produce a deterministic 100-point quality assessment.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence


__all__ = [
    "assess_proposal_quality",
    "build_create_task_proposals",
    "enrich_proposal_quality",
    "extract_jira_key",
    "parse_validation_evidence",
]


_MAX_AUTO_COMMITS = 3
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$", re.IGNORECASE)
_KOREAN_RE = re.compile(r"[가-힣]")
_MEASURABLE_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:%|건|개|회|초|분|시간|일)|"
    r"0건|실패\s*0|누락\s*0|100%|이상|이하|이내|모두|각\s+항목)",
    re.IGNORECASE,
)
_VALIDATION_COMMAND_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:[$>]\s*)?(?:"
    r"pytest(?:\.exe)?\b|"
    r"(?:[^\s]+[\\/])?(?:python|py)(?:\.exe)?\s+-m\s+"
    r"(?:pytest|unittest|compileall)\b|"
    r"npm(?:\.cmd)?\s+(?:test\b|run\s+(?:test|build|lint|check)\b)|"
    r"pnpm\s+(?:test\b|run\s+(?:test|build|lint|check)\b)|"
    r"yarn\s+(?:test\b|run\s+(?:test|build|lint|check)\b)|"
    r"dotnet\s+(?:test|build)\b|node(?:\.exe)?\s+--check\b|compileall\b|"
    r"ruff(?:\s+check)?\b|mypy\b|cargo\s+test\b|go\s+test\b|"
    r"mvnw?(?:\.cmd)?\s+(?:test|verify)\b|"
    r"gradlew?(?:\.bat)?\s+(?:test|check|build)\b|ctest\b|make\s+test\b|"
    r"playwright(?:\s+test)?\b|vitest\b|jest\b)",
    re.IGNORECASE,
)
_VALIDATION_RESULT_RE = re.compile(
    r"(?:\bpassed\b|\bfailed\b|\bsuccess(?:ful|fully)?\b|\bsucceeded\b|"
    r"\bexit(?:\s+code)?\s*[:=]?\s*-?\d+|\b[1-9]\d*\s+errors?\b|"
    r"\b0\s+errors?\b|통과|성공|실패|오류\s*\d+건)",
    re.IGNORECASE,
)


_JIRA_KEY_PATTERN = r"[A-Z][A-Z0-9]{1,15}-[1-9][0-9]*"
_JIRA_KEY_RE = re.compile(
    rf"(?<![A-Z0-9])({_JIRA_KEY_PATTERN})(?![0-9])",
    re.IGNORECASE,
)
_CONVENTIONAL_TYPE_PATTERN = r"(?:feat|fix|refactor|test|docs|chore|perf|build|ci)"
_CONVENTIONAL_RE = re.compile(
    rf"^{_CONVENTIONAL_TYPE_PATTERN}(?:\(([^)]+)\))?!?:\s*",
    re.IGNORECASE,
)
_QUALITY_CONVENTIONAL_PREFIX_RE = re.compile(
    r"^(?:build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(?:\([^\r\n)]*\))?!?:\s*",
    re.IGNORECASE,
)
_CONVENTIONAL_JIRA_SCOPE_RE = re.compile(
    rf"^\s*{_CONVENTIONAL_TYPE_PATTERN}\(\s*(?P<key>{_JIRA_KEY_PATTERN})\s*\)!?:",
    re.IGNORECASE,
)
_SUBJECT_BRACKETED_JIRA_RE = re.compile(
    rf"^\s*(?:{_CONVENTIONAL_TYPE_PATTERN}(?:\([^)]*\))?!?:\s*)?"
    rf"\[\s*(?P<key>{_JIRA_KEY_PATTERN})\s*\](?=\s|[:;,-]|$)",
    re.IGNORECASE,
)
_CONVENTIONAL_PREFIXED_JIRA_RE = re.compile(
    rf"^\s*{_CONVENTIONAL_TYPE_PATTERN}(?:\([^)]*\))?!?:\s*"
    rf"(?P<key>{_JIRA_KEY_PATTERN})(?=\s|[:;,-]|$)",
    re.IGNORECASE,
)
_SUBJECT_PREFIXED_JIRA_RE = re.compile(
    rf"^\s*(?:{_CONVENTIONAL_TYPE_PATTERN}(?:\([^)]*\))?!?:\s*)?"
    rf"(?P<key>{_JIRA_KEY_PATTERN})\s*:(?=\s|$)",
    re.IGNORECASE,
)
_JIRA_TRAILER_RE = re.compile(
    rf"^\s*(?:jira|issue|refs?|references?|fixes|closes|relates-to)\s*:\s*"
    rf"(?P<key>{_JIRA_KEY_PATTERN})"
    rf"(?:\s*[,;]\s*{_JIRA_KEY_PATTERN})*\s*$",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{2,}|[가-힣]{2,}", re.IGNORECASE)
_STOP_TOKENS = {
    "add", "added", "and", "auto", "autoreport", "build", "change", "changed",
    "chore", "code", "complete", "completed", "create", "created", "docs",
    "feat", "feature", "fix", "fixed", "for", "from", "implement", "implemented",
    "issue", "jira", "merge", "plan", "planned", "project", "refactor", "report",
    "task", "test", "tests", "the", "this", "update", "updated", "with",
    "결과", "계획", "구현", "변경", "보고", "수정", "업데이트", "완료", "작업",
    "적용", "추가", "프로젝트",
}

_TASK_SECTION_NAMES = {
    "tasks", "task", "proposals", "jira_tasks", "jira_task", "priority_actions",
    "next_actions", "mid_term_actions", "planned_work", "plan", "plans", "next_steps",
    "next_week", "next_month", "focus", "우선_작업", "중기_작업", "다음_액션",
    "계획", "작업",
}
_FALLBACK_TASK_SECTION_NAMES = {"remaining_work", "remaining", "남은_작업"}
_FIELD_ALIASES = {
    "problem": {"problem", "problems", "context", "issues", "문제", "배경"},
    "outcome": {"outcome", "outcomes", "results", "result", "expected_outcome", "성과", "결과"},
    # ``purpose`` answers "why this ticket exists" in one sentence; ``outcome``
    # answers "what will be true when it is done".  They are rendered as separate
    # headings because a reviewer approving a ticket needs both.
    "purpose": {"purpose", "goal", "objective", "목적"},
    "scope": {"scope", "scopes", "areas", "area", "범위", "영역"},
    "out_of_scope": {
        "out_of_scope", "non_goals", "not_in_scope", "exclusions", "excluded",
        "제외", "제외_범위", "비범위",
    },
    "acceptance_criteria": {
        "acceptance_criteria", "acceptance", "done_criteria", "completion_criteria",
        "완료_조건", "인수_조건",
    },
    # ``validation`` is intentionally treated as a future verification plan for
    # plan/report input.  Only the explicit ``executed_validation`` field or
    # command/result evidence attached to a commit can prove an executed check.
    "validation": {"validation", "validations", "verification", "tests", "test_results", "검증", "테스트"},
    "executed_validation": {
        "executed_validation", "executed_validations", "validation_results",
        "executed_tests", "실행_검증", "검증_결과", "실행_결과",
    },
    "verification_plan": {
        "verification_plan", "verification_plans", "planned_validation",
        "test_plan", "test_plans", "검증_계획", "테스트_계획",
    },
    "validation_environment": {
        "validation_environment", "test_environment", "environment",
        "검증_환경", "테스트_환경", "실행_환경",
    },
    "schedule_rationale": {
        "schedule_rationale", "schedule_basis", "estimate_rationale",
        "일정_근거", "일정_산정_근거",
    },
    "epic_rationale": {
        "epic_rationale", "epic_basis", "epic_alignment",
        "에픽_근거", "에픽_연결_근거",
    },
    "remaining_work": {"remaining_work", "remaining", "follow_up", "follow_ups", "남은_작업", "후속_작업"},
    "risks": {"risks", "risk", "concerns", "리스크", "위험"},
}

# Handled outside _FIELD_ALIASES: a work breakdown is a sequence of records
# ({summary, description, acceptance_criteria}), and _as_text_list would flatten
# each step into one unusable string.
_SUBTASK_ALIASES = {
    "subtasks", "sub_tasks", "subtask", "breakdown", "work_breakdown",
    "부작업", "서브작업", "하위작업", "세부작업",
}
_MAX_DERIVED_SUBTASKS = 3


def _section_name(value: Any) -> str:
    return re.sub(r"[\s\-/]+", "_", str(value or "").strip().lower())


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _as_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = _clean_text(value)
        return [text] if text else []
    if isinstance(value, Mapping):
        return []
    if isinstance(value, Iterable):
        result: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                continue
            text = _clean_text(item)
            if text:
                result.append(text)
        return result
    text = _clean_text(value)
    return [text] if text else []


def _as_validation_list(value: Any) -> list[Any]:
    """Normalize validation input without discarding structured records."""

    if value is None:
        return []
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, str):
        text = _clean_text(value)
        return [text] if text else []
    if isinstance(value, Iterable):
        result: list[Any] = []
        for item in value:
            if isinstance(item, Mapping):
                result.append(dict(item))
            else:
                text = _clean_text(item)
                if text:
                    result.append(text)
        return result
    text = _clean_text(value)
    return [text] if text else []


def _validation_legacy_texts(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, Mapping):
            text = _clean_text(value)
        else:
            command = _clean_text(value.get("command") or value.get("text"))
            actual_value = value.get("actual_result")
            if actual_value in (None, "") and "exit_code" in value:
                actual_value = value.get("exit_code")
            actual = re.sub(
                r"\s+", " ", str(actual_value if actual_value is not None else "")
            ).strip()
            text = f"{command}: {actual}" if command and actual else command
        if text:
            result.append(text)
    return _unique(result)


def _with_validation_environment(values: Iterable[Any], environment: str) -> list[Any]:
    prepared: list[Any] = []
    for value in values:
        if isinstance(value, Mapping):
            record = dict(value)
            if environment and not record.get("environment"):
                record["environment"] = environment
            prepared.append(record)
        else:
            prepared.append({"text": value, "environment": environment})
    return prepared


def _force_not_run(values: Iterable[Any], environment: str) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for value in _with_validation_environment(values, environment):
        record = dict(value) if isinstance(value, Mapping) else {"text": value}
        record["status"] = "not_run"
        record.pop("actual_result", None)
        record.pop("exit_code", None)
        record.pop("result", None)
        prepared.append(record)
    return prepared


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = _clean_text(item)
        marker = text.casefold()
        if text and marker not in seen:
            seen.add(marker)
            result.append(text)
    return result


def _normalize_subtasks(value: Any) -> tuple[dict[str, Any], ...]:
    """Normalize a work breakdown into ``{summary, description, acceptance_criteria}``.

    Accepts a plain string, a mapping, or a sequence of either.  Input order is
    preserved: a breakdown is a sequence of steps, and sorting it would destroy
    the ordering the author intended.  Entries without a summary are dropped, and
    a repeated summary is kept only once.
    """
    if value is None:
        return ()
    items = [value] if isinstance(value, (str, Mapping)) else list(value or [])
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, Mapping):
            summary = _clean_text(
                item.get("summary") or item.get("title") or item.get("name")
                or item.get("text") or item.get("action")
            )
            description = _clean_text(
                item.get("description") or item.get("detail")
                or item.get("details") or item.get("설명")
            )
            criteria = _as_text_list(
                item.get("acceptance_criteria") or item.get("done_criteria")
                or item.get("완료_조건") or item.get("완료조건")
            )
        else:
            summary, description, criteria = _clean_text(item), "", []
        if not summary:
            continue
        marker = summary.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append({
            "summary": summary[:255],
            "description": description,
            "acceptance_criteria": _unique(criteria),
        })
    return tuple(result)


def extract_jira_key(text: Any) -> str:
    """Return the first explicitly referenced Jira key in *text*.

    A subject must lead with a bracketed key, a ``KEY:`` prefix, or a Jira
    key used as the scope of a conventional commit.  Subsequent body lines
    must use an explicit trailer such as ``Jira: KEY`` or ``Refs: KEY``.
    Merely mentioning a Jira-looking token in prose or a code example does not
    bind the commit or plan item to that issue.
    """

    lines = str(text or "").splitlines()
    if not lines:
        return ""

    subject = lines[0]
    for pattern in (
        _CONVENTIONAL_JIRA_SCOPE_RE,
        _SUBJECT_BRACKETED_JIRA_RE,
        _CONVENTIONAL_PREFIXED_JIRA_RE,
        _SUBJECT_PREFIXED_JIRA_RE,
        _JIRA_TRAILER_RE,
    ):
        match = pattern.match(subject)
        if match:
            return match.group("key").upper()

    for line in lines[1:]:
        match = _JIRA_TRAILER_RE.match(line)
        if match:
            return match.group("key").upper()
    return ""


def _jira_key_from_explicit_field(value: Any) -> str:
    """Normalize a key carried by a field whose schema is already explicit."""

    match = _JIRA_KEY_RE.search(str(value or ""))
    return match.group(1).upper() if match else ""


def _explicit_jira_key(item: Mapping[str, Any]) -> str:
    for name in ("jira_key", "issue_key", "related_jira_key", "parent_key"):
        key = _jira_key_from_explicit_field(item.get(name))
        if key:
            return key
    return ""


def _clean_subject(subject: str) -> str:
    text = _CONVENTIONAL_RE.sub("", subject.strip())
    text = _JIRA_KEY_RE.sub("", text)
    text = re.sub(r"^[\s:;,#-]+|[\s:;,#-]+$", "", text)
    return _clean_text(text) or _clean_text(subject)


def _conventional_scope(subject: str, explicit_scope: Any = "") -> str:
    supplied = _clean_text(explicit_scope).casefold()
    if supplied:
        return supplied
    match = _CONVENTIONAL_RE.match(subject.strip())
    return _clean_text(match.group(1)).casefold() if match and match.group(1) else ""


def _tokens(*values: Any) -> frozenset[str]:
    found: set[str] = set()
    for value in values:
        if isinstance(value, (list, tuple, set, frozenset)):
            found.update(_tokens(*value))
            continue
        text = _JIRA_KEY_RE.sub(" ", str(value or "")).casefold()
        for token in _TOKEN_RE.findall(text):
            token = token.casefold().strip("_-.")
            if len(token) >= 3 and token not in _STOP_TOKENS:
                found.add(token)
    return frozenset(found)


def _repository_web_url(repository_url: str) -> str:
    url = repository_url.strip().rstrip("/")
    if url.startswith("git@") and ":" in url:
        host_path = url[4:]
        host, path = host_path.split(":", 1)
        url = f"https://{host}/{path}"
    elif url.startswith("ssh://git@"):
        url = "https://" + url[len("ssh://git@") :]
    if url.endswith(".git"):
        url = url[:-4]
    return url.rstrip("/")


def _validation_parts(text: str) -> tuple[str, str, str]:
    """Extract command, environment, and observed-result text conservatively."""

    normalized = _clean_text(text)
    environment = ""
    env_match = re.search(
        r"(?:\b(?:env|environment)\b|환경)\s*[:=]\s*([^;|]+)",
        normalized,
        re.IGNORECASE,
    )
    if env_match:
        environment = env_match.group(1).strip(" []()")
    else:
        bracket = re.match(r"^\[([^]]+)\]\s*", normalized)
        if bracket and re.search(
            r"windows|linux|macos|python|node|java|dotnet|ubuntu|docker|gha|ci",
            bracket.group(1),
            re.IGNORECASE,
        ):
            environment = bracket.group(1).strip()

    result_match = _VALIDATION_RESULT_RE.search(normalized)
    actual_result = ""
    command = normalized
    if result_match:
        delimiter = max(
            normalized.rfind("->", 0, result_match.start()),
            normalized.rfind("=>", 0, result_match.start()),
            normalized.rfind(":", 0, result_match.start()),
        )
        if delimiter >= 0:
            delimiter_width = 2 if normalized[delimiter:delimiter + 2] in {"->", "=>"} else 1
            command = normalized[:delimiter].strip(" |;,-")
            actual_result = normalized[delimiter + delimiter_width:].strip()
        else:
            command = normalized[:result_match.start()].strip(" []()|;,-")
            actual_result = normalized[result_match.start():].strip()

    command = re.sub(
        r"^(?:\[[^]]+\]\s*)|(?:\b(?:env|environment)\b|환경)\s*[:=]\s*[^;|]+[;|]?\s*",
        "",
        command,
        flags=re.IGNORECASE,
    ).strip()
    return command, environment, actual_result


def _planned_environment_for_command(command: str) -> str:
    """Return a deterministic future-run environment for a recognized command."""

    normalized = command.casefold().strip()
    if re.search(
        r"(?:^|[\\/])(?:python|py)(?:\.exe)?\s+-m\s+|"
        r"^pytest(?:\.exe)?\b|^compileall\b|^ruff\b|^mypy\b",
        normalized,
    ):
        return "프로젝트 저장소의 현재 Python 테스트 환경"
    if re.match(
        r"^(?:npm(?:\.cmd)?|pnpm|yarn|node(?:\.exe)?|playwright|vitest|jest)\b",
        normalized,
    ):
        return "프로젝트 저장소의 현재 Node.js 테스트 환경"
    if re.match(r"^dotnet\b", normalized):
        return "프로젝트 저장소의 현재 .NET 테스트 환경"
    if re.match(r"^cargo\b", normalized):
        return "프로젝트 저장소의 현재 Rust 테스트 환경"
    if re.match(r"^go\s+test\b", normalized):
        return "프로젝트 저장소의 현재 Go 테스트 환경"
    if re.match(r"^(?:mvnw?|gradlew?)", normalized):
        return "프로젝트 저장소의 현재 JVM 테스트 환경"
    return "프로젝트 저장소의 현재 빌드 테스트 환경"


def parse_validation_evidence(
    values: Any,
    *,
    source: str = "",
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split raw validation notes into executed evidence and future plans.

    An executed record is returned only when command, environment, actual
    result, and provenance source are all explicit.  Every other record becomes
    a ``status='not_run'`` verification plan, so an expected PASS can never be
    mistaken for a completed run.
    """

    if values is None:
        raw_values: list[Any] = []
    elif isinstance(values, (str, Mapping)):
        raw_values = [values]
    elif isinstance(values, Iterable):
        raw_values = list(values)
    else:
        raw_values = [values]

    executed: list[dict[str, str]] = []
    planned: list[dict[str, str]] = []
    for raw in raw_values:
        structured_command = False
        if isinstance(raw, Mapping):
            structured_command = bool(_clean_text(raw.get("command")))
            command = _clean_text(raw.get("command") or raw.get("test") or raw.get("value"))
            environment = _clean_text(raw.get("environment") or raw.get("test_environment"))
            actual_value = (
                raw.get("actual_result")
                if raw.get("actual_result") not in (None, "")
                else raw.get("result")
            )
            if actual_value in (None, "") and "exit_code" in raw:
                actual_value = raw.get("exit_code")
            actual_result = re.sub(
                r"\s+", " ", str(actual_value if actual_value is not None else "")
            ).strip()
            record_source = _clean_text(raw.get("source") or source)
            status = _clean_text(raw.get("status")).casefold()
            expected_result = _clean_text(raw.get("expected_result"))
            pass_criteria = _clean_text(raw.get("pass_criteria"))
            if not command and raw.get("text"):
                command, parsed_environment, parsed_result = _validation_parts(
                    _clean_text(raw.get("text"))
                )
                environment = environment or parsed_environment
                actual_result = actual_result or parsed_result
        else:
            text = _clean_text(raw)
            if not text:
                continue
            command, environment, actual_result = _validation_parts(text)
            record_source = _clean_text(source)
            status = ""
            expected_result = ""
            pass_criteria = ""

        raw_text = _clean_text(
            raw.get("text") if isinstance(raw, Mapping) else raw
        )
        question_like = bool(re.search(
            r"(?:나요|까요|습니까|인가요)\s*[?？]?\s*$|[?？]\s*$",
            raw_text,
            re.IGNORECASE,
        ))
        recognized_raw_command = bool(
            not question_like and _VALIDATION_COMMAND_RE.match(command)
        )

        is_executed = bool(
            command
            and environment
            and actual_result
            and record_source
            and status not in {"not_run", "planned", "pending"}
            and (structured_command or recognized_raw_command)
            and (
                _VALIDATION_RESULT_RE.search(actual_result)
                or re.fullmatch(r"-?\d+", actual_result)
            )
        )
        if is_executed:
            executed.append({
                "command": command,
                "environment": environment,
                "actual_result": actual_result,
                "source": record_source,
            })
            continue

        plan_command = command or _clean_text(raw.get("text") if isinstance(raw, Mapping) else raw)
        if not plan_command:
            continue
        if not structured_command and not recognized_raw_command:
            continue
        planned_environment = environment
        if (
            not structured_command
            and recognized_raw_command
            and (
                not planned_environment
                or re.search(
                    r"확정\s*필요|미정|unknown|unspecified",
                    planned_environment,
                    re.IGNORECASE,
                )
            )
        ):
            planned_environment = _planned_environment_for_command(plan_command)
        planned.append({
            "command": plan_command,
            "environment": planned_environment or "실행 전 확정 필요",
            "expected_result": expected_result or "명령이 오류 없이 완료된다.",
            "pass_criteria": pass_criteria or "실패 0건",
            "status": "not_run",
            "source": record_source or "계획 입력",
        })

    def marker(item: Mapping[str, str]) -> str:
        return json.dumps(item, ensure_ascii=False, sort_keys=True)

    executed_by_marker = {marker(item): item for item in executed}
    planned_by_marker = {marker(item): item for item in planned}
    return (
        [executed_by_marker[key] for key in sorted(executed_by_marker)],
        [planned_by_marker[key] for key in sorted(planned_by_marker)],
    )


@dataclass(frozen=True)
class _Commit:
    sha: str
    subject: str
    url: str
    body: str
    jira_key: str
    binding_source: str
    scope: str
    files: tuple[str, ...]
    executed_validation: tuple[Mapping[str, str], ...]
    verification_plan: tuple[Mapping[str, str], ...]
    validation_environment: tuple[str, ...]
    validation_legacy: tuple[str, ...]
    parent_count: int | None
    tokens: frozenset[str]


@dataclass(frozen=True)
class _PlanItem:
    summary: str
    jira_key: str
    binding_source: str
    project_key: str
    epic_key: str
    source_section: str
    commit_shas: frozenset[str]
    problem: tuple[str, ...]
    outcome: tuple[str, ...]
    purpose: tuple[str, ...]
    scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    subtasks: tuple[dict[str, Any], ...]
    acceptance_criteria: tuple[str, ...]
    executed_validation: tuple[Mapping[str, str], ...]
    verification_plan: tuple[Mapping[str, str], ...]
    validation_environment: tuple[str, ...]
    validation_legacy: tuple[str, ...]
    schedule_rationale: tuple[str, ...]
    epic_rationale: tuple[str, ...]
    remaining_work: tuple[str, ...]
    risks: tuple[str, ...]
    tokens: frozenset[str]


@dataclass(frozen=True)
class _CommitGroup:
    commits: tuple[_Commit, ...]
    jira_key: str
    binding_source: str
    scope: str
    tokens: frozenset[str]
    grouping_rationale: str


def _normalize_commits(
    commits: Sequence[Mapping[str, Any]], repository_url: str
) -> list[_Commit]:
    web_url = _repository_web_url(repository_url)
    by_sha: dict[str, _Commit] = {}
    for raw in commits:
        if not isinstance(raw, Mapping):
            raise TypeError("each commit must be a mapping")
        sha = _clean_text(raw.get("sha") or raw.get("hash") or raw.get("commit"))
        subject = _clean_text(raw.get("subject") or raw.get("title") or raw.get("message"))
        if not sha or not subject:
            raise ValueError("each commit requires non-empty sha/hash and subject/message")
        explicit_key = _explicit_jira_key(raw)
        body = str(raw.get("body") or raw.get("description") or "").strip()
        parsed_key = extract_jira_key(f"{subject}\n{body}")
        jira_key = explicit_key or parsed_key
        binding_source = "commit.jira_key" if explicit_key else ("commit.text" if parsed_key else "none")
        files = tuple(sorted(_unique(_as_text_list(raw.get("changed_files") or raw.get("files")))))
        validation_environment = tuple(_unique(_as_text_list(
            raw.get("validation_environment")
            or raw.get("test_environment")
            or raw.get("environment")
        )))
        environment = "; ".join(validation_environment)
        raw_executed = _as_validation_list(raw.get("executed_validation"))
        raw_legacy = [
            *_as_validation_list(raw.get("validation")),
            *_as_validation_list(raw.get("test_results")),
            *_as_validation_list(raw.get("tests")),
        ]
        raw_plan = [
            *_as_validation_list(raw.get("verification_plan")),
            *_as_validation_list(raw.get("test_plan")),
        ]
        executed_validation, rejected_executed = parse_validation_evidence(
            _with_validation_environment(raw_executed, environment),
            source=f"commit:{sha}",
        )
        legacy_executed, legacy_plan = parse_validation_evidence(
            _with_validation_environment(raw_legacy, environment),
            source=f"commit:{sha}",
        )
        _, explicit_plan = parse_validation_evidence(
            _with_validation_environment(raw_plan, environment),
            source=f"commit:{sha}",
        )
        executed_validation.extend(legacy_executed)
        _, verification_plan = parse_validation_evidence([
            *rejected_executed,
            *legacy_plan,
            *explicit_plan,
        ])
        scope = _conventional_scope(subject, raw.get("scope"))
        supplied_url = _clean_text(raw.get("url") or raw.get("commit_url"))
        url = supplied_url or (f"{web_url}/commit/{sha}" if web_url else "")
        parent_count_raw = raw.get("parent_count")
        try:
            parent_count = int(parent_count_raw) if parent_count_raw is not None else None
        except (TypeError, ValueError):
            parent_count = None
        commit = _Commit(
            sha=sha,
            subject=subject,
            url=url,
            body=body,
            jira_key=jira_key,
            binding_source=binding_source,
            scope=scope,
            files=files,
            executed_validation=tuple(executed_validation),
            verification_plan=tuple(verification_plan),
            validation_environment=validation_environment,
            validation_legacy=tuple(_validation_legacy_texts([
                *executed_validation,
                *verification_plan,
            ])),
            parent_count=parent_count,
            tokens=_tokens(_clean_subject(subject), body, scope, files),
        )
        old = by_sha.get(sha.casefold())
        if old and (old.subject != commit.subject or old.url != commit.url):
            raise ValueError(f"conflicting evidence for commit {sha}")
        by_sha[sha.casefold()] = commit
    return sorted(by_sha.values(), key=lambda c: (c.jira_key, c.scope, c.subject.casefold(), c.sha.casefold()))


def _plan_field(raw: Mapping[str, Any], name: str, global_fields: Mapping[str, list[str]]) -> tuple[str, ...]:
    aliases = _FIELD_ALIASES[name]
    local: list[str] = []
    for key, value in raw.items():
        if _section_name(key) in aliases:
            local.extend(_as_text_list(value))
    return tuple(_unique(local or global_fields.get(name, [])))


def _plan_validation_field(
    raw: Mapping[str, Any],
    name: str,
    plan_sections: Mapping[str, Any],
) -> list[Any]:
    aliases = _FIELD_ALIASES[name]
    local: list[Any] = []
    for key, value in raw.items():
        if _section_name(key) in aliases:
            local.extend(_as_validation_list(value))
    if local:
        return local
    global_values: list[Any] = []
    for section, value in plan_sections.items():
        if _section_name(section) in aliases:
            global_values.extend(_as_validation_list(value))
    return global_values


def _global_plan_fields(plan_sections: Mapping[str, Any]) -> dict[str, list[str]]:
    result = {name: [] for name in _FIELD_ALIASES}
    for section, value in plan_sections.items():
        normalized = _section_name(section)
        for name, aliases in _FIELD_ALIASES.items():
            if normalized in aliases:
                result[name].extend(_as_text_list(value))
    return {name: _unique(values) for name, values in result.items()}


def _iter_task_entries(plan_sections: Mapping[str, Any]) -> list[tuple[str, Any]]:
    entries: list[tuple[str, Any]] = []
    for section, value in plan_sections.items():
        normalized = _section_name(section)
        if normalized not in _TASK_SECTION_NAMES:
            continue
        if isinstance(value, Mapping) or isinstance(value, str):
            values = [value]
        elif isinstance(value, Iterable):
            values = list(value)
        else:
            values = [value]
        entries.extend((normalized, item) for item in values)
    if entries:
        # Structured entries first (stable within each class).  A report usually
        # repeats the same action as a bare string in ``priority_actions`` and as
        # a full record in ``tasks``; _normalize_plan_items dedupes by summary, so
        # whichever arrives first wins — and the record carries the narrative.
        entries.sort(key=lambda pair: 0 if isinstance(pair[1], Mapping) else 1)
        return entries
    for section, value in plan_sections.items():
        normalized = _section_name(section)
        if normalized not in _FALLBACK_TASK_SECTION_NAMES:
            continue
        values = [value] if isinstance(value, (Mapping, str)) else list(value or [])
        entries.extend((normalized, item) for item in values)
    return entries


def _normalize_plan_items(plan_sections: Mapping[str, Any]) -> tuple[list[_PlanItem], dict[str, list[str]]]:
    global_fields = _global_plan_fields(plan_sections)
    items: list[_PlanItem] = []
    seen: set[str] = set()
    for source_section, entry in _iter_task_entries(plan_sections):
        raw: Mapping[str, Any]
        if isinstance(entry, Mapping):
            raw = entry
            summary = _clean_text(
                raw.get("summary") or raw.get("title") or raw.get("action")
                or raw.get("text") or raw.get("name")
            )
        else:
            raw = {}
            summary = _clean_text(entry)
        if not summary:
            continue
        explicit_key = _explicit_jira_key(raw)
        parsed_key = extract_jira_key(summary)
        jira_key = explicit_key or parsed_key
        binding_source = "plan.jira_key" if explicit_key else ("plan.text" if parsed_key else "none")
        project_key = _clean_text(raw.get("project_key"))
        epic_key = _jira_key_from_explicit_field(raw.get("epic_key"))
        commit_shas = frozenset(
            s.casefold() for s in _as_text_list(raw.get("commit_shas") or raw.get("commits")) if s
        )
        keywords = _as_text_list(raw.get("keywords"))
        subtask_values: list[Any] = []
        for key, value in raw.items():
            if _section_name(key) not in _SUBTASK_ALIASES:
                continue
            subtask_values.extend(
                [value] if isinstance(value, (str, Mapping)) else list(value or [])
            )
        validation_environment = _plan_field(
            raw, "validation_environment", global_fields
        )
        environment = "; ".join(validation_environment)
        raw_executed = _plan_validation_field(
            raw, "executed_validation", plan_sections
        )
        raw_plan = _plan_validation_field(raw, "verification_plan", plan_sections)
        raw_legacy = _plan_validation_field(raw, "validation", plan_sections)
        executed_validation, rejected_executed = parse_validation_evidence(
            _with_validation_environment(raw_executed, environment),
            source=f"plan:{source_section}",
        )
        _, explicit_plan = parse_validation_evidence(
            _force_not_run(raw_plan, environment),
            source=f"plan:{source_section}",
        )
        # Legacy report ``validation`` is future work unless the author used the
        # explicit executed_validation schema.  This prevents narrative "PASS"
        # text from becoming execution evidence.
        _, legacy_plan = parse_validation_evidence(
            _force_not_run(raw_legacy, environment),
            source=f"plan:{source_section}",
        )
        _, verification_plan = parse_validation_evidence([
            *rejected_executed,
            *explicit_plan,
            *legacy_plan,
        ])
        item = _PlanItem(
            summary=summary[:255],
            jira_key=jira_key,
            binding_source=binding_source,
            project_key=project_key,
            epic_key=epic_key,
            source_section=source_section,
            commit_shas=commit_shas,
            problem=_plan_field(raw, "problem", global_fields),
            outcome=_plan_field(raw, "outcome", global_fields),
            purpose=_plan_field(raw, "purpose", global_fields),
            scope=_plan_field(raw, "scope", global_fields),
            out_of_scope=_plan_field(raw, "out_of_scope", global_fields),
            subtasks=_normalize_subtasks(subtask_values),
            acceptance_criteria=_plan_field(raw, "acceptance_criteria", global_fields),
            executed_validation=tuple(executed_validation),
            verification_plan=tuple(verification_plan),
            validation_environment=validation_environment,
            validation_legacy=tuple(_validation_legacy_texts([
                *executed_validation,
                *verification_plan,
            ])),
            schedule_rationale=_plan_field(raw, "schedule_rationale", global_fields),
            epic_rationale=_plan_field(raw, "epic_rationale", global_fields),
            remaining_work=_plan_field(raw, "remaining_work", global_fields),
            risks=_plan_field(raw, "risks", global_fields),
            tokens=_tokens(summary, keywords, raw.get("scope")),
        )
        # The section is deliberately NOT part of the identity: the same action
        # listed in both ``priority_actions`` and ``tasks`` is one piece of work,
        # and keying on the section produced two proposals for it (the identity
        # digest in _build_proposal carries plan_source, so they survived the
        # dedupe_marker pass too).
        marker = json.dumps(
            {
                "summary": item.summary.casefold(),
                "jira_key": item.jira_key,
                "commit_shas": sorted(item.commit_shas),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if marker not in seen:
            seen.add(marker)
            items.append(item)
    items.sort(key=lambda p: (p.jira_key, p.summary.casefold(), p.source_section))
    return items, global_fields


def _commits_related(left: _Commit, right: _Commit) -> bool:
    if left.jira_key and right.jira_key:
        return left.jira_key == right.jira_key
    if left.scope and right.scope and left.scope == right.scope:
        return True
    # Different conventional scopes are explicit feature boundaries.  Do not let
    # generic words such as "validation" bridge them into one oversized task.
    if left.scope and right.scope and left.scope != right.scope:
        return False

    generic = {
        "approval", "config", "regression", "scripts", "shared", "tests",
        "validation", "verify", "workflow",
    }
    overlap = (left.tokens & right.tokens) - generic
    if len(overlap) >= 2:
        return True

    left_roots = {path.replace("\\", "/").split("/", 1)[0].casefold() for path in left.files}
    right_roots = {path.replace("\\", "/").split("/", 1)[0].casefold() for path in right.files}
    # A shared repository root alone is weak evidence, so it needs at least one
    # non-generic purpose token as corroboration.
    return bool(left_roots & right_roots and overlap)


def _grouping_rationale(commits: Sequence[_Commit], scope: str, jira_key: str) -> str:
    if len(commits) == 1:
        return "단일 커밋이므로 별도 기능 작업으로 분리했다."
    if jira_key:
        return f"동일한 명시적 Jira 키 {jira_key}의 커밋을 묶었다."
    if scope:
        return f"동일한 기능 범위 '{scope}'의 커밋을 최대 {_MAX_AUTO_COMMITS}건으로 묶었다."
    common = set(commits[0].tokens)
    for commit in commits[1:]:
        common &= commit.tokens
    keywords = ", ".join(sorted(common)[:3]) or "공통 변경 경로와 목적"
    return f"{keywords} 연관성을 기준으로 커밋을 최대 {_MAX_AUTO_COMMITS}건으로 묶었다."


def _group_commits(
    commits: Sequence[_Commit], plans: Sequence[_PlanItem] = ()
) -> list[_CommitGroup]:
    if not commits:
        return []
    # A structured plan's commit_shas is an explicit binding, so it partitions
    # evidence before lexical/scope clustering.  This prevents two similarly worded
    # commits assigned to different planned tasks from being merged first and becoming
    # impossible to separate later.
    explicit_plan_for_sha: dict[str, int] = {}
    for plan_index, plan in enumerate(plans):
        for sha in plan.commit_shas:
            previous = explicit_plan_for_sha.get(sha)
            if previous is not None and previous != plan_index:
                raise ValueError(f"commit {sha} is explicitly bound to multiple plan tasks")
            explicit_plan_for_sha[sha] = plan_index
    parent = list(range(len(commits)))
    keys = [{c.jira_key} if c.jira_key else set() for c in commits]

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a == b or len(keys[a] | keys[b]) > 1:
            return
        parent[b] = a
        keys[a] |= keys[b]

    for left in range(len(commits)):
        for right in range(left + 1, len(commits)):
            left_plan = explicit_plan_for_sha.get(commits[left].sha.casefold())
            right_plan = explicit_plan_for_sha.get(commits[right].sha.casefold())
            if left_plan is not None or right_plan is not None:
                related = left_plan is not None and left_plan == right_plan
            else:
                related = _commits_related(commits[left], commits[right])
            if related:
                union(left, right)

    components: dict[int, list[_Commit]] = {}
    for index, commit in enumerate(commits):
        components.setdefault(find(index), []).append(commit)

    groups: list[_CommitGroup] = []
    for members in components.values():
        ordered_members = tuple(sorted(
            members,
            key=lambda c: (c.sha.casefold(), c.subject.casefold()),
        ))
        # Even a strongly related component is split into reviewable task grains.
        # This also prevents transitive lexical matching from producing one broad
        # auto-apply candidate.
        for offset in range(0, len(ordered_members), _MAX_AUTO_COMMITS):
            ordered = ordered_members[offset:offset + _MAX_AUTO_COMMITS]
            jira_keys = {c.jira_key for c in ordered if c.jira_key}
            jira_key = next(iter(jira_keys)) if jira_keys else ""
            if any(
                c.jira_key == jira_key and c.binding_source == "commit.jira_key"
                for c in ordered
            ):
                binding_source = "commit.jira_key"
            else:
                binding_source = "commit.text" if jira_key else "none"
            scopes = sorted({c.scope for c in ordered if c.scope})
            scope = scopes[0] if len(scopes) == 1 else ""
            groups.append(
                _CommitGroup(
                    commits=ordered,
                    jira_key=jira_key,
                    binding_source=binding_source,
                    scope=scope,
                    tokens=frozenset().union(*(c.tokens for c in ordered)),
                    grouping_rationale=_grouping_rationale(ordered, scope, jira_key),
                )
            )
    groups.sort(key=lambda g: (g.jira_key, g.scope, tuple(c.sha.casefold() for c in g.commits)))
    return groups


def _match_score(plan: _PlanItem, group: _CommitGroup, single_pair: bool) -> float:
    if plan.jira_key and group.jira_key and plan.jira_key != group.jira_key:
        return -1.0
    if plan.commit_shas:
        matched = plan.commit_shas & {c.sha.casefold() for c in group.commits}
        if matched:
            return 2000.0 + len(matched)
    score = 0.0
    if plan.jira_key and group.jira_key and plan.jira_key == group.jira_key:
        score += 1000.0
    overlap = plan.tokens & group.tokens
    score += 25.0 * len(overlap)
    union = plan.tokens | group.tokens
    if union:
        score += 10.0 * len(overlap) / len(union)
    if group.scope and group.scope in plan.tokens:
        score += 100.0
    if score == 0.0 and single_pair:
        score = 1.0
    return score


def _pair_plan_and_groups(
    plans: Sequence[_PlanItem], groups: Sequence[_CommitGroup]
) -> tuple[list[tuple[_PlanItem | None, _CommitGroup | None]], set[int], set[int]]:
    candidates: list[tuple[float, int, int]] = []
    single_pair = len(plans) == 1 and len(groups) == 1
    for pi, plan in enumerate(plans):
        for gi, group in enumerate(groups):
            score = _match_score(plan, group, single_pair)
            if score > 0:
                candidates.append((score, pi, gi))
    candidates.sort(
        key=lambda row: (
            -row[0], plans[row[1]].summary.casefold(),
            tuple(c.sha.casefold() for c in groups[row[2]].commits),
        )
    )
    used_plans: set[int] = set()
    used_groups: set[int] = set()
    pairs: list[tuple[_PlanItem | None, _CommitGroup | None]] = []
    for _, pi, gi in candidates:
        if pi in used_plans or gi in used_groups:
            continue
        used_plans.add(pi)
        used_groups.add(gi)
        pairs.append((plans[pi], groups[gi]))
    return pairs, used_plans, used_groups


def _binding(plan: _PlanItem | None, group: _CommitGroup | None) -> tuple[str, str]:
    # Explicit fields outrank keys merely parsed from free text.  Plan metadata is
    # authoritative when both explicit fields agree; conflicting effective keys are
    # never paired by _match_score.
    candidates = (
        (plan.jira_key if plan and plan.binding_source == "plan.jira_key" else "", "plan.jira_key"),
        (group.jira_key if group and group.binding_source == "commit.jira_key" else "", "commit.jira_key"),
        (plan.jira_key if plan and plan.binding_source == "plan.text" else "", "plan.text"),
        (group.jira_key if group and group.binding_source == "commit.text" else "", "commit.text"),
    )
    return next(((key, source) for key, source in candidates if key), ("", "none"))


def _group_summary(group: _CommitGroup) -> str:
    first = _clean_subject(group.commits[0].subject)
    if len(group.commits) == 1:
        return first[:255]
    label = f"{group.scope}: " if group.scope else ""
    return f"{label}{first} (+{len(group.commits) - 1} related commits)"[:255]


def _is_measurable_korean_criterion(value: Any) -> bool:
    text = _clean_text(value)
    return bool(_KOREAN_RE.search(text) and _MEASURABLE_RE.search(text))


def _acceptance_criteria(
    supplied: Iterable[str],
    *,
    summary: str,
    commit_count: int,
    file_count: int,
    scope_count: int,
    evidence_context: str,
) -> list[str]:
    criteria = _unique(supplied)
    if commit_count:
        generated = [
            (
                (
                    f"{evidence_context}의 범위를 검토하고 관련 없는 변경이 0건임을 확인한다."
                    if file_count
                    else (
                        f"근거 커밋 {commit_count}건의 변경 파일 목록을 수집하고 "
                        "파일 근거 누락 0건을 확인한다."
                    )
                )
            ),
            (
                f"'{summary}' 관련 검증 계획을 실행하고 실패 0건과 실행 환경 1건 이상을 "
                "Jira에 기록한다."
            ),
        ]
    else:
        generated = [
            (
                f"'{summary}' 계획 범위 {max(scope_count, 1)}개 항목의 담당자와 완료 조건을 "
                "확정하고 누락 0건을 확인한다."
            ),
            (
                "관련 검증 계획을 실행해 실패 0건과 실행 환경 1건 이상을 Jira에 기록한다."
            ),
        ]
    measurable_korean = sum(_is_measurable_korean_criterion(item) for item in criteria)
    for item in generated:
        if measurable_korean >= 2:
            break
        criteria.append(item)
        measurable_korean += 1
    return _unique(criteria)


def _task_specific_risk(
    *,
    summary: str,
    commits: Sequence[_Commit],
    files: Sequence[str],
    scopes: Sequence[str],
    has_validation_evidence: bool,
    evidence_context: str,
) -> str:
    target = (files[0] if files else (scopes[0] if scopes else summary))
    if not commits:
        return (
            f"{evidence_context}에서 범위·담당·의존성 누락이 발생하면 일정이 지연될 수 있으므로 "
            "착수 전에 미확정 항목 0건을 확인한다."
        )
    if not has_validation_evidence:
        return (
            f"{evidence_context}의 {target} 회귀를 입증할 실행 근거가 없어 결함이 누락될 수 있으므로 "
            "Task 종료 전에 해당 검증 계획을 실행해 실패 0건을 확인한다."
        )
    return (
        f"'{summary}' 변경이 {target} 외 경로에 회귀를 만들 수 있으므로 영향 범위 검증에서 "
        "실패 0건을 확인한다."
    )


def _default_verification_command(files: Sequence[str]) -> str:
    normalized = [path.replace("\\", "/").casefold() for path in files]
    if any(path.endswith((".ts", ".tsx", ".js", ".jsx")) for path in normalized):
        return "npm test"
    if any(path.endswith(".cs") for path in normalized):
        return "dotnet test"
    if any(path.endswith(".go") for path in normalized):
        return "go test ./..."
    test_files = [path for path in normalized if path.endswith(".py") and "test" in path]
    return f"pytest {' '.join(test_files[:3])} -q" if test_files else "pytest -q"


def _commit_evidence_refs(commits: Sequence[_Commit]) -> str:
    """Short commit references for prose reuse.

    ``evidence_context`` is interpolated into the problem, acceptance-criteria,
    remaining-work and risk sentences, so full 40-char hashes were repeated four
    or more times in a single ticket body.  No length cap is needed here —
    ``_group_commits`` already chunks a group at ``_MAX_AUTO_COMMITS``.

    The unabridged hashes and their links stay in the 근거 커밋 section, and
    ``source_commits[].sha`` is untouched: the quality gate's ``full_shas`` check
    and the proposal identity digest both read that field, not this prose.
    """
    return ", ".join(commit.sha[:12] for commit in commits)


def _derived_subtasks(
    *,
    evidence_context: str,
    files: Sequence[str],
    verification_plan: Sequence[Any],
    remaining_work: Sequence[str],
) -> list[dict[str, Any]]:
    """Evidence-anchored breakdown for a proposal whose plan supplied none.

    Every step names something already in the proposal — the change surface, the
    verification command, the follow-up the report asked for — so the breakdown
    stays checkable instead of becoming boilerplate.  Callers only reach this for
    commit-backed proposals: a plan draft with no author-written breakdown shows
    an empty section rather than an invented one.
    """
    steps: list[dict[str, Any]] = [{
        "summary": "변경 범위 확인",
        "description": (
            f"{evidence_context}를 파일 단위로 확인하고 이 작업과 무관한 변경을 분리한다."
        ),
        "acceptance_criteria": [
            f"검토한 변경 파일 {len(files)}개 중 무관한 변경 0건을 확인한다."
            if files
            else "변경 파일 목록을 수집하고 무관한 변경 0건을 확인한다."
        ],
    }]
    command = ""
    pass_criteria = ""
    for record in verification_plan:
        if isinstance(record, Mapping):
            command = _clean_text(record.get("command"))
            pass_criteria = _clean_text(record.get("pass_criteria"))
        if command:
            break
    if command:
        steps.append({
            "summary": f"검증 실행 — {command}",
            "description": "검증 계획의 명령을 실행하고 실행 환경과 실제 결과를 Jira에 기록한다.",
            "acceptance_criteria": [pass_criteria or "실패 0건을 확인한다."],
        })
    follow_up = next(
        (text for text in (_clean_text(item) for item in remaining_work) if text), ""
    )
    if follow_up:
        steps.append({
            "summary": "후속 작업 확정",
            "description": follow_up,
            "acceptance_criteria": ["후속 작업의 담당자와 기한을 Jira에 등록한다."],
        })
    return steps[:_MAX_DERIVED_SUBTASKS]


def _build_proposal(
    plan: _PlanItem | None,
    group: _CommitGroup | None,
    global_fields: Mapping[str, list[str]],
    default_project_key: str,
    default_epic_key: str,
) -> dict[str, Any]:
    commits = group.commits if group else ()
    summary = plan.summary if plan else _group_summary(group)  # type: ignore[arg-type]
    jira_key, binding_source = _binding(plan, group)
    project_key = (plan.project_key if plan else "") or _clean_text(default_project_key)
    if not project_key and jira_key:
        project_key = jira_key.split("-", 1)[0]
    epic_key = (plan.epic_key if plan else "") or _jira_key_from_explicit_field(default_epic_key)

    commit_outcomes = [_clean_subject(c.subject) for c in commits]
    files = sorted({path for commit in commits for path in commit.files})
    scopes = sorted({commit.scope for commit in commits if commit.scope})
    commit_refs = _commit_evidence_refs(commits)
    evidence_context = (
        (
            f"커밋 {commit_refs}의 변경 파일 {len(files)}개"
            if files
            else f"커밋 {commit_refs}의 변경 파일 목록 미수집"
        )
        if commits
        else f"계획 항목 '{summary}'"
    )
    problem_parts = list(plan.problem if plan else ()) or list(global_fields.get("problem", []))
    outcome_parts = list(plan.outcome if plan else ()) or list(global_fields.get("outcome", []))
    purpose_parts = list(plan.purpose if plan else ()) or list(global_fields.get("purpose", []))
    scope_parts = list(plan.scope if plan else ()) or list(global_fields.get("scope", []))
    out_of_scope_parts = (
        list(plan.out_of_scope if plan else ()) or list(global_fields.get("out_of_scope", []))
    )
    acceptance = list(plan.acceptance_criteria if plan else ()) or list(global_fields.get("acceptance_criteria", []))
    validation_environment = list(plan.validation_environment if plan else ())
    schedule_rationale = list(plan.schedule_rationale if plan else ())
    epic_rationale = list(plan.epic_rationale if plan else ())
    remaining = list(plan.remaining_work if plan else ()) or list(global_fields.get("remaining_work", []))
    risks = list(plan.risks if plan else ()) or list(global_fields.get("risks", []))

    if not problem_parts:
        problem_parts = [
            f"{evidence_context}에 대한 문제·영향 및 후속 작업이 구조화된 본문으로 기록되지 않았습니다."
        ]
    if not outcome_parts:
        outcome_parts = commit_outcomes or [f"Planned outcome for: {summary}"]
    scope_parts = _unique([*scope_parts, *files, *scopes])
    if not scope_parts:
        scope_parts = [summary]
    acceptance = _acceptance_criteria(
        acceptance,
        summary=summary,
        commit_count=len(commits),
        file_count=len(files),
        scope_count=len(scope_parts),
        evidence_context=evidence_context,
    )
    validation_environment = _unique([
        *validation_environment,
        *(item for commit in commits for item in commit.validation_environment),
    ])
    executed_validation, rejected_validation = parse_validation_evidence([
        *(plan.executed_validation if plan else ()),
        *(item for commit in commits for item in commit.executed_validation),
    ])
    _, verification_plan = parse_validation_evidence([
        *rejected_validation,
        *(plan.verification_plan if plan else ()),
        *(item for commit in commits for item in commit.verification_plan),
    ])
    has_validation_evidence = bool(executed_validation)
    if not verification_plan:
        _, verification_plan = parse_validation_evidence(
            [{
                "command": _default_verification_command(files),
                "environment": (
                    "; ".join(validation_environment)
                    or "프로젝트 CI 기본 환경"
                ),
                "expected_result": "명령이 오류 없이 완료되고 회귀가 발견되지 않는다.",
                "pass_criteria": "실패 0건",
                "status": "not_run",
            }],
            source="generated:verification_plan",
        )
    legacy_validation = _unique([
        *(plan.validation_legacy if plan else ()),
        *(item for commit in commits for item in commit.validation_legacy),
    ])
    if not legacy_validation:
        legacy_validation = [
            f"검증 근거 미기록, 관련 테스트 실행 필요 ({evidence_context})."
        ]
    if not remaining:
        remaining = (
            [f"Execute planned work: {summary}"]
            if not commits
            else [f"{evidence_context}를 검토해 필요한 후속 작업, 담당자, 일정을 확정한다."]
        )
    subtasks = [dict(item) for item in (plan.subtasks if plan else ())]
    if not subtasks and commits:
        subtasks = _derived_subtasks(
            evidence_context=evidence_context,
            files=files,
            verification_plan=verification_plan,
            remaining_work=remaining,
        )
    task_specific_risk = _task_specific_risk(
        summary=summary,
        commits=commits,
        files=files,
        scopes=scopes,
        has_validation_evidence=has_validation_evidence,
        evidence_context=evidence_context,
    )
    risks = _unique([*risks, task_specific_risk])

    estimated_working_days = max(1, min(5, len(commits) or 3))
    if not schedule_rationale:
        schedule_rationale = [
            (
                (
                    f"근거 커밋 {len(commits)}건과 변경 파일 {len(files)}개를 기준으로 "
                    f"검토·검증에 {estimated_working_days}영업일을 산정했다."
                    if files
                    else (
                        f"근거 커밋 {len(commits)}건은 확인했으나 변경 파일 목록이 없어 "
                        f"수집·검토에 {estimated_working_days}영업일을 임시 산정했다."
                    )
                )
            )
            if commits
            else "커밋 근거가 없는 계획 초안이므로 범위 확정과 검증 준비에 3영업일을 임시 산정했다."
        ]
    if not epic_rationale:
        epic_rationale = [
            (
                f"명시적으로 지정된 Epic {epic_key}의 목표 범위에 연결한다."
                if epic_key
                else "Epic 연결 근거가 없어 자동 적용 전에 대상 Epic 확인이 필요하다."
            )
        ]

    source_commits: list[dict[str, Any]] = []
    for commit in sorted(
        commits,
        key=lambda c: (c.sha.casefold(), c.subject.casefold()),
    ):
        source_commit: dict[str, Any] = {
            "sha": commit.sha,
            "subject": commit.subject,
            "url": commit.url,
        }
        if commit.parent_count is not None:
            source_commit["parent_count"] = commit.parent_count
        source_commits.append(source_commit)
    identity = {
        "version": 1,
        "action": "create_task",
        "project_key": project_key,
        "epic_key": epic_key,
        "related_jira_key": jira_key,
        "summary": summary.casefold(),
        "source_shas": [c["sha"].casefold() for c in source_commits],
        # Which task-carrying section the plan item came from is reported on the
        # proposal but deliberately kept out of its identity: moving one action
        # from priority_actions to tasks must not mint a new id.  A new id drops
        # the carried approve/reject status (merge_suggestion_status keys on id)
        # and the Jira-side ARID marker derives from the id too, so a second
        # approval would create a duplicate issue instead of matching the first.
        "plan_source": "plan" if plan else "commit_evidence",
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "id": f"jtp-{digest[:12]}",
        "dedupe_marker": f"autoreport:jira-create-task:v1:{digest[:20]}",
        "action": "create_task",
        "issue_type": "Task",
        "project_key": project_key,
        "epic_key": epic_key,
        "related_jira_key": jira_key,
        "binding_source": binding_source,
        "summary": summary,
        "purpose": "\n".join(_unique(purpose_parts)),
        "problem": "\n".join(_unique(problem_parts)),
        "outcome": "\n".join(_unique(outcome_parts)),
        "scope": scope_parts,
        "out_of_scope": _unique(out_of_scope_parts),
        "subtasks": subtasks,
        "acceptance_criteria": _unique(acceptance),
        # ``validation`` remains as a compatibility view for existing report
        # renderers.  The two authoritative fields below separate observed
        # execution evidence from future work.
        "validation": legacy_validation,
        "executed_validation": executed_validation,
        "verification_plan": verification_plan,
        "validation_environment": validation_environment,
        "remaining_work": _unique(remaining),
        "risks": risks,
        "task_specific_risks": [task_specific_risk],
        "schedule": {
            "estimated_working_days": estimated_working_days,
            "start_rule": "승인 후 다음 영업일",
            "due_rule": f"착수 후 {estimated_working_days}영업일 이내",
        },
        "schedule_rationale": "\n".join(_unique(schedule_rationale)),
        "epic_rationale": "\n".join(_unique(epic_rationale)),
        "source_commits": source_commits,
        "source_files": files,
        "proposal_type": "commit_backed" if commits else "plan_draft",
        "evidence_type": "commit_backed" if commits else "plan_draft",
        "grouping_rationale": (
            group.grouping_rationale
            if group
            else "커밋 근거가 없는 계획 초안으로 별도 관리한다."
        ),
        "create_suppressed": bool(jira_key),
        "plan_source": plan.source_section if plan else "commit_evidence",
    }


def _quality_validation_record(record: Any) -> bool:
    if not isinstance(record, Mapping):
        return False
    command = _clean_text(record.get("command"))
    environment = _clean_text(record.get("environment"))
    actual_value = record.get("actual_result")
    if actual_value in (None, "") and "exit_code" in record:
        actual_value = record.get("exit_code")
    actual_result = re.sub(
        r"\s+", " ", str(actual_value if actual_value is not None else "")
    ).strip()
    source = _clean_text(record.get("source"))
    return bool(
        command
        and environment
        and actual_result
        and source
        and (
            _VALIDATION_RESULT_RE.search(actual_result)
            or re.fullmatch(r"-?\d+", actual_result)
        )
    )


def _quality_verification_plan_record(record: Any) -> bool:
    if not isinstance(record, Mapping):
        return False
    command = _clean_text(record.get("command"))
    environment = _clean_text(record.get("environment"))
    expected_result = _clean_text(record.get("expected_result"))
    pass_criteria = _clean_text(record.get("pass_criteria"))
    status = _clean_text(record.get("status")).casefold()
    environment_is_specific = bool(
        environment
        and not re.search(r"확정\s*필요|미정|unknown|unspecified", environment, re.IGNORECASE)
    )
    return bool(
        command
        and environment_is_specific
        and expected_result
        and pass_criteria
        and status == "not_run"
    )


def _proposal_commit_scopes(source_commits: Sequence[Any]) -> set[str]:
    scopes: set[str] = set()
    for item in source_commits:
        if not isinstance(item, Mapping):
            continue
        subject = _clean_text(item.get("subject"))
        scope = _conventional_scope(subject)
        if scope and not _JIRA_KEY_RE.fullmatch(scope):
            scopes.add(scope)
    return scopes


def _generic_or_non_actionable_commit(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return True
    subject = _clean_text(item.get("subject"))
    normalized = _QUALITY_CONVENTIONAL_PREFIX_RE.sub("", subject, count=1).strip()
    try:
        is_merge_commit = int(item.get("parent_count") or 0) > 1
    except (TypeError, ValueError):
        is_merge_commit = False
    return bool(
        not subject
        or len(subject) < 8
        or is_merge_commit
        or re.match(
            r"^\s*\[(?:wip|tmp|temp|temporary|snapshot)\]",
            subject,
            re.IGNORECASE,
        )
        or re.match(r"^auto[\s-]?commit\b", normalized, re.IGNORECASE)
        or re.match(
            r"^(?:(?:end[\s-]*of[\s-]*day|eod|daily|nightly)\s+)?snapshot\b",
            normalized,
            re.IGNORECASE,
        )
        or re.match(
            r"^(?:wip\b|work\s+in\s+progress\b)",
            normalized,
            re.IGNORECASE,
        )
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
    )


def _risk_is_task_specific(proposal: Mapping[str, Any]) -> bool:
    explicit = _as_text_list(proposal.get("task_specific_risks"))
    if explicit:
        return True
    risks = _as_text_list(proposal.get("risks"))
    anchors = _tokens(
        proposal.get("summary"),
        proposal.get("scope"),
        proposal.get("source_files"),
    )
    return any(bool(_tokens(risk) & anchors) for risk in risks)


def assess_proposal_quality(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic 100-point quality assessment for one proposal.

    The score explains content quality, while ``blocking_reasons`` is the hard
    safety gate.  A high score can never override missing commit evidence,
    unexecuted validation, an existing Jira binding, or plan-only provenance.
    """

    if not isinstance(proposal, Mapping):
        raise TypeError("proposal must be a mapping")

    source_commits = list(proposal.get("source_commits") or [])
    proposal_type = _clean_text(proposal.get("proposal_type")) or (
        "commit_backed" if source_commits else "plan_draft"
    )
    action = _clean_text(proposal.get("action") or proposal.get("type"))
    related_jira_key = _jira_key_from_explicit_field(
        proposal.get("related_jira_key")
    )
    full_shas = bool(source_commits) and all(
        isinstance(item, Mapping)
        and bool(_FULL_GIT_SHA_RE.fullmatch(_clean_text(item.get("sha"))))
        for item in source_commits
    )
    source_files = _as_text_list(proposal.get("source_files"))
    actionable_commits = bool(source_commits) and all(
        not _generic_or_non_actionable_commit(item) for item in source_commits
    )
    proper_commit_count = 1 <= len(source_commits) <= _MAX_AUTO_COMMITS
    scopes = _proposal_commit_scopes(source_commits)
    coherent = len(scopes) <= 1
    grouping_rationale = _clean_text(proposal.get("grouping_rationale"))
    has_core_narrative = all(
        bool(proposal.get(field)) for field in ("problem", "outcome", "scope")
    )

    criteria = _as_text_list(proposal.get("acceptance_criteria"))
    korean_criteria = [item for item in criteria if _KOREAN_RE.search(item)]
    measurable_korean = [
        item for item in criteria if _is_measurable_korean_criterion(item)
    ]
    raw_executed = list(proposal.get("executed_validation") or [])
    complete_validation = [
        item for item in raw_executed if _quality_validation_record(item)
    ]
    executed_path_complete = bool(raw_executed) and (
        len(complete_validation) == len(raw_executed)
    )
    raw_verification_plan = list(proposal.get("verification_plan") or [])
    complete_verification_plan = [
        item
        for item in raw_verification_plan
        if _quality_verification_plan_record(item)
    ]
    planned_path_complete = bool(raw_verification_plan) and (
        len(complete_verification_plan) == len(raw_verification_plan)
    )
    actionable_validation = executed_path_complete or planned_path_complete
    risks = _as_text_list(proposal.get("risks"))
    task_specific_risk = _risk_is_task_specific(proposal)
    schedule_rationale = _clean_text(proposal.get("schedule_rationale"))
    epic_key = _jira_key_from_explicit_field(proposal.get("epic_key"))
    epic_rationale = _clean_text(proposal.get("epic_rationale"))
    project_key = _clean_text(proposal.get("project_key"))

    dimensions: dict[str, dict[str, Any]] = {}

    traceability_score = 0
    traceability_score += 4 if proposal_type == "commit_backed" else 0
    traceability_score += 4 if proper_commit_count else 0
    traceability_score += 4 if full_shas else 0
    traceability_score += 4 if source_files else 0
    traceability_score += 3 if project_key else 0
    traceability_score += 3 if not related_jira_key else 0
    traceability_score += 3 if has_core_narrative else 0
    dimensions["traceability"] = {
        "score": traceability_score,
        "max_score": 25,
        "checks": {
            "commit_backed": proposal_type == "commit_backed",
            "commit_count_1_to_3": proper_commit_count,
            "full_commit_shas": full_shas,
            "changed_file_evidence": bool(source_files),
            "project_key": bool(project_key),
            "no_existing_jira_binding": not bool(related_jira_key),
            "problem_outcome_scope": has_core_narrative,
        },
    }

    coherence_score = (
        (6 if proper_commit_count else 0)
        + (4 if grouping_rationale else 0)
        + (6 if coherent else 0)
        + (4 if actionable_commits else 0)
    )
    dimensions["task_coherence"] = {
        "score": coherence_score,
        "max_score": 20,
        "checks": {
            "commit_count_1_to_3": proper_commit_count,
            "grouping_rationale": bool(grouping_rationale),
            "single_function_scope": coherent,
            "actionable_commit_subjects": actionable_commits,
        },
    }

    acceptance_score = (
        (4 if len(criteria) >= 2 else 0)
        + (6 if len(korean_criteria) >= 2 else 0)
        + (10 if len(measurable_korean) >= 2 else 0)
    )
    dimensions["acceptance_criteria"] = {
        "score": acceptance_score,
        "max_score": 20,
        "checks": {
            "at_least_two": len(criteria) >= 2,
            "at_least_two_korean": len(korean_criteria) >= 2,
            "at_least_two_measurable_korean": len(measurable_korean) >= 2,
        },
    }

    validation_score = (
        (5 if raw_executed or raw_verification_plan else 0)
        + (5 if complete_validation or complete_verification_plan else 0)
        + (5 if actionable_validation else 0)
    )
    dimensions["validation_evidence"] = {
        "score": validation_score,
        "max_score": 15,
        "checks": {
            "executed_evidence_complete": executed_path_complete,
            "not_run_plan_complete": planned_path_complete,
            "actionable_executed_or_planned": actionable_validation,
        },
    }

    risk_score = (3 if risks else 0) + (7 if task_specific_risk else 0)
    dimensions["risk"] = {
        "score": risk_score,
        "max_score": 10,
        "checks": {
            "risk_present": bool(risks),
            "task_specific_risk": task_specific_risk,
        },
    }

    planning_score = (
        (4 if schedule_rationale else 0)
        + (2 if epic_key else 0)
        + (4 if epic_rationale else 0)
    )
    dimensions["planning_context"] = {
        "score": planning_score,
        "max_score": 10,
        "checks": {
            "schedule_rationale": bool(schedule_rationale),
            "epic_key": bool(epic_key),
            "epic_rationale": bool(epic_rationale),
        },
    }

    blocking_reasons: list[str] = []
    if action != "create_task":
        blocking_reasons.append("non_create_action")
    if proposal_type != "commit_backed":
        blocking_reasons.append("plan_draft_requires_human_planning")
    if not source_commits:
        blocking_reasons.append("missing_commit_evidence")
    if source_commits and not proper_commit_count:
        blocking_reasons.append("commit_count_out_of_range")
    if source_commits and not full_shas:
        blocking_reasons.append("non_full_commit_sha")
    if proposal_type == "commit_backed" and not source_files:
        blocking_reasons.append("missing_changed_file_evidence")
    if source_commits and not actionable_commits:
        blocking_reasons.append("generic_or_non_actionable_commit")
    if not coherent:
        blocking_reasons.append("mixed_commit_scope")
    if not grouping_rationale:
        blocking_reasons.append("missing_grouping_rationale")
    if not project_key:
        blocking_reasons.append("missing_project_key")
    if related_jira_key or bool(proposal.get("create_suppressed")):
        blocking_reasons.append("existing_jira_binding_suppresses_create")
    if not has_core_narrative:
        blocking_reasons.append("incomplete_problem_outcome_scope")
    if len(measurable_korean) < 2:
        blocking_reasons.append(
            "insufficient_measurable_korean_acceptance_criteria"
        )
    if not actionable_validation:
        blocking_reasons.append("missing_actionable_validation")
    if not task_specific_risk:
        blocking_reasons.append("missing_task_specific_risk")
    if not schedule_rationale:
        blocking_reasons.append("missing_schedule_rationale")
    if not epic_key or not epic_rationale:
        blocking_reasons.append("missing_epic_alignment")

    quality_score = sum(item["score"] for item in dimensions.values())
    auto_apply_eligible = not blocking_reasons and quality_score >= 85
    quality_grade = (
        "draft"
        if proposal_type == "plan_draft"
        else ("high" if auto_apply_eligible else "manual")
    )
    return {
        "quality_score": quality_score,
        "quality_grade": quality_grade,
        "blocking_reasons": blocking_reasons,
        "auto_apply_eligible": auto_apply_eligible,
        "quality_dimensions": dimensions,
    }


def enrich_proposal_quality(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep-copied proposal with deterministic quality fields merged."""

    if not isinstance(proposal, Mapping):
        raise TypeError("proposal must be a mapping")
    enriched = deepcopy(dict(proposal))
    enriched.update(assess_proposal_quality(enriched))
    return enriched


def build_create_task_proposals(
    commits: Sequence[Mapping[str, Any]],
    plan_sections: Mapping[str, Any] | None,
    *,
    repository_url: str = "",
    default_project_key: str = "",
    default_epic_key: str = "",
) -> list[dict[str, Any]]:
    """Build deterministic, review-only Jira task-creation proposals.

    ``commits`` requires a ``sha``/``hash`` and ``subject``/``message`` for every
    item.  ``plan_sections`` accepts report-style sections such as
    ``priority_actions`` and structured ``tasks`` entries.  A structured task may
    provide ``jira_key``, ``project_key``, ``epic_key``, ``commit_shas``, and any
    of the proposal evidence fields, plus the reviewer-facing narrative:
    ``purpose``, ``out_of_scope``, and a ``subtasks`` breakdown of
    ``{summary, description, acceptance_criteria}`` records.  When both lists
    carry the same summary the structured record wins, so a report can keep its
    flat ``priority_actions`` rendering unchanged.

    Jira binding precedence is explicit plan ``jira_key`` -> explicit commit
    ``jira_key`` -> key parsed from plan text -> key parsed from commit text.
    Conflicting effective keys are never paired.  Every returned proposal has
    ``action == 'create_task'`` and ``issue_type == 'Task'``; this module never
    proposes or performs completion/status transitions.
    """

    if plan_sections is not None and not isinstance(plan_sections, Mapping):
        raise TypeError("plan_sections must be a mapping or None")
    normalized_commits = _normalize_commits(commits, repository_url)
    plans, global_fields = _normalize_plan_items(plan_sections or {})
    groups = _group_commits(normalized_commits, plans)
    pairs, used_plans, used_groups = _pair_plan_and_groups(plans, groups)
    pairs.extend((plan, None) for index, plan in enumerate(plans) if index not in used_plans)
    pairs.extend((None, group) for index, group in enumerate(groups) if index not in used_groups)

    proposals = [
        _build_proposal(
            plan,
            group,
            global_fields,
            default_project_key,
            default_epic_key,
        )
        for plan, group in pairs
    ]
    # Stable across commit/section input ordering, and defensive against duplicate
    # plan entries that normalize to the same proposal identity.
    by_marker = {
        proposal["dedupe_marker"]: enrich_proposal_quality(proposal)
        for proposal in proposals
    }
    return sorted(by_marker.values(), key=lambda p: (p["project_key"], p["summary"].casefold(), p["id"]))
