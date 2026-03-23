from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent
REPORTS_ROOT = WORKSPACE_ROOT / "reports"
PROJECTS_ROOT = REPORTS_ROOT / "projects"
AUTOMATION_ROOT = REPORTS_ROOT / "automation_status"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate cross-project history dashboard from existing reports.")
    parser.add_argument("--date", default=None, help="Reference date YYYY-MM-DD")
    return parser.parse_args()


def parse_meta(markdown_path: Path) -> dict[str, str]:
    text = markdown_path.read_text(encoding="utf-8", errors="replace")
    meta: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^-\s*([^:]+):\s*`?(.+?)`?$", line.strip())
        if match:
            meta[match.group(1).strip()] = match.group(2).strip()
        if line.startswith("## "):
            break
    return meta


def parse_section_items(markdown_path: Path, section_title: str, limit: int = 3) -> list[str]:
    text = markdown_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    items: list[str] = []
    in_section = False
    for raw in lines:
        line = raw.strip()
        if line == f"## {section_title}":
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and line.startswith("- "):
            items.append(line[2:].strip())
    return items[:limit]


def infer_report_date(path: Path) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    return match.group(1) if match else ""


def load_automation_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = defaultdict(dict)
    if not AUTOMATION_ROOT.exists():
        return index
    for json_path in sorted(AUTOMATION_ROOT.glob("*-auto-commit-push.json")):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        day = str(payload.get("date") or infer_report_date(json_path))
        html_path = json_path.with_suffix(".html")
        for item in payload.get("projects") or []:
            name = str(item.get("name") or "")
            if not name:
                continue
            index[name][day] = {
                "status": str(item.get("status") or ""),
                "message": str(item.get("message") or ""),
                "commit": str(item.get("commit") or ""),
                "changed_files": int(item.get("changed_files") or 0),
                "html_link": html_path.as_uri() if html_path.exists() else "",
            }
    return index


def collect_project_history() -> dict[str, list[dict[str, Any]]]:
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not PROJECTS_ROOT.exists():
        return history

    for project_dir in sorted(p for p in PROJECTS_ROOT.iterdir() if p.is_dir()):
        project_name = project_dir.name
        daily_dir = project_dir / "reports" / "daily_brief"
        weekly_dir = project_dir / "reports" / "weekly_brief"
        monthly_dir = project_dir / "reports" / "monthly_brief"
        jira_dir = project_dir / "reports" / "jira"
        dashboard_dir = project_dir / "reports" / "dashboard"

        for md_path in sorted(daily_dir.glob("*-daily-report.md")):
            run_date = infer_report_date(md_path)
            html_path = md_path.with_suffix(".html")
            meta = parse_meta(md_path)
            history[project_name].append(
                {
                    "date": run_date,
                    "type": "daily",
                    "title": md_path.read_text(encoding="utf-8", errors="replace").splitlines()[0].lstrip("# ").strip(),
                    "work_type": meta.get("작업 유형") or meta.get("Work Type") or "",
                    "profile": meta.get("분석 프로필") or meta.get("Domain Profile") or "",
                    "commits": int((meta.get("커밋 수") or "0").strip("`")),
                    "files": int((meta.get("변경 파일 수") or "0").strip("`")),
                    "summary": parse_section_items(md_path, "핵심 요약", 2),
                    "source_changes": parse_section_items(md_path, "소스 기반 핵심 변경", 2),
                    "html_link": html_path.as_uri() if html_path.exists() else "",
                    "md_link": md_path.as_uri(),
                }
            )

        for md_path in sorted(weekly_dir.glob("*-weekly-report.md")):
            run_date = infer_report_date(md_path)
            html_path = md_path.with_suffix(".html")
            history[project_name].append(
                {
                    "date": run_date,
                    "type": "weekly",
                    "title": md_path.read_text(encoding="utf-8", errors="replace").splitlines()[0].lstrip("# ").strip(),
                    "summary": parse_section_items(md_path, "주간 요약", 2),
                    "html_link": html_path.as_uri() if html_path.exists() else "",
                    "md_link": md_path.as_uri(),
                }
            )

        for md_path in sorted(monthly_dir.glob("*-monthly-report.md")):
            run_date = infer_report_date(md_path)
            html_path = md_path.with_suffix(".html")
            history[project_name].append(
                {
                    "date": run_date,
                    "type": "monthly",
                    "title": md_path.read_text(encoding="utf-8", errors="replace").splitlines()[0].lstrip("# ").strip(),
                    "summary": parse_section_items(md_path, "월간 요약", 2),
                    "html_link": html_path.as_uri() if html_path.exists() else "",
                    "md_link": md_path.as_uri(),
                }
            )

        for md_path in sorted(jira_dir.glob("*-jira-plan.md")):
            run_date = infer_report_date(md_path)
            html_path = md_path.with_suffix(".html")
            history[project_name].append(
                {
                    "date": run_date,
                    "type": "jira_plan",
                    "title": md_path.read_text(encoding="utf-8", errors="replace").splitlines()[0].lstrip("# ").strip(),
                    "summary": parse_section_items(md_path, "Summary", 1),
                    "html_link": html_path.as_uri() if html_path.exists() else "",
                    "md_link": md_path.as_uri(),
                }
            )

        for html_path in sorted(dashboard_dir.glob("*-startup-dashboard.html")):
            run_date = infer_report_date(html_path)
            history[project_name].append(
                {
                    "date": run_date,
                    "type": "dashboard",
                    "title": html_path.name,
                    "html_link": html_path.as_uri(),
                    "md_link": "",
                }
            )
    for items in history.values():
        items.sort(key=lambda item: (item.get("date", ""), item.get("type", "")), reverse=True)
    return history


