from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from html import escape
from pathlib import Path
import re


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate startup reports for multiple projects.")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "startup_projects.json"))
    parser.add_argument("--date", default=None, help="Reference date YYYY-MM-DD")
    return parser.parse_args()


def load_projects(config_path: Path) -> list[dict]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    projects = data.get("projects") or []
    return [item for item in projects if isinstance(item, dict) and item.get("enabled", True)]


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def run_project_report(repo_path: Path, report_date: str | None) -> tuple[bool, str]:
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
    if report_date:
        cmd.extend(["--date", report_date])
    proc = subprocess.run(cmd, text=True, capture_output=True, encoding="utf-8", errors="replace")
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
    subtasks = [line[2:].strip() for line in sections.get("Subtasks", []) if line.startswith("- ")]
    validation = [line[2:].strip() for line in sections.get("Validation", []) if line.startswith("- ")]
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
        "subtasks": subtasks,
        "validation": validation,
    }


def build_task_board(item: dict) -> str:
    plan = item.get("jira_plan_data") or {}
    task_name = str(plan.get("task_name") or item["name"])
    task_goal = str(plan.get("task_goal") or item["message"])
    task_scope = [str(x) for x in (plan.get("task_scope") or [])[:3]]
    subtasks = [str(x) for x in (plan.get("subtasks") or [])[:4]]
    validation = [str(x) for x in (plan.get("validation") or [])[:3]]
    task_scope_html = "".join(f"<li>{escape(x)}</li>" for x in task_scope) or "<li>No scoped items</li>"
    subtask_html = "".join(
        f"""
<li class="subtask-item">
  <span class="dot"></span>
  <div>
    <strong>{escape(text)}</strong>
    <span>Jira Title: {escape(text[:72])}</span>
  </div>
</li>
"""
        for text in subtasks
    ) or '<li class="subtask-item"><span class="dot"></span><div><strong>No subtasks</strong><span>Jira Title: define manual subtasks</span></div></li>'
    validation_html = "".join(f"<li>{escape(x)}</li>" for x in validation) or "<li>Validation not defined</li>"
    jira_plan_link = item.get("jira_plan_link", "")
    jira_result_link = item.get("jira_result_link", "")
    return f"""
<section class="task-board">
  <div class="task-col parent">
    <div class="task-tag">Parent Task</div>
    <h3>{escape(task_name)}</h3>
    <p>{escape(task_goal)}</p>
    <ul>{task_scope_html}</ul>
  </div>
  <div class="task-col subtasks">
    <div class="task-tag">Subtasks</div>
    <ul class="subtask-list">{subtask_html}</ul>
  </div>
  <div class="task-col result">
    <div class="task-tag">Definition Of Done</div>
    <ul>{validation_html}</ul>
    <div class="task-links">
      {f'<a href="{escape(jira_plan_link)}">Jira Plan</a>' if jira_plan_link else ''}
      {f'<a href="{escape(jira_result_link)}">Jira Result</a>' if jira_result_link else ''}
    </div>
  </div>
</section>
"""


def render_portfolio_dashboard(run_date: str, items: list[dict]) -> str:
    cards = []
    board_sections = []
    for item in items:
        status = item["status"]
        badge_class = "ok" if status == "generated" else "skip"
        daily_link = item.get("daily_link", "")
        dashboard_link = item.get("dashboard_link", "")
        jira_plan_link = item.get("jira_plan_link", "")
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
  <p class="message">{escape(item['message'])}</p>
  <div class="links">
    {f'<a href="{escape(dashboard_link)}">Project Dashboard</a>' if dashboard_link else ''}
    {f'<a href="{escape(daily_link)}">Daily Report</a>' if daily_link else ''}
    {f'<a href="{escape(jira_plan_link)}">Jira Plan</a>' if jira_plan_link else ''}
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
      {f'<a href="{escape(jira_plan_link)}">Open Jira Draft</a>' if jira_plan_link else ''}
    </div>
  </div>
  {build_task_board(item)}
