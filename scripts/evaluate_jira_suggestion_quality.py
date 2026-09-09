"""Read-only cohort quality report for generated Jira suggestions.

The evaluator never imports a Jira client and never mutates suggestion files.  It
delegates proposal-level scoring to ``workflow.jira_planning`` and aggregates the
stable cohort KPIs used by the Jira suggestion quality-improvement plan.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
AUTOREPORT_ROOT = SCRIPT_DIR.parent
if str(AUTOREPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOREPORT_ROOT))

EXIT_OK = 0
EXIT_THRESHOLD_FAILED = 1
EXIT_ERROR = 2
SCHEMA_VERSION = "jira-suggestion-quality-report/v1"

Assessor = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def _default_assessor(proposal: Mapping[str, Any]) -> Mapping[str, Any]:
    """Import the shared validator lazily so this module remains test-injectable."""

    from workflow.jira_planning import assess_proposal_quality

    return assess_proposal_quality(proposal)


@dataclass(frozen=True)
class SuggestionRecord:
    """A suggestion and the file/index that supplied it."""

    source: str
    index: int
    proposal: Mapping[str, Any]


@dataclass(frozen=True)
class Thresholds:
    """Optional cohort acceptance thresholds.

    Rates use the inclusive 0..1 range.  ``minimum_average_score_10`` uses the
    user-facing 0..10 scale.
    """

    minimum_approval_ready_rate: float | None = None
    minimum_average_score_10: float | None = None
    minimum_evidence_precision: float | None = None
    minimum_grain_correctness_proxy: float | None = None
    maximum_guardrail_violations: int | None = None


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, frozenset, Mapping)):
        return bool(value)
    return True


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set, frozenset)):
        return list(value)
    return [value]


def _reason_name(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("code", "reason", "name", "message"):
            if _nonempty(value.get(key)):
                return str(value[key]).strip()
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value).strip()


def _reason_list(value: Any) -> list[str]:
    return sorted({_reason_name(item) for item in _as_list(value) if _reason_name(item)})


def _assessment_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        converted = asdict(value)
        if isinstance(converted, Mapping):
            return converted
    raise TypeError("assess_proposal_quality() must return a mapping or dataclass")


def _quality_score(assessment: Mapping[str, Any], proposal: Mapping[str, Any]) -> float:
    value = assessment.get("quality_score", proposal.get("quality_score"))
    if value is None:
        score_10 = assessment.get("score_10", proposal.get("score_10"))
        if score_10 is not None:
            value = float(score_10) * 10.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("quality assessment requires numeric quality_score")
    score = float(value)
    if not 0.0 <= score <= 100.0:
        raise ValueError(f"quality_score must be between 0 and 100, got {score}")
    return score


def _evidence_type(assessment: Mapping[str, Any], proposal: Mapping[str, Any]) -> str:
    value = str(
        assessment.get("evidence_type")
        or proposal.get("evidence_type")
        or ("commit_backed" if _as_list(proposal.get("source_commits")) else "plan_draft")
    ).strip()
    if value not in {"commit_backed", "plan_draft"}:
        raise ValueError(f"unsupported evidence_type: {value!r}")
    return value


def _auto_apply_eligible(
    assessment: Mapping[str, Any], proposal: Mapping[str, Any]
) -> bool:
    value = assessment.get(
        "auto_apply_eligible",
        assessment.get("approval_ready", proposal.get("auto_apply_eligible", False)),
    )
    return value is True


def _evidence_counts(proposal: Mapping[str, Any]) -> tuple[int, int]:
    """Return (valid, candidate) counts for structured executed evidence."""

    candidates = _as_list(proposal.get("executed_validation"))
    valid = 0
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        has_result = _nonempty(item.get("actual_result")) or _nonempty(item.get("exit_code"))
        if (
            _nonempty(item.get("command"))
            and _nonempty(item.get("environment"))
            and has_result
            and _nonempty(item.get("source"))
        ):
            valid += 1
    return valid, len(candidates)


_GRAIN_BLOCKING_MARKERS = (
    "mixed_scope",
    "mixed-scope",
    "mixed scope",
    "unrelated",
    "uncohesive",
    "scope_mixed",
    "commit_group_mixed",
    "too_many_commits",
    "3개 초과",
    "무관",
    "혼합 범위",
)


def _grain_correct(source_commit_count: int, blocking_reasons: Sequence[str]) -> bool:
    if not 1 <= source_commit_count <= 3:
        return False
    normalized = "\n".join(blocking_reasons).casefold()
    return not any(marker.casefold() in normalized for marker in _GRAIN_BLOCKING_MARKERS)


def _explicit_guardrail_names(
    proposal: Mapping[str, Any], assessment: Mapping[str, Any]
) -> list[str]:
    result: list[str] = []
    for container in (proposal, assessment):
        for item in _as_list(container.get("guardrail_violations")):
            name = _reason_name(item)
            if name:
                result.append(f"explicit:{name}")
    return result


def _proposal_guardrails(
    proposal: Mapping[str, Any],
    assessment: Mapping[str, Any],
    *,
    evidence_type: str,
    auto_apply_eligible: bool,
) -> list[str]:
    violations = _explicit_guardrail_names(proposal, assessment)
    if evidence_type == "plan_draft" and auto_apply_eligible:
        violations.append("plan_draft_auto_apply_eligible")
    if _nonempty(proposal.get("task_key")) and auto_apply_eligible:
        violations.append("existing_jira_key_auto_apply_eligible")

    proposal_type = str(proposal.get("type") or "").strip().casefold()
    action = str(proposal.get("action") or "").strip().casefold()
    allowed = {"", "create_task", "create-task"}
    if proposal_type not in allowed or action not in allowed:
        violations.append("unsafe_non_create_action")
    if any(_nonempty(proposal.get(key)) for key in ("transition", "status_transition", "target_status")):
        violations.append("status_transition_requested")
    if str(proposal.get("sprint_state") or "").strip().casefold() in {
        "closed",
        "complete",
        "completed",
        "expired",
    }:
        violations.append("expired_sprint_target")
    if not _nonempty(proposal.get("dedupe_marker")):
        violations.append("missing_dedupe_marker")
    if not _nonempty(proposal.get("proposal_revision")):
        violations.append("missing_proposal_revision")
    return sorted(set(violations))


def load_suggestion_files(paths: Sequence[str | Path]) -> list[SuggestionRecord]:
    """Load one or more JSON files without modifying them.

    Accepted roots are ``{"suggestions": [...]}`` and a bare suggestion list.
    Paths are sorted and duplicate path arguments are evaluated once so output is
    stable regardless of argument order.
    """

    unique_paths = sorted({Path(path).resolve() for path in paths}, key=lambda p: str(p).casefold())
    if not unique_paths:
        raise ValueError("at least one suggestions JSON path is required")

    records: list[SuggestionRecord] = []
    for path in unique_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"cannot read suggestions file {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {exc}") from exc

        suggestions = payload.get("suggestions") if isinstance(payload, Mapping) else payload
        if not isinstance(suggestions, list):
            raise ValueError(f"{path} must contain a suggestions list")
        for index, proposal in enumerate(suggestions):
            if not isinstance(proposal, Mapping):
                raise ValueError(f"{path}: suggestions[{index}] must be an object")
            records.append(SuggestionRecord(source=str(path), index=index, proposal=proposal))
    return records


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def build_quality_report(
    records: Sequence[SuggestionRecord],
    *,
    assessor: Assessor = _default_assessor,
    thresholds: Thresholds | None = None,
) -> dict[str, Any]:
    """Assess proposals and return a deterministic cohort report."""

    proposal_rows: list[dict[str, Any]] = []
    blocking_counts: Counter[str] = Counter()
    guardrail_counts: Counter[str] = Counter()
    dedupe_markers: Counter[str] = Counter()
    quality_scores: list[float] = []
    evidence_valid = 0
    evidence_candidates = 0
    commit_backed = 0
    plan_draft = 0
    approval_ready = 0
    grain_correct = 0

    ordered_records = sorted(
        records,
        key=lambda record: (
            record.source.casefold(),
            str(record.proposal.get("id") or "").casefold(),
            record.index,
        ),
    )
    for record in ordered_records:
        proposal = record.proposal
        assessment = _assessment_mapping(assessor(proposal))
        evidence_type = _evidence_type(assessment, proposal)
        score = _quality_score(assessment, proposal)
        grade = str(
            assessment.get("quality_grade")
            or proposal.get("quality_grade")
            or ("auto" if score >= 85 else "review" if score >= 70 else "draft")
        ).strip()
        reasons = _reason_list(
            assessment.get("blocking_reasons", proposal.get("blocking_reasons"))
        )
        eligible = _auto_apply_eligible(assessment, proposal)
        valid_count, candidate_count = _evidence_counts(proposal)
        source_commit_count = len(_as_list(proposal.get("source_commits")))
        grain_value: bool | None = None

        if evidence_type == "commit_backed":
            commit_backed += 1
            if eligible:
                approval_ready += 1
            grain_value = _grain_correct(source_commit_count, reasons)
            if grain_value:
                grain_correct += 1
        else:
            plan_draft += 1

        quality_scores.append(score)
        evidence_valid += valid_count
        evidence_candidates += candidate_count
        blocking_counts.update(reasons)
        violations = _proposal_guardrails(
            proposal,
            assessment,
            evidence_type=evidence_type,
            auto_apply_eligible=eligible,
        )
        guardrail_counts.update(violations)
        marker = str(proposal.get("dedupe_marker") or "").strip()
        if marker:
            dedupe_markers[marker] += 1

        proposal_rows.append(
            {
                "source": record.source,
                "index": record.index,
                "id": str(proposal.get("id") or f"suggestion-{record.index}"),
                "evidence_type": evidence_type,
                "quality_score": round(score, 2),
                "quality_score_10": round(score / 10.0, 2),
                "quality_grade": grade,
                "auto_apply_eligible": eligible,
                "blocking_reasons": reasons,
                "evidence_valid_count": valid_count,
                "evidence_candidate_count": candidate_count,
                "grain_correctness_proxy": grain_value,
                "guardrail_violations": violations,
            }
        )

    duplicate_count = sum(count - 1 for count in dedupe_markers.values() if count > 1)
    if duplicate_count:
        guardrail_counts["duplicate_dedupe_marker"] += duplicate_count

    suggestion_count = len(proposal_rows)
    average_score_10 = (
        round(sum(quality_scores) / suggestion_count / 10.0, 2) if suggestion_count else None
    )
    cohort = {
        "suggestion_count": suggestion_count,
        "commit_backed_count": commit_backed,
        "plan_draft_count": plan_draft,
        "approval_ready_count": approval_ready,
        "approval_ready_denominator": commit_backed,
        "approval_ready_rate": _rate(approval_ready, commit_backed),
        "average_quality_score_10": average_score_10,
        "evidence_valid_count": evidence_valid,
        "evidence_candidate_count": evidence_candidates,
        "evidence_precision": _rate(evidence_valid, evidence_candidates),
        "grain_correct_count": grain_correct,
        "grain_denominator": commit_backed,
        "grain_correctness_proxy": _rate(grain_correct, commit_backed),
        "blocking_reason_counts": dict(sorted(blocking_counts.items())),
        "guardrail_counts": dict(sorted(guardrail_counts.items())),
        "guardrail_violation_count": sum(guardrail_counts.values()),
    }
    configured = thresholds or Thresholds()
    threshold_result = evaluate_thresholds(cohort, configured)
    return {
        "schema_version": SCHEMA_VERSION,
        "sources": sorted({record.source for record in records}, key=str.casefold),
        "cohort": cohort,
        "thresholds": {
            key: value for key, value in asdict(configured).items() if value is not None
        },
        "threshold_result": threshold_result,
        "suggestions": proposal_rows,
    }


def evaluate_thresholds(
    cohort: Mapping[str, Any], thresholds: Thresholds
) -> dict[str, Any]:
    """Evaluate configured thresholds; an unavailable metric fails closed."""

    evaluated = any(value is not None for value in asdict(thresholds).values())
    if not evaluated:
        return {"evaluated": False, "passed": None, "failures": []}

    failures: list[dict[str, Any]] = []
    minimum_checks = (
        (
            "approval_ready_rate",
            cohort.get("approval_ready_rate"),
            thresholds.minimum_approval_ready_rate,
        ),
        (
            "average_quality_score_10",
            cohort.get("average_quality_score_10"),
            thresholds.minimum_average_score_10,
        ),
        (
            "evidence_precision",
            cohort.get("evidence_precision"),
            thresholds.minimum_evidence_precision,
        ),
        (
            "grain_correctness_proxy",
            cohort.get("grain_correctness_proxy"),
            thresholds.minimum_grain_correctness_proxy,
        ),
    )
    for metric, actual, threshold in minimum_checks:
        if threshold is not None and (actual is None or float(actual) < threshold):
            failures.append(
                {"metric": metric, "actual": actual, "operator": ">=", "threshold": threshold}
            )

    maximum = thresholds.maximum_guardrail_violations
    actual_guardrails = int(cohort.get("guardrail_violation_count") or 0)
    if maximum is not None and actual_guardrails > maximum:
        failures.append(
            {
                "metric": "guardrail_violation_count",
                "actual": actual_guardrails,
                "operator": "<=",
                "threshold": maximum,
            }
        )
    return {"evaluated": True, "passed": not failures, "failures": failures}


def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _display_rate(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"


def _markdown_count_table(title: str, values: Mapping[str, Any]) -> list[str]:
    lines = [f"## {title}", "", "| Name | Count |", "|---|---:|"]
    if values:
        for name, count in sorted(values.items()):
            safe_name = str(name).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {safe_name} | {count} |")
    else:
        lines.append("| None | 0 |")
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    cohort = report["cohort"]
    lines = [
        "# Jira Suggestion Quality Report",
        "",
        f"Schema: `{report['schema_version']}`",
        "",
        "## Sources",
        "",
    ]
    lines.extend(f"- `{source}`" for source in report.get("sources", []))
    lines.extend(
        [
            "",
            "## Cohort KPI",
            "",
            "| KPI | Value |",
            "|---|---:|",
            f"| Suggestions | {cohort['suggestion_count']} |",
            f"| Commit-backed | {cohort['commit_backed_count']} |",
            f"| Plan drafts | {cohort['plan_draft_count']} |",
            f"| Approval-ready rate | {_display_rate(cohort['approval_ready_rate'])} |",
            f"| Average quality score | {cohort['average_quality_score_10'] if cohort['average_quality_score_10'] is not None else 'n/a'}/10 |",
            f"| Evidence precision | {_display_rate(cohort['evidence_precision'])} |",
            f"| Grain correctness proxy | {_display_rate(cohort['grain_correctness_proxy'])} |",
            f"| Guardrail violations | {cohort['guardrail_violation_count']} |",
            "",
        ]
    )
    lines.extend(_markdown_count_table("Blocking reasons", cohort["blocking_reason_counts"]))
    lines.append("")
    lines.extend(_markdown_count_table("Guardrail violations", cohort["guardrail_counts"]))
    lines.extend(["", "## Threshold verdict", ""])
    threshold_result = report["threshold_result"]
    if not threshold_result["evaluated"]:
        lines.append("NOT ENFORCED")
    elif threshold_result["passed"]:
        lines.append("PASS")
    else:
        lines.append("FAIL")
        lines.append("")
        for failure in threshold_result["failures"]:
            lines.append(
                f"- `{failure['metric']}`: {failure['actual']} "
                f"{failure['operator']} {failure['threshold']}"
            )
    return "\n".join(lines).rstrip() + "\n"


def _rate_argument(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("rate must be between 0 and 1")
    return parsed


def _score_argument(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 10.0:
        raise argparse.ArgumentTypeError("score must be between 0 and 10")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate Jira suggestion JSON files without writing to Jira."
    )
    parser.add_argument("inputs", nargs="+", help="One or more Jira suggestions JSON files.")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", help="Optional report file; stdout is used when omitted.")
    parser.add_argument(
        "--enforce-plan-targets",
        action="store_true",
        help="Require 0.80 approval rate, 8.0/10 mean, 0.85 evidence precision, 0.95 grain proxy, and zero guardrail violations.",
    )
    parser.add_argument("--min-approval-ready-rate", type=_rate_argument)
    parser.add_argument("--min-average-score-10", type=_score_argument)
    parser.add_argument("--min-evidence-precision", type=_rate_argument)
    parser.add_argument("--min-grain-correctness", type=_rate_argument)
    parser.add_argument("--max-guardrail-violations", type=int)
    return parser.parse_args(argv)


def _thresholds_from_args(args: argparse.Namespace) -> Thresholds:
    defaults = Thresholds(
        minimum_approval_ready_rate=0.80,
        minimum_average_score_10=8.0,
        minimum_evidence_precision=0.85,
        minimum_grain_correctness_proxy=0.95,
        maximum_guardrail_violations=0,
    ) if args.enforce_plan_targets else Thresholds()
    maximum = args.max_guardrail_violations
    if maximum is not None and maximum < 0:
        raise ValueError("max guardrail violations cannot be negative")
    return Thresholds(
        minimum_approval_ready_rate=(
            args.min_approval_ready_rate
            if args.min_approval_ready_rate is not None
            else defaults.minimum_approval_ready_rate
        ),
        minimum_average_score_10=(
            args.min_average_score_10
            if args.min_average_score_10 is not None
            else defaults.minimum_average_score_10
        ),
        minimum_evidence_precision=(
            args.min_evidence_precision
            if args.min_evidence_precision is not None
            else defaults.minimum_evidence_precision
        ),
        minimum_grain_correctness_proxy=(
            args.min_grain_correctness
            if args.min_grain_correctness is not None
            else defaults.minimum_grain_correctness_proxy
        ),
        maximum_guardrail_violations=(
            maximum if maximum is not None else defaults.maximum_guardrail_violations
        ),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    assessor: Assessor = _default_assessor,
) -> int:
    args = parse_args(argv)
    try:
        thresholds = _thresholds_from_args(args)
        records = load_suggestion_files(args.inputs)
        report = build_quality_report(records, assessor=assessor, thresholds=thresholds)
        rendered = render_json(report) if args.format == "json" else render_markdown(report)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
    except (ImportError, OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return (
        EXIT_THRESHOLD_FAILED
        if report["threshold_result"]["passed"] is False
        else EXIT_OK
    )


if __name__ == "__main__":
    raise SystemExit(main())