def build_overview_cards(project_history: dict[str, list[dict[str, Any]]], automation_index: dict[str, dict[str, Any]]) -> str:
    cards = []
    for project, items in sorted(project_history.items()):
        latest_daily = next((item for item in items if item["type"] == "daily"), {})
        latest_weekly = next((item for item in items if item["type"] == "weekly"), {})
        latest_monthly = next((item for item in items if item["type"] == "monthly"), {})
        latest_auto = {}
        if project in automation_index and automation_index[project]:
            latest_day = sorted(automation_index[project].keys())[-1]
            latest_auto = automation_index[project][latest_day]
        summary = "".join(f"<li>{escape(x)}</li>" for x in latest_daily.get("summary", [])[:2]) or "<li>최근 일일 요약 없음</li>"
        cards.append(
            f"""
<section class="overview-card">
  <div class="overview-head">
    <div>
      <h2>{escape(project)}</h2>
      <p>{escape(str(latest_daily.get('work_type') or '작업 유형 없음'))}</p>
    </div>
    <span class="mini-status">{escape(str(latest_auto.get('status') or 'no_status'))}</span>
  </div>
  <div class="stats">
    <div><span>Latest Daily</span><strong>{escape(str(latest_daily.get('date') or '-'))}</strong></div>
    <div><span>Commits</span><strong>{escape(str(latest_daily.get('commits') or 0))}</strong></div>
    <div><span>Files</span><strong>{escape(str(latest_daily.get('files') or 0))}</strong></div>
    <div><span>Automation</span><strong>{escape(str(latest_auto.get('status') or '-'))}</strong></div>
  </div>
  <ul>{summary}</ul>
  <div class="links">
    {f'<a href="{escape(str(latest_daily.get("html_link") or ""))}">Daily</a>' if latest_daily.get("html_link") else ''}
    {f'<a href="{escape(str(latest_weekly.get("html_link") or ""))}">Weekly</a>' if latest_weekly.get("html_link") else ''}
    {f'<a href="{escape(str(latest_monthly.get("html_link") or ""))}">Monthly</a>' if latest_monthly.get("html_link") else ''}
    {f'<a href="{escape(str(latest_auto.get("html_link") or ""))}">Auto Commit</a>' if latest_auto.get("html_link") else ''}
  </div>
</section>
"""
        )
    return "".join(cards)


