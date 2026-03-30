from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from html import escape
from pathlib import Path
import re
from hashlib import sha1


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent

sys.path.insert(0, str(WORKSPACE_ROOT))
from scripts.design_system import DESIGN_CSS, CHECKLIST_JS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate startup reports for multiple projects.")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "startup_projects.json"))
    parser.add_argument("--date", default=None, help="Reference date YYYY-MM-DD")
    return parser.parse_args()


def previous_business_day(target: date) -> date:
    current = target - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def load_projects(config_path: Path) -> list[dict]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    projects = data.get("projects") or []
    return [item for item in projects if isinstance(item, dict) and item.get("enabled", True)]


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def run_project_report(project: dict, report_date: str | None) -> tuple[bool, str]:
    repo_path = Path(str(project.get("path") or "")).resolve()
    safe_name = repo_path.name
    output_root = WORKSPACE_ROOT / "reports" / "projects" / safe_name
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "generate_periodic_reports.py"),
        "--repo",
        str(repo_path),
        "--output-root",
        str(output_root),
    ]
    profile = str(project.get("profile") or "").strip()
    if profile:
        cmd.extend(["--profile", profile])
    if report_date:
        cmd.extend(["--date", report_date])
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        return False, "Timed out after 600s"
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    return False, (proc.stderr.strip() or proc.stdout.strip() or "Unknown error")