</section>
"""
            )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Multi Project Startup Dashboard {run_date}</title>
  <style>
    body {{ margin:0; font-family:"Segoe UI","Noto Sans KR",sans-serif; background:linear-gradient(180deg,#f7f1e8,#efe6d8); color:#1f2937; }}
    .wrap {{ max-width:1100px; margin:0 auto; padding:32px; }}
    .hero {{ background:linear-gradient(135deg,#12343b,#2c6e63); color:#fff; border-radius:28px; padding:28px; margin-bottom:22px; }}
    .hero h1 {{ margin:0 0 8px; font-size:36px; }}
    .hero p {{ margin:0; opacity:.9; }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }}
    .card {{ background:#fffdf9; border:1px solid #ddd2c1; border-radius:24px; padding:22px; box-shadow:0 14px 30px rgba(23,33,43,.08); }}
    .head {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }}
    .head h2 {{ margin:0 0 6px; }}
    .head p {{ margin:0; color:#6b7280; font-size:13px; }}
    .badge {{ padding:8px 12px; border-radius:999px; font-size:12px; font-weight:700; }}
    .badge.ok {{ background:#d8f3dc; color:#1b4332; }}
    .badge.skip {{ background:#fef3c7; color:#92400e; }}
    .message {{ margin:16px 0; line-height:1.5; }}
    .links {{ display:flex; gap:12px; flex-wrap:wrap; }}
    .links a {{ text-decoration:none; color:#0f4c5c; background:#edf6f9; border:1px solid #c8d9dd; padding:10px 14px; border-radius:999px; font-weight:600; }}
    .section-title {{ margin:34px 0 14px; font-size:28px; }}
    .section-copy {{ margin:0 0 18px; color:#4b5563; line-height:1.6; }}
    .project-board {{ background:linear-gradient(180deg,#fffdf9,#f8f1e4); border:1px solid #ddd2c1; border-radius:28px; padding:24px; box-shadow:0 14px 30px rgba(23,33,43,.08); margin-bottom:18px; }}
    .project-head {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:16px; }}
    .project-head h2 {{ margin:0 0 6px; }}
    .project-head p {{ margin:0; color:#6b7280; font-size:13px; }}
    .project-links {{ display:flex; gap:10px; flex-wrap:wrap; }}
    .project-links a {{ text-decoration:none; color:#0f4c5c; background:#f5ede1; border:1px solid #d8ccb7; padding:10px 14px; border-radius:999px; font-weight:700; }}
    .task-board {{ display:grid; grid-template-columns:1.1fr 1.1fr .9fr; gap:14px; }}
    .task-col {{ border:1px solid #ddd2c1; border-radius:22px; padding:18px; background:linear-gradient(180deg,#fffdfa,#f7f1e5); }}
    .task-col.parent {{ background:linear-gradient(180deg,#eef8f8,#e9f3f1); }}
    .task-col.subtasks {{ background:linear-gradient(180deg,#fff9ef,#fbf1de); }}
    .task-col.result {{ background:linear-gradient(180deg,#fff4f1,#faece8); }}
    .task-col h3 {{ margin:0 0 10px; font-size:20px; line-height:1.2; }}
    .task-col p {{ margin:0 0 12px; color:#4b5563; line-height:1.55; }}
    .task-col ul {{ margin:0; padding-left:20px; }}
    .task-col li {{ margin-bottom:8px; line-height:1.5; }}
    .task-tag {{ display:inline-block; margin-bottom:10px; padding:6px 10px; border-radius:999px; border:1px solid #ddd2c1; background:rgba(255,255,255,.7); font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:#6b7280; }}
    .subtask-list {{ list-style:none; padding-left:0; }}
    .subtask-item {{ display:grid; grid-template-columns:auto 1fr; gap:12px; align-items:start; margin-bottom:12px; }}
    .subtask-item strong {{ display:block; margin-bottom:4px; font-size:14px; }}
    .subtask-item span {{ display:block; color:#6b7280; font-size:12px; }}
    .dot {{ width:14px; height:14px; border-radius:50%; margin-top:4px; background:#d17a22; box-shadow:0 0 0 4px rgba(209,122,34,.14); }}
    .task-links {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:16px; }}
    .task-links a {{ text-decoration:none; color:#7c2d12; background:#fff7ed; border:1px solid #f5d0a9; padding:9px 12px; border-radius:999px; font-weight:700; }}
    .portfolio-grid {{ display:grid; grid-template-columns:1fr; gap:18px; }}
    @media (max-width:980px) {{ .task-board {{ grid-template-columns:1fr; }} }}
    @media (max-width:800px) {{ .grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>Multi Project Startup Dashboard</h1>
      <p>{escape(run_date)} portfolio summary for configured repositories.</p>
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
</body>
</html>"""


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    projects = load_projects(config_path)
    run_date = args.date or date.today().isoformat()
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

        ok, message = run_project_report(repo_path, args.date)
        output_root = WORKSPACE_ROOT / "reports" / "projects" / name
        dashboard = output_root / "reports" / "dashboard" / f"{run_date}-startup-dashboard.html"
        daily = output_root / "reports" / "daily_brief" / f"{run_date}-daily-report.html"
        jira_plan = output_root / "reports" / "jira" / f"{run_date}-jira-plan.html"
        jira_result = output_root / "reports" / "jira" / f"{run_date}-jira-result.html"
        jira_plan_md = output_root / "reports" / "jira" / f"{run_date}-jira-plan.md"
        items.append(
            {
                "name": name,
                "path": str(repo_path),
                "status": "generated" if ok else "skipped",
                "message": "Reports generated successfully." if ok else message,
                "dashboard_link": dashboard.as_uri() if dashboard.exists() else "",
                "daily_link": daily.as_uri() if daily.exists() else "",
                "jira_plan_link": jira_plan.as_uri() if jira_plan.exists() else "",
                "jira_result_link": jira_result.as_uri() if jira_result.exists() else "",
                "jira_plan_data": parse_jira_plan(jira_plan_md),
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
