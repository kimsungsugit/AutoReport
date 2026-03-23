from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from fastapi.testclient import TestClient

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend.main import app


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_once(args: argparse.Namespace) -> Dict[str, Any]:
    client = TestClient(app)
    data: Dict[str, Any] = {
        "source_root": args.source_root,
        "req_paths": args.req_paths,
        "report_dir": args.report_dir,
        "doc_only": "false" if args.full else "true",
        "test_mode": "true" if args.test_mode else "false",
        "ai_enable": "true" if args.ai_enable else "false",
        "expand": "true" if args.expand else "false",
        "ai_detailed": "true" if args.ai_detailed else "false",
        "call_relation_mode": "code",
        "rag_top_k": str(args.rag_top_k),
        "globals_format_with_labels": "true",
    }
    files = None
    if args.template:
        tpl_path = Path(args.template)
        files = {
            "template_file": (
                tpl_path.name,
                tpl_path.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        }
    res = client.post("/api/local/uds/generate", data=data, files=files)
    payload = {"status_code": res.status_code}
    try:
        payload["response"] = res.json()
    except Exception:
        payload["response"] = {"raw": res.text}
    payload["executed_at"] = datetime.now().isoformat(timespec="seconds")
    payload["input_fingerprint"] = _fingerprint_inputs(args)
    return payload


def _fingerprint_inputs(args: argparse.Namespace) -> str:
    raw = {
        "source_root": str(args.source_root or ""),
        "req_paths": str(args.req_paths or ""),
        "report_dir": str(args.report_dir or ""),
        "template": str(args.template or ""),
        "test_mode": bool(args.test_mode),
        "full": bool(args.full),
        "ai_enable": bool(args.ai_enable),
        "expand": bool(args.expand),
        "ai_detailed": bool(args.ai_detailed),
        "rag_top_k": int(args.rag_top_k or 12),
    }
    text = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _compare(prev: Dict[str, Any], cur: Dict[str, Any]) -> Dict[str, Any]:
    p_gate = (((prev.get("response") or {}).get("quick_quality_gate") or {}).get("rates") or {})
    c_gate = (((cur.get("response") or {}).get("quick_quality_gate") or {}).get("rates") or {})
    p_counts = (((prev.get("response") or {}).get("quick_quality_gate") or {}).get("counts") or {})
    c_counts = (((cur.get("response") or {}).get("quick_quality_gate") or {}).get("counts") or {})
    keys = [
        "called_fill",
        "calling_fill",
        "input_fill",
        "output_fill",
        "global_fill",
        "static_fill",
        "description_fill",
        "asil_fill",
        "related_fill",
        "description_trusted_fill",
        "asil_trusted_fill",
        "related_trusted_fill",
    ]
    delta: Dict[str, Any] = {}
    for key in keys:
        p = float(p_gate.get(key) or 0.0)
        c = float(c_gate.get(key) or 0.0)
        delta[key] = {"prev": p, "cur": c, "delta": round(c - p, 1)}
    prev_codes = set((((prev.get("response") or {}).get("quality_evaluation") or {}).get("reason_codes") or []))
    cur_codes = set((((cur.get("response") or {}).get("quality_evaluation") or {}).get("reason_codes") or []))
    soft_fail_reasons = []
    hard_fail_reasons = []
    for metric_name, row in delta.items():
        if float(row.get("delta") or 0.0) < -3.0:
            soft_fail_reasons.append(f"REGRESSION_{metric_name.upper()}")
    if len(cur_codes - prev_codes) > 0:
        soft_fail_reasons.append("NEW_REASON_CODES")
    cur_status = int(cur.get("status_code") or 0)
    cur_total = int(c_counts.get("total_functions") or 0)
    if cur_status != 200:
        hard_fail_reasons.append("STATUS_NOT_200")
    if cur_total <= 0:
        hard_fail_reasons.append("NO_FUNCTIONS")
    prev_fp = str(prev.get("input_fingerprint") or "")
    cur_fp = str(cur.get("input_fingerprint") or "")
    return {
        "prev_status": prev.get("status_code"),
        "cur_status": cur_status,
        "prev_gate_pass": (((prev.get("response") or {}).get("quality_evaluation") or {}).get("gate_pass")),
        "cur_gate_pass": (((cur.get("response") or {}).get("quality_evaluation") or {}).get("gate_pass")),
        "input_fingerprint_match": bool(prev_fp and cur_fp and prev_fp == cur_fp),
        "counts": {
            "prev": p_counts,
            "cur": c_counts,
        },
        "rates": delta,
        "reason_codes": {
            "prev": sorted(prev_codes),
            "cur": sorted(cur_codes),
            "added": sorted(cur_codes - prev_codes),
            "removed": sorted(prev_codes - cur_codes),
        },
        "hard_fail": bool(len(hard_fail_reasons) > 0),
        "hard_fail_reasons": hard_fail_reasons,
        "soft_fail": bool(len(soft_fail_reasons) > 0),
        "soft_fail_reasons": soft_fail_reasons,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="UDS quality baseline/compare/repro runner")
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--req-paths", required=True, help="newline/comma separated requirement paths")
    ap.add_argument("--report-dir", default="reports")
    ap.add_argument("--template", default="")
    ap.add_argument("--test-mode", action="store_true")
    ap.add_argument("--full", action="store_true", help="generate extra reports (doc_only=false)")
    ap.add_argument("--ai-enable", action="store_true")
    ap.add_argument("--expand", action="store_true")
    ap.add_argument("--ai-detailed", action="store_true")
    ap.add_argument("--rag-top-k", type=int, default=12)
    ap.add_argument("--baseline-out", default="reports/uds_local/quality_baseline.json")
    ap.add_argument("--run-out", default="reports/uds_local/quality_run.json")
    ap.add_argument("--compare-out", default="reports/uds_local/quality_compare.json")
    args = ap.parse_args()

    run = _run_once(args)
    run_out = Path(args.run_out)
    _save_json(run_out, run)

    baseline_out = Path(args.baseline_out)
    if not baseline_out.exists():
        _save_json(baseline_out, run)
        print(f"[baseline] created: {baseline_out}")
        return

    prev = _load_json(baseline_out)
    cmp_data = _compare(prev, run)
    if not bool(cmp_data.get("input_fingerprint_match")):
        cmp_data["soft_fail"] = True
        reasons = list(cmp_data.get("soft_fail_reasons") or [])
        if "INPUT_FINGERPRINT_MISMATCH" not in reasons:
            reasons.append("INPUT_FINGERPRINT_MISMATCH")
        cmp_data["soft_fail_reasons"] = reasons
    _save_json(Path(args.compare_out), cmp_data)
    print(f"[run] saved: {run_out}")
    print(f"[compare] saved: {args.compare_out}")
    print(json.dumps(cmp_data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
