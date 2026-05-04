"""Lightweight local API server that proxies Jira operations for the HTML dashboard.

Usage:
    python scripts/jira_proxy.py

Listens on http://localhost:18923
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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

# --- Report regeneration state ---------------------------------------------
# A single in-flight regeneration is tracked via a lock file. The file is
# created when a job starts and updated/removed when it finishes.
REGEN_LOCK = REPO_ROOT / "reports" / ".regenerate.lock"
REGEN_LOG = REPO_ROOT / "reports" / ".regenerate.log"
REGEN_SCRIPT = REPO_ROOT / "scripts" / "generate_multi_project_reports.py"


def _regen_read_state() -> dict:
    """Return current regeneration state {running, started_at, elapsed_s, exit_code, ...}.

    If the recorded PID has died but no `exit_code` was recorded (e.g. proxy was
    restarted mid-job, so the watcher thread never ran), synthesize exit_code=-1
    so the UI can show "✗ 실패 (인터럽트)" instead of an empty completion state.
    """
    if not REGEN_LOCK.exists():
        return {"running": False}
    try:
        state = json.loads(REGEN_LOCK.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"running": False}
    pid = state.get("pid")
    started_at = state.get("started_at", 0)
    if not pid:
        return {"running": False}
    alive = _pid_alive(int(pid))
    if not alive:
        exit_code = state.get("exit_code")
        finished_at = state.get("finished_at")
        if exit_code is None:
            # Watcher never recorded the exit (proxy restart mid-job). Mark as
            # interrupted so the UI shows a real status rather than "running".
            exit_code = -1
            finished_at = finished_at or time.time()
        return {
            "running": False,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_s": (finished_at or time.time()) - started_at,
            "exit_code": exit_code,
            "log_tail": _read_log_tail(),
        }
    return {
        "running": True,
        "pid": pid,
        "started_at": started_at,
        "elapsed_s": time.time() - started_at,
    }


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL, text=True, timeout=5)
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _read_log_tail(max_lines: int = 20) -> str:
    if not REGEN_LOG.exists():
        return ""
    try:
        lines = REGEN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""


def _regen_start() -> tuple[bool, str, dict]:
    """Spawn the multi-project regeneration as a detached subprocess.

    Returns (started, error_message, state_after_attempt).
    Idempotent: if a job is already running, returns (False, reason, state).
    """
    state = _regen_read_state()
    if state.get("running"):
        return False, "이미 재생성 작업이 진행 중입니다", state
    REGEN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    # Truncate previous log
    REGEN_LOG.write_text("", encoding="utf-8")
    log_handle = open(REGEN_LOG, "ab")
    try:
        # Detached: don't tie child's lifetime to this proxy.
        # `-u` makes Python unbuffered so log_tail reflects real-time progress.
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
        proc = subprocess.Popen(
            [sys.executable, "-u", str(REGEN_SCRIPT)],
            cwd=str(REPO_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
        )
    except Exception as e:
        log_handle.close()
        return False, f"프로세스 시작 실패: {e}", {"running": False}

    started_at = time.time()
    # Write lock FIRST, then start the watcher. If watcher fires before the
    # main thread writes the lock, the watcher's exit_code update could be
    # lost when this thread overwrites it.
    REGEN_LOCK.write_text(json.dumps({
        "pid": proc.pid,
        "started_at": started_at,
    }), encoding="utf-8")

    import threading
    def _watch():
        rc = proc.wait()
        try:
            cur = json.loads(REGEN_LOCK.read_text(encoding="utf-8"))
        except Exception:
            cur = {"pid": proc.pid, "started_at": started_at}
        cur["exit_code"] = rc
        cur["finished_at"] = time.time()
        try:
            REGEN_LOCK.write_text(json.dumps(cur), encoding="utf-8")
        except OSError:
            pass
        try:
            log_handle.close()
        except Exception:
            pass
    threading.Thread(target=_watch, daemon=True).start()

    return True, "", {"running": True, "pid": proc.pid, "started_at": started_at, "elapsed_s": 0}


def _find_latest_dashboard() -> Path | None:
    """Find the most recent startup dashboard HTML.

    Sort by file basename (date in name) rather than full path so a legacy
    fallback location with newer-named files doesn't shadow the primary one.
    """
    patterns = [
        str(REPO_ROOT / "reports" / "projects" / "*" / "reports" / "dashboard" / "*-startup-dashboard.html"),
        str(Path("D:/Project/devops/Release_claude/reports/dashboard/*-startup-dashboard.html")),
    ]
    files = []
    for p in patterns:
        files.extend(glob(p))
    # Sort by (basename, path) reverse — newest date wins; primary path wins ties
    files.sort(key=lambda fp: (Path(fp).name, fp), reverse=True)
    return Path(files[0]) if files else None


def _find_suggestions_file(target_date: str | None = None) -> Path | None:
    """Find the most recent suggestions JSON file."""
    pattern = str(REPO_ROOT / "reports" / "projects" / "*" / "reports" / "jira" / "*-jira-suggestions.json")
    # Also check project-level output roots
    pattern2 = str(Path("D:/Project/devops/Release_claude/reports/jira/*-jira-suggestions.json"))
    files = sorted(glob(pattern) + glob(pattern2),
                   key=lambda fp: (Path(fp).name, fp), reverse=True)
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


def _last_error() -> str:
    """Read the most recent provider error message (empty string if none)."""
    return getattr(provider, "last_error", "") or ""


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
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"[jira-proxy] bad request body: {e}")
        return {}


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

        elif parsed.path == "/api/regenerate/status":
            _json_response(self, _regen_read_state())

        elif parsed.path == "/api/suggestions":
            qs = parse_qs(parsed.query)
            target_date = qs.get("date", [None])[0]
            _, data = _load_suggestions(target_date)
            pending = [s for s in data.get("suggestions", []) if s.get("status") == "pending"]
            _json_response(self, {"date": data.get("date", ""), "suggestions": pending})

        elif parsed.path in ("/", "/dashboard", "/portfolio"):
            # Serve latest dashboard HTML via HTTP (avoids file:// CORS issues)
            if parsed.path == "/portfolio":
                pattern = str(REPO_ROOT / "reports" / "portfolio" / "*-multi-project-dashboard.html")
                files = sorted(glob(pattern), reverse=True)
                dashboard = Path(files[0]) if files else None
            else:
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

        # POST /api/regenerate
        if path == "/api/regenerate":
            started, err, state = _regen_start()
            if started:
                _json_response(self, {"ok": True, "state": state})
            else:
                _json_response(self, {"ok": False, "error": err, "state": state}, 409)
            return

        # POST /api/issue/{key}/comment
        if path.startswith("/api/issue/") and path.endswith("/comment"):
            key = path.split("/")[3]
            comment = body.get("comment", "")
            description = body.get("description", "")
            err_desc = err_comment = ""
            if description:
                ok_desc = provider.update_description(key, description)
                if not ok_desc: err_desc = _last_error()
            else:
                ok_desc = True
            if comment:
                ok_comment = provider.add_comment(key, comment)
                if not ok_comment: err_comment = _last_error()
            else:
                ok_comment = True
            _json_response(self, {"ok": ok_desc and ok_comment,
                                  "comment_ok": ok_comment, "description_ok": ok_desc,
                                  "comment_error": err_comment, "description_error": err_desc})

        # POST /api/issue/{key}/description
        elif path.startswith("/api/issue/") and path.endswith("/description"):
            key = path.split("/")[3]
            description = body.get("description", "")
            ok = provider.update_description(key, description)
            _json_response(self, {"ok": ok, "error": "" if ok else _last_error()})

        # POST /api/issue/{key}/complete
        elif path.startswith("/api/issue/") and path.endswith("/complete"):
            key = path.split("/")[3]
            comment = body.get("comment", "")
            description = body.get("description", "")
            err_desc = err_complete = ""
            if description:
                ok_desc = provider.update_description(key, description)
                if not ok_desc: err_desc = _last_error()
            else:
                ok_desc = True
            ok_complete = provider.complete_issue(key, comment)
            if not ok_complete: err_complete = _last_error()
            _json_response(self, {"ok": ok_desc and ok_complete,
                                  "comment_ok": ok_complete, "description_ok": ok_desc,
                                  "comment_error": err_complete, "description_error": err_desc})

        # POST /api/issue/{key}/transition
        elif path.startswith("/api/issue/") and path.endswith("/transition"):
            key = path.split("/")[3]
            status = body.get("status", "")
            comment = body.get("comment", "")
            description = body.get("description", "")
            err_desc = err_trans = ""
            if description:
                ok_desc = provider.update_description(key, description)
                if not ok_desc: err_desc = _last_error()
            else:
                ok_desc = True
            ok_trans = provider.transition_issue(key, status, comment)
            if not ok_trans: err_trans = _last_error()
            _json_response(self, {"ok": ok_desc and ok_trans,
                                  "comment_ok": ok_trans, "description_ok": ok_desc,
                                  "comment_error": err_trans, "description_error": err_desc})

        # POST /api/issue/create
        elif path == "/api/issue/create":
            parent_key = body.get("parent_key", "")
            summary = body.get("summary", "")
            description = body.get("description", "")
            if not parent_key or not summary:
                _json_response(self, {"error": "parent_key and summary required"}, 400)
                return
            try:
                fields = {
                    "project": {"key": parent_key.split("-")[0]},
                    "parent": {"key": parent_key},
                    "summary": summary,
                    "issuetype": {"name": "부작업"},
                }
                if description:
                    fields["description"] = description
                result = provider._request("POST", "/rest/api/2/issue", {"fields": fields})
                _json_response(self, {"ok": True, "key": result.get("key", "")})
            except Exception as e:
                _json_response(self, {"ok": False, "error": str(e)}, 500)

        # POST /api/suggestions/{id}/approve
        elif "/api/suggestions/" in path and path.endswith("/approve"):
            sid = path.split("/")[3]
            task_key = body.get("task_key", "")
            stype = body.get("type", "comment")
            # `text` is the legacy single-field; `comment` and `description` are the
            # split fields from the new dual-input UI. Fall back to `text` for
            # backwards compatibility with older payloads.
            text = body.get("text", "")
            comment = body.get("comment", text)
            description = body.get("description", "")
            # Require at least one meaningful action; empty approve must not silently succeed.
            if not (comment.strip() or description.strip()):
                _json_response(self, {"ok": False, "error": "comment 또는 description 중 하나는 입력해야 합니다",
                                      "comment_ok": False, "description_ok": False}, 400)
                return
            # add_subtask: summary is mandatory (Jira rejects empty summary).
            if stype == "add_subtask" and not (comment.strip() or text.strip()):
                _json_response(self, {"ok": False, "error": "부작업 제목이 비어 있습니다",
                                      "comment_ok": False, "description_ok": False}, 400)
                return

            # For add_subtask, the description belongs in the NEW subtask body,
            # not on the parent issue — so we must NOT call update_description on task_key.
            # For other types, description updates the issue identified by task_key.
            ok_desc = True
            err_desc = err_action = ""
            if description and task_key and stype != "add_subtask":
                ok_desc = provider.update_description(task_key, description)
                if not ok_desc: err_desc = _last_error()
            ok_action = False
            if stype == "comment" and task_key:
                if comment:
                    ok_action = provider.add_comment(task_key, comment)
                    if not ok_action: err_action = _last_error()
                else:
                    ok_action = True
            elif stype == "complete" and task_key:
                ok_action = provider.complete_issue(task_key, comment)
                if not ok_action: err_action = _last_error()
            elif stype == "transition" and task_key:
                ok_action = provider.transition_issue(task_key, "진행 중", comment)
                if not ok_action: err_action = _last_error()
            elif stype == "add_subtask" and task_key:
                try:
                    sub_fields = {
                        "project": {"key": task_key.split("-")[0]},
                        "parent": {"key": task_key},
                        "summary": comment or text,
                        "issuetype": {"name": "부작업"},
                    }
                    if description:
                        sub_fields["description"] = description
                    provider._request("POST", "/rest/api/2/issue", {"fields": sub_fields})
                    ok_action = True
                except Exception as e:
                    ok_action = False
                    err_action = str(e)
            ok = ok_action and ok_desc
            # Update suggestion status in JSON
            fpath, data = _load_suggestions()
            if fpath:
                for s in data.get("suggestions", []):
                    if s.get("id") == sid:
                        s["status"] = "approved" if ok else "failed"
                        s["suggested_text"] = comment
                        s["suggested_description"] = description
                        break
                _save_suggestions(fpath, data)
            _json_response(self, {"ok": ok, "comment_ok": ok_action, "description_ok": ok_desc,
                                  "comment_error": err_action, "description_error": err_desc})

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


class _ReusableHTTPServer(HTTPServer):
    # Allow re-binding port immediately after restart (avoids TIME_WAIT lockout)
    allow_reuse_address = True


if __name__ == "__main__":
    import os
    server = _ReusableHTTPServer(("127.0.0.1", PORT), ProxyHandler)
    print(f"Jira proxy server running at http://localhost:{PORT} (pid={os.getpid()})")
    print(f"Endpoints:")
    print(f"  GET  /api/sprint/tasks?sprint_id=152")
    print(f"  GET  /api/suggestions?date=2026-04-08")
    print(f"  POST /api/issue/{{key}}/comment        (body: comment, description)")
    print(f"  POST /api/issue/{{key}}/description    (body: description)")
    print(f"  POST /api/issue/{{key}}/complete       (body: comment, description)")
    print(f"  POST /api/issue/{{key}}/transition     (body: status, comment, description)")
    print(f"  POST /api/issue/create")
    print(f"  POST /api/suggestions/{{id}}/approve")
    print(f"  POST /api/suggestions/{{id}}/reject")
    print(f"  POST /api/regenerate                   (start multi-project regen)")
    print(f"  GET  /api/regenerate/status            (poll progress)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
