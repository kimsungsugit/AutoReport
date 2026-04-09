"""Lightweight local API server that proxies Jira operations for the HTML dashboard.

Usage:
    python scripts/jira_proxy.py

Listens on http://localhost:18923
"""
from __future__ import annotations

import json
import sys
from datetime import date
from glob import glob
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Allow importing workflow module from project root
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from workflow.task_provider import get_task_provider

PORT = 18923
provider = get_task_provider({"jira": {"project_key": "APPL", "sprint_id": 152}})


def _find_latest_dashboard() -> Path | None:
    """Find the most recent startup dashboard HTML."""
    patterns = [
        str(REPO_ROOT / "reports" / "projects" / "*" / "reports" / "dashboard" / "*-startup-dashboard.html"),
        str(Path("D:/Project/devops/Release_claude/reports/dashboard/*-startup-dashboard.html")),
    ]
    files = []
    for p in patterns:
        files.extend(glob(p))
    files.sort(reverse=True)
    return Path(files[0]) if files else None


def _find_suggestions_file(target_date: str | None = None) -> Path | None:
    """Find the most recent suggestions JSON file."""
    pattern = str(REPO_ROOT / "reports" / "projects" / "*" / "reports" / "jira" / "*-jira-suggestions.json")
    # Also check project-level output roots
    pattern2 = str(Path("D:/Project/devops/Release_claude/reports/jira/*-jira-suggestions.json"))
    files = sorted(glob(pattern) + glob(pattern2), reverse=True)
    if target_date:
        for f in files:
            if target_date in f:
                return Path(f)
    return Path(files[0]) if files else None


def _load_suggestions(target_date: str | None = None) -> tuple[Path | None, dict]:
    path = _find_suggestions_file(target_date)
    if not path or not path.exists():
        return None, {"date": "", "suggestions": []}
    try:
        with open(path, encoding="utf-8") as f:
            return path, json.load(f)
    except (json.JSONDecodeError, OSError):
        return path, {"date": "", "suggestions": []}


def _save_suggestions(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _json_response(handler: BaseHTTPRequestHandler, data: dict, status: int = 200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


class ProxyHandler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/sprint/tasks":
            qs = parse_qs(parsed.query)
            sprint_id = qs.get("sprint_id", [None])[0]
            p = provider
            if sprint_id and hasattr(p, "sprint_id"):
                p.sprint_id = sprint_id
            tasks = p.get_tasks()
            _json_response(self, tasks)

        elif parsed.path == "/api/suggestions":
            qs = parse_qs(parsed.query)
            target_date = qs.get("date", [None])[0]
            _, data = _load_suggestions(target_date)
            pending = [s for s in data.get("suggestions", []) if s.get("status") == "pending"]
            _json_response(self, {"date": data.get("date", ""), "suggestions": pending})

        elif parsed.path == "/" or parsed.path == "/dashboard":
            # Serve latest dashboard HTML via HTTP (avoids file:// CORS issues)
            dashboard = _find_latest_dashboard()
            if dashboard and dashboard.exists():
                body = dashboard.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                _json_response(self, {"error": "no dashboard found"}, 404)

        else:
            _json_response(self, {"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = _read_body(self)

        # POST /api/issue/{key}/comment
        if path.startswith("/api/issue/") and path.endswith("/comment"):
            key = path.split("/")[3]
            comment = body.get("comment", "")
            ok = provider.add_comment(key, comment)
            _json_response(self, {"ok": ok})

        # POST /api/issue/{key}/complete
        elif path.startswith("/api/issue/") and path.endswith("/complete"):
            key = path.split("/")[3]
            comment = body.get("comment", "")
            ok = provider.complete_issue(key, comment)
            _json_response(self, {"ok": ok})

        # POST /api/issue/{key}/transition
        elif path.startswith("/api/issue/") and path.endswith("/transition"):
            key = path.split("/")[3]
            status = body.get("status", "")
            comment = body.get("comment", "")
            ok = provider.transition_issue(key, status, comment)
            _json_response(self, {"ok": ok})

        # POST /api/issue/create
        elif path == "/api/issue/create":
            parent_key = body.get("parent_key", "")
            summary = body.get("summary", "")
            if not parent_key or not summary:
                _json_response(self, {"error": "parent_key and summary required"}, 400)
                return
            try:
                result = provider._request("POST", "/rest/api/2/issue", {
                    "fields": {
                        "project": {"key": parent_key.split("-")[0]},
                        "parent": {"key": parent_key},
                        "summary": summary,
                        "issuetype": {"name": "부작업"},
                    }
                })
                _json_response(self, {"ok": True, "key": result.get("key", "")})
            except Exception as e:
                _json_response(self, {"ok": False, "error": str(e)}, 500)

        # POST /api/suggestions/{id}/approve
        elif "/api/suggestions/" in path and path.endswith("/approve"):
            sid = path.split("/")[3]
            task_key = body.get("task_key", "")
            stype = body.get("type", "comment")
            text = body.get("text", "")
            ok = False
            if stype == "comment" and task_key:
                ok = provider.add_comment(task_key, text)
            elif stype == "complete" and task_key:
                ok = provider.complete_issue(task_key, text)
            elif stype == "transition" and task_key:
                ok = provider.transition_issue(task_key, "진행 중", text)
            elif stype == "add_subtask" and task_key:
                try:
                    result = provider._request("POST", "/rest/api/2/issue", {
                        "fields": {
                            "project": {"key": task_key.split("-")[0]},
                            "parent": {"key": task_key},
                            "summary": text,
                            "issuetype": {"name": "부작업"},
                        }
                    })
                    ok = True
                except Exception:
                    ok = False
            # Update suggestion status in JSON
            fpath, data = _load_suggestions()
            if fpath:
                for s in data.get("suggestions", []):
                    if s.get("id") == sid:
                        s["status"] = "approved" if ok else "failed"
                        s["suggested_text"] = text
                        break
                _save_suggestions(fpath, data)
            _json_response(self, {"ok": ok})

        # POST /api/suggestions/{id}/reject
        elif "/api/suggestions/" in path and path.endswith("/reject"):
            sid = path.split("/")[3]
            fpath, data = _load_suggestions()
            if fpath:
                for s in data.get("suggestions", []):
                    if s.get("id") == sid:
                        s["status"] = "rejected"
                        break
                _save_suggestions(fpath, data)
            _json_response(self, {"ok": True})

        else:
            _json_response(self, {"error": "not found"}, 404)

    def log_message(self, format, *args):
        print(f"[jira-proxy] {args[0]}")


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), ProxyHandler)
    print(f"Jira proxy server running at http://localhost:{PORT}")
    print(f"Endpoints:")
    print(f"  GET  /api/sprint/tasks?sprint_id=152")
    print(f"  GET  /api/suggestions?date=2026-04-08")
    print(f"  POST /api/issue/{{key}}/comment")
    print(f"  POST /api/issue/{{key}}/complete")
    print(f"  POST /api/issue/{{key}}/transition")
    print(f"  POST /api/issue/create")
    print(f"  POST /api/suggestions/{{id}}/approve")
    print(f"  POST /api/suggestions/{{id}}/reject")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