def svg_trend_chart(project_history: dict[str, list[dict[str, Any]]]) -> str:
    points = []
    for project in sorted(project_history):
        daily_items = [item for item in project_history[project] if item["type"] == "daily"][:8]
        if not daily_items:
            continue
        points.append((project, list(reversed(daily_items))))
    width = 1120
    height = 340
    colors = ["#264653", "#2a9d8f", "#f4a261", "#6d597a", "#e76f51"]
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="History trend chart">']
    parts.append('<line x1="80" y1="280" x2="1060" y2="280" stroke="#c8b9a5" stroke-width="2"></line>')
    parts.append('<line x1="80" y1="40" x2="80" y2="280" stroke="#c8b9a5" stroke-width="2"></line>')
    max_value = 1
    for _, items in points:
        for item in items:
            max_value = max(max_value, int(item.get("files") or 0))
    for idx, (project, items) in enumerate(points):
        color = colors[idx % len(colors)]
        poly = []
        for point_idx, item in enumerate(items):
            x = 120 + point_idx * 120
            y = 280 - int((int(item.get("files") or 0) / max_value) * 200)
            poly.append(f"{x},{y}")
            parts.append(f'<circle cx="{x}" cy="{y}" r="5" fill="{color}"></circle>')
            parts.append(f'<text x="{x}" y="302" text-anchor="middle" font-size="11" fill="#6b7280">{escape(str(item.get("date") or "")[5:])}</text>')
        parts.append(f'<polyline points="{" ".join(poly)}" fill="none" stroke="{color}" stroke-width="4"></polyline>')
        parts.append(f'<text x="890" y="{52 + idx*22}" font-size="13" fill="{color}">{escape(project)}</text>')
    parts.append('<text x="82" y="28" font-size="12" fill="#6b7280">Changed files</text>')
    parts.append('</svg>')
    return "".join(parts)


def build_history_tables(project_history: dict[str, list[dict[str, Any]]], automation_index: dict[str, dict[str, Any]]) -> str:
    sections = []
    for project, items in sorted(project_history.items()):
        rows = []
        by_date: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for item in items:
            by_date[str(item.get("date") or "")][str(item.get("type") or "")] = item
        for day in sorted(by_date.keys(), reverse=True)[:30]:
            bucket = by_date[day]
            auto = automation_index.get(project, {}).get(day, {})
            rows.append(
                f"""
<tr>
  <td>{escape(day)}</td>
  <td>{f'<a href="{escape(str(bucket.get("daily", {}).get("html_link") or ""))}">Daily</a>' if bucket.get("daily", {}).get("html_link") else '-'}</td>
  <td>{f'<a href="{escape(str(bucket.get("weekly", {}).get("html_link") or ""))}">Weekly</a>' if bucket.get("weekly", {}).get("html_link") else '-'}</td>
  <td>{f'<a href="{escape(str(bucket.get("monthly", {}).get("html_link") or ""))}">Monthly</a>' if bucket.get("monthly", {}).get("html_link") else '-'}</td>
  <td>{f'<a href="{escape(str(bucket.get("jira_plan", {}).get("html_link") or ""))}">Jira</a>' if bucket.get("jira_plan", {}).get("html_link") else '-'}</td>
  <td>{escape(str(auto.get("status") or '-'))}</td>
  <td>{f'<a href="{escape(str(auto.get("html_link") or ""))}">Open</a>' if auto.get("html_link") else '-'}</td>
</tr>
"""
            )
        sections.append(
            f"""
<section class="table-panel">
  <h2>{escape(project)} History</h2>
  <table>
    <thead>
      <tr>
        <th>Date</th>
        <th>Daily</th>
        <th>Weekly</th>
        <th>Monthly</th>
        <th>Jira</th>
        <th>Auto Commit</th>
        <th>Status Link</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows) or '<tr><td colspan="7">No history</td></tr>'}
    </tbody>
  </table>
</section>
"""
        )
    return "".join(sections)


def build_index_payload(project_history: dict[str, list[dict[str, Any]]], automation_index: dict[str, dict[str, Any]], run_date: str) -> dict[str, Any]:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_date": run_date,
        "projects": project_history,
        "automation": automation_index,
    }


