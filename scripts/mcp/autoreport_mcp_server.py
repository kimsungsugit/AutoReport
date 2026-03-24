#!/usr/bin/env python3
"""AutoReport MCP Server.

Exposes AutoReport capabilities as MCP tools so Claude Code (or any
MCP-compatible client) can generate reports, query dashboards, and manage
projects directly from the conversation.

Usage (stdio transport — register in Claude Code settings):
    python scripts/mcp/autoreport_mcp_server.py

Register in Claude Code settings.json:
    "mcpServers": {
      "autoreport": {
        "command": "python",
        "args": ["D:/Project/Program/AutoReport/scripts/mcp/autoreport_mcp_server.py"]
      }
    }
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# Ensure project root is importable
SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

# ---------------------------------------------------------------------------
# Try to import the `mcp` SDK.  If not installed, print a helpful message.
# ---------------------------------------------------------------------------
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    print(
        "ERROR: The 'mcp' package is not installed.\n"
        "Install it with:  pip install mcp\n",
        file=sys.stderr,
    )
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
REPORTS_DIR = WORKSPACE_ROOT / "reports"
STARTUP_PROJECTS_JSON = SCRIPTS_DIR / "startup_projects.json"


def _load_projects() -> list[dict[str, Any]]:
    if not STARTUP_PROJECTS_JSON.exists():
        return []
    data = json.loads(STARTUP_PROJECTS_JSON.read_text(encoding="utf-8"))
    return [p for p in (data.get("projects") or []) if isinstance(p, dict)]


def _enabled_projects() -> list[dict[str, Any]]:
    return [p for p in _load_projects() if p.get("enabled", True)]


def _run_script(script_name: str, extra_args: list[str] | None = None, timeout: int = 300) -> dict[str, Any]:
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name)]
    if extra_args:
        cmd.extend(extra_args)
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(WORKSPACE_ROOT),
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _latest_file(directory: Path, glob_pattern: str = "*.html") -> Path | None:
    if not directory.exists():
        return None
    files = sorted(directory.glob(glob_pattern), key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _read_latest_report(subdir: str, pattern: str = "*.md") -> str:
    path = REPORTS_DIR / subdir
    latest = _latest_file(path, pattern)
    if latest is None:
        return f"No reports found in {path}"
    return latest.read_text(encoding="utf-8", errors="replace")


def _previous_business_day(d: date) -> date:
    current = d - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


# ---------------------------------------------------------------------------
# MCP Server definition
# ---------------------------------------------------------------------------
server = Server("autoreport")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_projects",
            description="List all configured AutoReport projects with their paths and profiles.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="generate_all_reports",
            description=(
                "Generate daily/plan/jira reports for ALL enabled projects. "
                "Also produces the portfolio dashboard HTML."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_date": {
                        "type": "string",
                        "description": "Report date in YYYY-MM-DD format. Defaults to today.",
                    },
                },
            },
        ),
        Tool(
            name="generate_project_report",
            description="Generate reports for a SINGLE project by name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_name": {
                        "type": "string",
                        "description": "Project name as listed in startup_projects.json.",
                    },
                    "report_date": {
                        "type": "string",
                        "description": "Report date in YYYY-MM-DD format. Defaults to today.",
                    },
                },
                "required": ["project_name"],
            },
        ),
        Tool(
            name="get_latest_report",
            description="Read the latest report content for a given category (daily_brief, plans, jira, weekly_brief, monthly_brief).",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Report category: daily_brief, plans, jira, weekly_brief, monthly_brief.",
                        "enum": ["daily_brief", "plans", "jira", "weekly_brief", "monthly_brief"],
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Optional project name. If omitted, returns the aggregated report.",
                    },
                },
                "required": ["category"],
            },
        ),
        Tool(
            name="get_dashboard_path",
            description="Get the file path to the latest HTML dashboard (startup, portfolio, or project-specific).",
            inputSchema={
                "type": "object",
                "properties": {
                    "dashboard_type": {
                        "type": "string",
                        "description": "Type of dashboard: startup, portfolio, history.",
                        "enum": ["startup", "portfolio", "history"],
                    },
                },
                "required": ["dashboard_type"],
            },
        ),
        Tool(
            name="report_status",
            description="Show which reports exist for today (or a given date) and their file sizes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "report_date": {
                        "type": "string",
                        "description": "Date to check in YYYY-MM-DD format. Defaults to today.",
                    },
                },
            },
        ),
        Tool(
            name="auto_commit_push",
            description="Trigger auto commit and push for all configured repositories. Use --dry-run for safety.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, only inspect without committing. Defaults to true for safety.",
                        "default": True,
                    },
                    "report_date": {
                        "type": "string",
                        "description": "Date label in YYYY-MM-DD format. Defaults to today.",
                    },
                },
            },
        ),
        Tool(
            name="update_project_config",
            description="Add, remove, or toggle a project in the startup_projects.json configuration.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "enable", "disable"],
                        "description": "Action to perform on the project.",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Name of the project.",
                    },
                    "project_path": {
                        "type": "string",
                        "description": "Filesystem path (required for 'add' action).",
                    },
                    "profile": {
                        "type": "string",
                        "description": "Domain profile (optional, for 'add' action).",
                    },
                },
                "required": ["action", "project_name"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        result = _dispatch(name, arguments)
    except Exception as exc:
        result = f"Error: {exc}"
    return [TextContent(type="text", text=str(result))]


def _dispatch(name: str, args: dict[str, Any]) -> str:
    if name == "list_projects":
        return _handle_list_projects()
    if name == "generate_all_reports":
        return _handle_generate_all(args)
    if name == "generate_project_report":
        return _handle_generate_project(args)
    if name == "get_latest_report":
        return _handle_get_latest_report(args)
    if name == "get_dashboard_path":
        return _handle_get_dashboard(args)
    if name == "report_status":
        return _handle_report_status(args)
    if name == "auto_commit_push":
        return _handle_auto_commit(args)
    if name == "update_project_config":
        return _handle_update_config(args)
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------
def _handle_list_projects() -> str:
    projects = _load_projects()
    if not projects:
        return "No projects configured in startup_projects.json."
    lines = ["# Configured Projects\n"]
    for p in projects:
        status = "enabled" if p.get("enabled", True) else "disabled"
        lines.append(f"- **{p.get('name', '?')}** ({status})")
        lines.append(f"  - Path: `{p.get('path', '?')}`")
        lines.append(f"  - Profile: `{p.get('profile', 'default')}`")
    return "\n".join(lines)


def _handle_generate_all(args: dict[str, Any]) -> str:
    extra = []
    rd = args.get("report_date")
    if rd:
        extra.extend(["--date", rd])
    result = _run_script("generate_multi_project_reports.py", extra, timeout=600)
    if result["returncode"] == 0:
        return f"Reports generated successfully.\n\n{result['stdout']}"
    return f"Report generation failed (exit {result['returncode']}).\n\nstdout:\n{result['stdout']}\n\nstderr:\n{result['stderr']}"


def _handle_generate_project(args: dict[str, Any]) -> str:
    project_name = args["project_name"]
    projects = _enabled_projects()
    project = next((p for p in projects if p.get("name") == project_name), None)
    if project is None:
        available = ", ".join(p.get("name", "?") for p in projects)
        return f"Project '{project_name}' not found. Available: {available}"

    repo_path = Path(str(project.get("path", ""))).resolve()
    output_root = REPORTS_DIR / "projects" / project_name
    extra = [
        "--repo", str(repo_path),
        "--output-root", str(output_root),
    ]
    profile = str(project.get("profile") or "").strip()
    if profile:
        extra.extend(["--profile", profile])
    rd = args.get("report_date")
    if rd:
        extra.extend(["--date", rd])

    result = _run_script("generate_periodic_reports.py", extra, timeout=600)
    if result["returncode"] == 0:
        return f"Reports for '{project_name}' generated.\n\n{result['stdout']}"
    return f"Failed for '{project_name}' (exit {result['returncode']}).\n\nstderr:\n{result['stderr']}"


def _handle_get_latest_report(args: dict[str, Any]) -> str:
    category = args["category"]
    project_name = args.get("project_name")
    if project_name:
        subdir = f"projects/{project_name}/reports/{category}"
    else:
        subdir = category
    return _read_latest_report(subdir, "*.md")


def _handle_get_dashboard(args: dict[str, Any]) -> str:
    dtype = args["dashboard_type"]
    dirs = {
        "startup": REPORTS_DIR / "dashboard",
        "portfolio": REPORTS_DIR / "portfolio",
        "history": REPORTS_DIR / "history",
    }
    target_dir = dirs.get(dtype)
    if target_dir is None:
        return f"Unknown dashboard type: {dtype}"
    latest = _latest_file(target_dir, "*.html")
    if latest is None:
        return f"No dashboard found in {target_dir}."
    return f"Dashboard: {latest}\nURI: {latest.as_uri()}"


def _handle_report_status(args: dict[str, Any]) -> str:
    rd = args.get("report_date") or date.today().isoformat()
    categories = ["daily_brief", "plans", "jira", "dashboard", "portfolio"]
    lines = [f"# Report Status for {rd}\n"]
    for cat in categories:
        cat_dir = REPORTS_DIR / cat
        if not cat_dir.exists():
            lines.append(f"- **{cat}**: directory missing")
            continue
        files = sorted(cat_dir.glob(f"{rd}*"))
        if files:
            for f in files:
                size_kb = f.stat().st_size / 1024
                lines.append(f"- **{cat}**: `{f.name}` ({size_kb:.1f} KB)")
        else:
            lines.append(f"- **{cat}**: no reports for {rd}")

    # Per-project status
    projects_dir = REPORTS_DIR / "projects"
    if projects_dir.exists():
        lines.append("\n## Per-Project Reports\n")
        for pdir in sorted(projects_dir.iterdir()):
            if not pdir.is_dir():
                continue
            found = list(pdir.rglob(f"{rd}*"))
            lines.append(f"- **{pdir.name}**: {len(found)} file(s)")
    return "\n".join(lines)


def _handle_auto_commit(args: dict[str, Any]) -> str:
    dry_run = args.get("dry_run", True)
    extra = []
    if dry_run:
        extra.append("--dry-run")
    rd = args.get("report_date")
    if rd:
        extra.extend(["--date", rd])
    result = _run_script("auto_commit_push.py", extra, timeout=120)
    prefix = "[DRY RUN] " if dry_run else ""
    if result["returncode"] == 0:
        return f"{prefix}Auto commit completed.\n\n{result['stdout']}"
    return f"{prefix}Auto commit failed (exit {result['returncode']}).\n\nstderr:\n{result['stderr']}"


def _handle_update_config(args: dict[str, Any]) -> str:
    action = args["action"]
    project_name = args["project_name"]

    data: dict[str, Any] = {}
    if STARTUP_PROJECTS_JSON.exists():
        data = json.loads(STARTUP_PROJECTS_JSON.read_text(encoding="utf-8"))
    projects: list[dict[str, Any]] = data.get("projects") or []

    if action == "add":
        project_path = args.get("project_path")
        if not project_path:
            return "Error: project_path is required for 'add' action."
        if any(p.get("name") == project_name for p in projects):
            return f"Project '{project_name}' already exists."
        new_entry: dict[str, Any] = {
            "name": project_name,
            "path": project_path,
            "profile": args.get("profile", ""),
            "enabled": True,
        }
        projects.append(new_entry)
        data["projects"] = projects
        STARTUP_PROJECTS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"Added project '{project_name}'."

    if action == "remove":
        before = len(projects)
        projects = [p for p in projects if p.get("name") != project_name]
        if len(projects) == before:
            return f"Project '{project_name}' not found."
        data["projects"] = projects
        STARTUP_PROJECTS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"Removed project '{project_name}'."

    if action in ("enable", "disable"):
        found = False
        for p in projects:
            if p.get("name") == project_name:
                p["enabled"] = action == "enable"
                found = True
                break
        if not found:
            return f"Project '{project_name}' not found."
        data["projects"] = projects
        STARTUP_PROJECTS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"Project '{project_name}' {action}d."

    return f"Unknown action: {action}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def _main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