def parse_markdown_sections(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        match = re.match(r"^##\s+(.+)$", line)
        if match:
            current = match.group(1).strip()
            sections[current] = []
            continue
        if current:
            sections[current].append(line)
    return sections


def parse_jira_plan(path: Path) -> dict[str, object]:
    sections = parse_markdown_sections(path)
    task_lines = [line[2:].strip() for line in sections.get("Task", []) if line.startswith("- ")]
    completed = [line[2:].strip() for line in sections.get("Completed", []) if line.startswith("- ")]
    in_progress = [line[2:].strip() for line in sections.get("In Progress", []) if line.startswith("- ")]
    remaining = [line[2:].strip() for line in sections.get("Remaining", []) if line.startswith("- ")]
    validation = [line[2:].strip() for line in sections.get("Validation", []) if line.startswith("- ")]
    sprint_status = [line[2:].strip() for line in sections.get("Sprint Status", []) if line.startswith("- ")]
    task_name = ""
    task_goal = ""
    task_scope: list[str] = []
    for line in task_lines:
        if line.startswith("Name:"):
            task_name = line.split(":", 1)[1].strip()
        elif line.startswith("Goal:"):
            task_goal = line.split(":", 1)[1].strip()
        else:
            task_scope.append(line)
    return {
        "task_name": task_name,
        "task_goal": task_goal,
        "task_scope": task_scope,
        "completed": completed,
        "in_progress": in_progress,
        "remaining": remaining,
        "validation": validation,
        "sprint_status": sprint_status,
    }


def parse_daily_facets(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"work_type": "", "primary_facets": [], "supporting_facets": [], "summary": [], "source_changes": []}

    text = path.read_text(encoding="utf-8", errors="replace")
    work_type_match = re.search(r"-\s*\uC791\uC5C5 \uC720\uD615:\s*`([^`]+)`", text)
    work_type = work_type_match.group(1).strip() if work_type_match else ""

    primary: list[str] = []
    supporting: list[str] = []
    summary: list[str] = []
    source_changes: list[str] = []
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "### \uC8FC\uC694 \uBCC0\uACBD \uC131\uACA9":
            current = "primary"
            continue
        if line == "### \uBCF4\uC870 \uBCC0\uACBD \uC131\uACA9":
            current = "supporting"
            continue
        if line == "## \uD575\uC2EC \uC694\uC57D":
            current = "summary"
            continue
        if line == "## \uC18C\uC2A4 \uAE30\uBC18 \uD575\uC2EC \uBCC0\uACBD":
            current = "source_changes"
            continue
        if line.startswith("## "):
            current = None
            continue
        if current and line.startswith("- "):
            body = line[2:].strip()
            if current == "primary":
                primary.append(body)
            elif current == "supporting":
                supporting.append(body)
            elif current == "summary":
                summary.append(body)
            elif current == "source_changes":
                source_changes.append(body)

    return {
        "work_type": work_type,
        "primary_facets": primary[:3],
        "supporting_facets": supporting[:3],
        "summary": summary[:3],
        "source_changes": source_changes[:3],
    }


def parse_automation_status(path: Path, project_name: str) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    for item in payload.get("projects") or []:
        if str(item.get("name") or "") == project_name:
            return dict(item)
    return {}


def build_task_board(item: dict) -> str:
    plan = item.get("jira_plan_data") or {}
    daily = item.get("daily_data") or {}
    auto_status = item.get("automation_status_data") or {}
    task_name = str(plan.get("task_name") or item["name"])
    task_goal = str(plan.get("task_goal") or item["message"])
    task_scope = [str(x) for x in (plan.get("task_scope") or [])[:3]]
    completed = [str(x) for x in (plan.get("completed") or [])[:3]]
    in_progress = [str(x) for x in (plan.get("in_progress") or [])[:3]]
    remaining = [str(x) for x in (plan.get("remaining") or [])[:4]]
    validation = [str(x) for x in (plan.get("validation") or [])[:3]]
    sprint_status = [str(x) for x in (plan.get("sprint_status") or [])[:3]]
    work_type = str(daily.get("work_type") or "")
    primary_facets = [str(x) for x in (daily.get("primary_facets") or [])[:3]]
    supporting_facets = [str(x) for x in (daily.get("supporting_facets") or [])[:3]]
    summary = [str(x) for x in (daily.get("summary") or [])[:2]]
    source_changes = [str(x) for x in (daily.get("source_changes") or [])[:3]]
    task_scope_html = "".join(f"<li>{escape(x)}</li>" for x in task_scope) or "<li>No scoped items</li>"
    subtask_html = "".join(
        f"""
<li class="subtask-item">
  <label class="check-label">
  <input class="check-input" type="checkbox" data-checklist-id="{sha1((item['name'] + '::' + text).encode('utf-8')).hexdigest()[:16]}">
  <span class="check-box"></span>
  <div>
    <strong>{escape(text)}</strong>
    <span>Remaining Work: {escape(text[:72])}</span>
  </div>
  </label>
</li>
"""
        for text in remaining
    ) or '<li class="subtask-item"><span class="check-box done"></span><div><strong>No remaining tasks</strong><span>Remaining work already cleared</span></div></li>'
    validation_html = "".join(f"<li>{escape(x)}</li>" for x in validation) or "<li>Validation not defined</li>"
    sprint_html = "".join(f"<li>{escape(x)}</li>" for x in sprint_status) or "<li>Status not defined</li>"
    completed_html = "".join(f"<li>{escape(x)}</li>" for x in completed) or "<li>No completed items</li>"
    progress_html = "".join(f"<li>{escape(x)}</li>" for x in in_progress) or "<li>No in-progress items</li>"
    summary_html = "".join(f"<li>{escape(x)}</li>" for x in summary) or "<li>핵심 요약 없음</li>"
    source_change_html = "".join(f"<li>{escape(x)}</li>" for x in source_changes) or "<li>핵심 변경 파일 없음</li>"
    primary_facet_html = "".join(f'<span class="facet-chip primary">{escape(x)}</span>' for x in primary_facets) or '<span class="facet-chip primary">핵심 변경 분류 없음</span>'
    supporting_facet_html = "".join(f'<span class="facet-chip support">{escape(x)}</span>' for x in supporting_facets) or '<span class="facet-chip support">보조 변경 없음</span>'
    auto_status_text = str(auto_status.get("status") or "")
    jira_plan_link = item.get("jira_plan_link", "")
    auto_status_link = item.get("automation_status_link", "")
    return f"""
<section class="task-board">
  <div class="task-col parent">
    <div class="task-tag">Parent Task</div>
    {f'<div class="work-type">{escape(work_type)}</div>' if work_type else ''}
    {f'<div class="work-type">Auto Commit · {escape(auto_status_text)}</div>' if auto_status_text else ''}
    <h3>{escape(task_name)}</h3>
    <p>{escape(task_goal)}</p>
    <div class="facet-zone">
      <div class="facet-title">Primary Change Facets</div>
      <div class="facet-row">{primary_facet_html}</div>
      <div class="facet-title">Supporting Change Facets</div>
      <div class="facet-row">{supporting_facet_html}</div>
    </div>
    <div class="snapshot-block">
      <div class="facet-title">Executive Snapshot</div>
      <ul>{summary_html}</ul>
    </div>
    <div class="snapshot-block">
      <div class="facet-title">Key Source Changes</div>
      <ul>{source_change_html}</ul>
    </div>
    <ul>{task_scope_html}</ul>
  </div>
  <div class="task-col subtasks">
    <div class="task-tag">Remaining Work</div>
    <ul class="subtask-list">{subtask_html}</ul>
  </div>
  <div class="task-col result">
    <div class="task-tag">Sprint Snapshot</div>
    <div class="snapshot-block">
      <div class="facet-title">Status</div>
      <ul>{sprint_html}</ul>
    </div>
    <div class="snapshot-block">
      <div class="facet-title">Completed</div>
      <ul>{completed_html}</ul>
    </div>
    <div class="snapshot-block">
      <div class="facet-title">In Progress</div>
      <ul>{progress_html}</ul>
    </div>
    <div class="facet-title">Definition Of Done</div>
    <ul>{validation_html}</ul>
    <div class="task-links">
      {f'<a href="{escape(jira_plan_link)}">Jira Status</a>' if jira_plan_link else ''}
      {f'<a href="{escape(auto_status_link)}">Auto Commit Status</a>' if auto_status_link else ''}
    </div>
  </div>
</section>
"""


def render_portfolio_dashboard(run_date: str, items: list[dict]) -> str:
    history_dashboard = WORKSPACE_ROOT / "reports" / "history" / f"{run_date}-history-dashboard.html"
    history_link = history_dashboard.as_uri() if history_dashboard.exists() else ""
    cards = []
    board_sections = []
    for item in items:
        status = item["status"]
        badge_class = "badge-ok" if status == "generated" else "badge-warn"
        daily_link = item.get("daily_link", "")
        dashboard_link = item.get("dashboard_link", "")
        jira_plan_link = item.get("jira_plan_link", "")
        daily = item.get("daily_data") or {}
        auto_status = item.get("automation_status_data") or {}
        work_type = str(daily.get("work_type") or "")
        primary_facets = [str(x) for x in (daily.get("primary_facets") or [])[:2]]
        supporting_facets = [str(x) for x in (daily.get("supporting_facets") or [])[:2]]
        summary = [str(x) for x in (daily.get("summary") or [])[:1]]
        source_changes = [str(x) for x in (daily.get("source_changes") or [])[:2]]
        auto_status_text = str(auto_status.get("status") or "")
        card_primary_html = "".join(f'<span class="mini-chip primary">{escape(x)}</span>' for x in primary_facets)
        card_support_html = "".join(f'<span class="mini-chip support">{escape(x)}</span>' for x in supporting_facets)
        auto_chip_html = f'<span class="mini-chip support">Auto Commit · {escape(auto_status_text)}</span>' if auto_status_text else ""
        summary_html = "".join(f"<p class=\"summary-line\">{escape(x)}</p>" for x in summary)
        source_change_html = "".join(f"<li>{escape(x)}</li>" for x in source_changes)
        cards.append(
            f"""
<section class="card">
  <div class="head">
    <div>
      <h2>{escape(item['name'])}</h2>
      <p>{escape(item['path'])}</p>
    </div>
    <span class="badge {badge_class}">{escape(status.upper())}</span>
  </div>
  {f'<p class="work-type-line">Work Type · <strong>{escape(work_type)}</strong></p>' if work_type else ''}
  <p class="message">{escape(item['message'])}</p>
  <div class="mini-facets">
    {card_primary_html}
    {card_support_html}
    {auto_chip_html}
  </div>
  {summary_html}
  <div class="source-box">
    <div class="source-title">Key Source Changes</div>
    <ul>{source_change_html or '<li>핵심 변경 파일 없음</li>'}</ul>
  </div>
  <div class="links">
    {f'<a href="{escape(dashboard_link)}">Project Dashboard</a>' if dashboard_link else ''}
    {f'<a href="{escape(daily_link)}">Daily Report</a>' if daily_link else ''}
    {f'<a href="{escape(jira_plan_link)}">Jira Status</a>' if jira_plan_link else ''}
    {f'<a href="{escape(item.get("automation_status_link",""))}">Auto Commit Status</a>' if item.get("automation_status_link","") else ''}
  </div>
</section>
"""
        )
        if status == "generated":
            board_sections.append(
                f"""
<section class="project-board">
  <div class="project-head">
    <div>
      <h2>{escape(item['name'])}</h2>
      <p>{escape(item['path'])}</p>
    </div>
    <div class="project-links">
      {f'<a href="{escape(dashboard_link)}">Open Project Dashboard</a>' if dashboard_link else ''}
      {f'<a href="{escape(jira_plan_link)}">Open Jira Status</a>' if jira_plan_link else ''}
    </div>
  </div>
  {build_task_board(item)}
</section>
"""
            )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Multi Project Startup Dashboard {run_date}</title>
  <style>
{DESIGN_CSS}
    /* --- Portfolio overrides --- */
    .badge-ok {{ background:var(--ok-bg); color:var(--ok-ink); }}
    .badge-warn {{ background:var(--warn-bg); color:var(--warn-ink); }}
    .task-board {{ grid-template-columns:1.1fr 1.1fr .9fr; }}
  </style>
</head>
<body>
  <a href="#main-content" class="skip-link">Skip to content</a>
  <div class="wrap" id="main-content">
    <section class="hero">
      <h1>Multi Project Startup Dashboard</h1>
      <p>{escape(run_date)} portfolio summary for configured repositories.</p>
      {f'<div class="hero-links"><a href="{escape(history_link)}">Open History Dashboard</a></div>' if history_link else ''}
    </section>
    <h2 class="section-title">Portfolio Task Boards</h2>
    <p class="section-copy">Use each parent task as the Jira task draft, then create the listed subtasks and close them against the shown completion criteria.</p>
    <div class="portfolio-grid">
      {"".join(board_sections)}
    </div>
    <h2 class="section-title">Project Status Cards</h2>
    <div class="grid">
      {"".join(cards)}
    </div>
  </div>
{CHECKLIST_JS}
</body>
</html>"""


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    projects = load_projects(config_path)
    run_date = args.date or date.today().isoformat()
    run_day = date.fromisoformat(run_date)
    automation_day = previous_business_day(run_day).isoformat()
    automation_json = WORKSPACE_ROOT / "reports" / "automation_status" / f"{automation_day}-auto-commit-push.json"
    automation_html = WORKSPACE_ROOT / "reports" / "automation_status" / f"{automation_day}-auto-commit-push.html"
    items: list[dict] = []

    for project in projects:
        name = str(project.get("name") or Path(str(project.get("path") or "")).name or "project")
        repo_path = Path(str(project.get("path") or "")).resolve()
        if not repo_path.exists():
            items.append({"name": name, "path": str(repo_path), "status": "skipped", "message": "Path does not exist."})
            continue
        if not is_git_repo(repo_path):
            items.append({"name": name, "path": str(repo_path), "status": "skipped", "message": "Git repository not found. Reports will start once this folder is committed or cloned as a git repo."})
            continue

        ok, message = run_project_report(project, args.date)
        output_root = WORKSPACE_ROOT / "reports" / "projects" / name
        dashboard = output_root / "reports" / "dashboard" / f"{run_date}-startup-dashboard.html"
        daily = output_root / "reports" / "daily_brief" / f"{run_date}-daily-report.html"
        daily_md = output_root / "reports" / "daily_brief" / f"{run_date}-daily-report.md"
        jira_plan = output_root / "reports" / "jira" / f"{run_date}-jira-status.html"
        jira_plan_md = output_root / "reports" / "jira" / f"{run_date}-jira-status.md"
        items.append(
            {
                "name": name,
                "path": str(repo_path),
                "status": "generated" if ok else "skipped",
                "message": "Reports generated successfully." if ok else message,
                "dashboard_link": dashboard.as_uri() if dashboard.exists() else "",
                "daily_link": daily.as_uri() if daily.exists() else "",
                "jira_plan_link": jira_plan.as_uri() if jira_plan.exists() else "",
                "jira_plan_data": parse_jira_plan(jira_plan_md),
                "daily_data": parse_daily_facets(daily_md),
                "automation_status_link": automation_html.as_uri() if automation_html.exists() else "",
                "automation_status_data": parse_automation_status(automation_json, name),
            }
        )

    output = WORKSPACE_ROOT / "reports" / "portfolio" / f"{run_date}-multi-project-dashboard.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_portfolio_dashboard(run_date, items), encoding="utf-8")

    print("Generated multi-project dashboard:")
    print(output)
    for item in items:
        print(f"{item['name']}: {item['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