def render_history_dashboard(run_date: str, project_history: dict[str, list[dict[str, Any]]], automation_index: dict[str, dict[str, Any]]) -> str:
    overview_html = build_overview_cards(project_history, automation_index)
    trend_html = svg_trend_chart(project_history)
    tables_html = build_history_tables(project_history, automation_index)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>History Dashboard {run_date}</title>
  <style>
    body {{ margin:0; font-family:"Segoe UI","Noto Sans KR",sans-serif; background:linear-gradient(180deg,#f6f1e8,#ebe2d4); color:#17212b; }}
    .wrap {{ max-width:1280px; margin:0 auto; padding:30px; }}
    .hero {{ background:linear-gradient(135deg,#12343b,#2c6e63); color:#fff; border-radius:30px; padding:30px; margin-bottom:22px; }}
    .hero h1 {{ margin:0 0 8px; font-size:38px; }}
    .hero p {{ margin:0; opacity:.9; line-height:1.6; }}
    .overview {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; margin-bottom:22px; }}
    .overview-card {{ background:#fffdf9; border:1px solid #ddd2c1; border-radius:24px; padding:22px; box-shadow:0 14px 30px rgba(23,33,43,.08); }}
    .overview-head {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; margin-bottom:10px; }}
    .overview-head h2 {{ margin:0 0 4px; }}
    .overview-head p {{ margin:0; color:#6b7280; font-size:13px; }}
    .mini-status {{ padding:8px 12px; border-radius:999px; background:#f8ead7; border:1px solid #e6c8a2; font-size:12px; font-weight:700; color:#9a3412; }}
    .stats {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin:12px 0; }}
    .stats div {{ background:#f8f1e4; border:1px solid #e6d8c4; border-radius:16px; padding:12px; }}
    .stats span {{ display:block; font-size:11px; color:#6b7280; text-transform:uppercase; letter-spacing:.08em; }}
    .stats strong {{ display:block; margin-top:5px; font-size:20px; }}
    .overview-card ul {{ margin:0 0 14px; padding-left:18px; }}
    .overview-card li {{ margin-bottom:6px; line-height:1.5; }}
    .links {{ display:flex; gap:10px; flex-wrap:wrap; }}
    .links a {{ text-decoration:none; color:#0f4c5c; background:#edf6f9; border:1px solid #c8d9dd; padding:10px 13px; border-radius:999px; font-weight:700; }}
    .panel {{ background:#fffdf9; border:1px solid #ddd2c1; border-radius:26px; padding:24px; box-shadow:0 14px 30px rgba(23,33,43,.08); margin-bottom:22px; }}
    .panel h2 {{ margin:0 0 14px; }}
    .chart {{ width:100%; height:auto; }}
    .table-panel {{ background:#fffdf9; border:1px solid #ddd2c1; border-radius:26px; padding:24px; box-shadow:0 14px 30px rgba(23,33,43,.08); margin-bottom:18px; overflow:auto; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ padding:12px 10px; border-bottom:1px solid #eee3d2; text-align:left; vertical-align:top; }}
    th {{ font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:#6b7280; }}
    td a {{ color:#0f4c5c; font-weight:700; text-decoration:none; }}
    @media (max-width:1024px) {{ .overview {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>History Dashboard</h1>
      <p>{escape(run_date)} 기준으로 생성된 종합 이력 대시보드입니다. 현재 리포트는 유지하고, 과거 데일리/주간/월간/Jira/자동 Commit 상태를 한 화면에서 탐색합니다.</p>
    </section>
    <section class="overview">
      {overview_html}
    </section>
    <section class="panel">
      <h2>Trend Overview</h2>
      {trend_html}
    </section>
    {tables_html}
  </div>
</body>
</html>"""


def main() -> int:
    args = parse_args()
    run_date = args.date or date.today().isoformat()
    project_history = collect_project_history()
    automation_index = load_automation_index()
    history_dir = REPORTS_ROOT / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    index_payload = build_index_payload(project_history, automation_index, run_date)
    (history_dir / "index.json").write_text(json.dumps(index_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = history_dir / f"{run_date}-history-dashboard.html"
    html_path.write_text(render_history_dashboard(run_date, project_history, automation_index), encoding="utf-8")
    print("Generated history dashboard:")
    print(html_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
