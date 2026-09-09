from __future__ import annotations

import json
from pathlib import Path

from scripts import evaluate_jira_suggestion_quality as quality_report


FIXTURE = Path(__file__).parent / "fixtures" / "jira_quality" / "synthetic-suggestions.json"
GUARDRAIL_FIXTURE = (
    Path(__file__).parent / "fixtures" / "jira_quality" / "synthetic-guardrail-suggestions.json"
)


def _fixture_assessor(proposal):
    return proposal["expected_assessment"]


def test_synthetic_cohort_kpis_are_deterministic_and_use_defined_denominators():
    records = quality_report.load_suggestion_files([FIXTURE])

    first = quality_report.build_quality_report(records, assessor=_fixture_assessor)
    second = quality_report.build_quality_report(records, assessor=_fixture_assessor)

    assert first == second
    assert quality_report.render_json(first) == quality_report.render_json(second)
    cohort = first["cohort"]
    assert cohort == {
        "suggestion_count": 3,
        "commit_backed_count": 2,
        "plan_draft_count": 1,
        "approval_ready_count": 1,
        "approval_ready_denominator": 2,
        "approval_ready_rate": 0.5,
        "average_quality_score_10": 6.0,
        "evidence_valid_count": 2,
        "evidence_candidate_count": 4,
        "evidence_precision": 0.5,
        "grain_correct_count": 1,
        "grain_denominator": 2,
        "grain_correctness_proxy": 0.5,
        "blocking_reason_counts": {
            "missing_acceptance_criteria": 1,
            "mixed_scope": 1,
            "plan_only": 1,
        },
        "guardrail_counts": {},
        "guardrail_violation_count": 0,
    }
    assert [row["id"] for row in first["suggestions"]] == [
        "good-commit-backed",
        "mixed-commit-backed",
        "plan-only",
    ]


def test_cli_returns_nonzero_when_a_configured_threshold_is_missed(capsys):
    exit_code = quality_report.main(
        [str(FIXTURE), "--min-average-score-10", "8.0"],
        assessor=_fixture_assessor,
    )

    assert exit_code == quality_report.EXIT_THRESHOLD_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert payload["threshold_result"] == {
        "evaluated": True,
        "passed": False,
        "failures": [
            {
                "metric": "average_quality_score_10",
                "actual": 6.0,
                "operator": ">=",
                "threshold": 8.0,
            }
        ],
    }


def test_cli_passes_at_boundary_and_markdown_contains_cohort_sections(capsys):
    exit_code = quality_report.main(
        [
            str(FIXTURE),
            "--format",
            "markdown",
            "--min-approval-ready-rate",
            "0.5",
            "--min-average-score-10",
            "6.0",
            "--min-evidence-precision",
            "0.5",
            "--min-grain-correctness",
            "0.5",
            "--max-guardrail-violations",
            "0",
        ],
        assessor=_fixture_assessor,
    )

    output = capsys.readouterr().out
    assert exit_code == quality_report.EXIT_OK
    assert "| Approval-ready rate | 50.0% |" in output
    assert "| Average quality score | 6.0/10 |" in output
    assert "## Blocking reasons" in output
    assert "## Guardrail violations" in output
    assert output.endswith("PASS\n")


def test_missing_evidence_metric_fails_closed_when_threshold_is_requested():
    cohort = {
        "approval_ready_rate": None,
        "average_quality_score_10": None,
        "evidence_precision": None,
        "grain_correctness_proxy": None,
        "guardrail_violation_count": 0,
    }

    result = quality_report.evaluate_thresholds(
        cohort,
        quality_report.Thresholds(minimum_evidence_precision=0.85),
    )

    assert result["passed"] is False
    assert result["evaluated"] is True
    assert result["failures"][0]["metric"] == "evidence_precision"


def test_guardrail_counts_detect_unsafe_plan_draft_and_duplicate_marker():
    records = quality_report.load_suggestion_files([FIXTURE, GUARDRAIL_FIXTURE])
    report = quality_report.build_quality_report(
        records,
        assessor=_fixture_assessor,
        thresholds=quality_report.Thresholds(maximum_guardrail_violations=0),
    )

    assert report["cohort"]["suggestion_count"] == 4
    assert report["cohort"]["guardrail_counts"] == {
        "duplicate_dedupe_marker": 1,
        "missing_proposal_revision": 1,
        "plan_draft_auto_apply_eligible": 1,
        "status_transition_requested": 1,
    }
    assert report["cohort"]["guardrail_violation_count"] == 4
    assert report["threshold_result"]["passed"] is False
