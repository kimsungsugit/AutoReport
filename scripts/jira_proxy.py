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


def _is_valid_date(s: str) -> bool:
    """True if `s` is empty (means clear/skip) or a YYYY-MM-DD ISO date."""
    if not s:
        return True
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


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
            # Attach a freshly-rendered Gantt SVG so the client can replace the
            # chart in-place after task dates change. Lazy import keeps boot light.
            try:
                from scripts.generate_periodic_reports import svg_sprint_gantt
                sprint = tasks.get("sprint", {}) if isinstance(tasks, dict) else {}
                tasks["gantt_html"] = svg_sprint_gantt(tasks.get("tasks", []), sprint)
            except Exception:
                tasks["gantt_html"] = ""
            _json_response(self, tasks)

        elif parsed.path == "/api/jira/epics":
            # Populates the Epic dropdown when creating a Task at project root.
            # ?project=<KEY> overrides the proxy's default project so dashboards
            # rendering multiple boards (APPL, etc.) hit the right project.
            qs = parse_qs(parsed.query)
            project_key = qs.get("project", [""])[0]
            try:
                epics = (
                    provider.list_epics(project_key) if hasattr(provider, "list_epics") else []
                )
            except Exception as e:
                _json_response(self, {"epics": [], "error": str(e)}, 500)
                return
            _json_response(self, {"epics": epics})

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

        # POST /api/issue/{key}/dates  (body: start, end — both YYYY-MM-DD or empty to clear)
        elif path.startswith("/api/issue/") and path.endswith("/dates"):
            key = path.split("/")[3]
            start = (body.get("start") or "").strip()
            end = (body.get("end") or "").strip()
            if not _is_valid_date(start) or not _is_valid_date(end):
                _json_response(self, {"ok": False, "error": "날짜 형식은 YYYY-MM-DD 여야 합니다"}, 400)
                return
            if start and end and start > end:
                _json_response(self, {"ok": False, "error": "시작일이 종료일보다 늦을 수 없습니다"}, 400)
                return
            ok = provider.update_dates(key, start, end)
            err = _last_error() if not ok else ""
            _json_response(self, {"ok": ok, "key": key, "start": start, "end": end, "error": err})

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
        # Used by the Sprint Board "+ Sub" button. The suggestion-approve flow
        # has its own subtask path that already inherits required customfields
        # from the parent — this endpoint missed that, so the +Sub button 400'd
        # on tasks with required cf_10230/10900/11100.
        elif path == "/api/issue/create":
            parent_key = body.get("parent_key", "")
            summary = body.get("summary", "")
            description = body.get("description", "")
            if not parent_key or not summary:
                _json_response(self, {"error": "parent_key and summary required"}, 400)
                return
            try:
                fields: dict = {
                    "project": {"key": parent_key.split("-")[0]},
                    "parent": {"key": parent_key},
                    "summary": summary,
                    "issuetype": {"name": "부작업"},
                }
                if description:
                    fields["description"] = description
                # Inherit required customfields from parent (mirrors the
                # suggestion-approve add_subtask branch). If the parent fetch
                # fails or the fields are unset on the parent, Jira's 400
                # surfaces the missing-field message to the caller as before.
                try:
                    required_cfs = ("customfield_10230", "customfield_10900", "customfield_11100")
                    parent = provider._request(
                        "GET",
                        f"/rest/api/2/issue/{parent_key}?fields={','.join(required_cfs)}",
                    )
                    pfields = (parent or {}).get("fields", {}) or {}
                    for cf in required_cfs:
                        val = pfields.get(cf)
                        if val is None:
                            continue
                        if isinstance(val, dict) and "id" in val:
                            fields[cf] = {"id": val["id"]}
                        elif isinstance(val, list):
                            fields[cf] = [
                                {"id": v["id"]} if isinstance(v, dict) and "id" in v else v
                                for v in val
                            ]
                        else:
                            fields[cf] = val
                except Exception:
                    pass
                result = provider._request("POST", "/rest/api/2/issue", {"fields": fields})
                _json_response(self, {"ok": True, "key": result.get("key", "")})
            except Exception as e:
                _json_response(self, {"ok": False, "error": str(e)}, 500)

        # POST /api/jira/issue/create  (creates Epic or Task at project root)
        # body: {type: "epic"|"task", summary, description, start, end, epic_key?}
        elif path == "/api/jira/issue/create":
            issuetype = (body.get("type") or "").strip()
            summary = (body.get("summary") or "").strip()
            description = body.get("description") or ""
            start = (body.get("start") or "").strip()
            end = (body.get("end") or "").strip()
            epic_key = (body.get("epic_key") or "").strip()
            if not summary:
                _json_response(self, {"ok": False, "error": "제목은 필수입니다"}, 400)
                return
            if issuetype.lower() not in ("epic", "task", "에픽", "작업"):
                _json_response(self, {"ok": False, "error": "type은 epic 또는 task여야 합니다"}, 400)
                return
            if not _is_valid_date(start) or not _is_valid_date(end):
                _json_response(self, {"ok": False, "error": "날짜 형식은 YYYY-MM-DD 여야 합니다"}, 400)
                return
            if start and end and start > end:
                _json_response(self, {"ok": False, "error": "시작일이 종료일보다 늦을 수 없습니다"}, 400)
                return
            project_key = (body.get("project_key") or "").strip()
            report_required = (body.get("report_required") or "").strip()
            try:
                new_key = (
                    provider.create_issue(
                        issuetype, summary, description, start, end, epic_key,
                        project_key=project_key,
                        report_required=report_required,
                    )
                    if hasattr(provider, "create_issue") else ""
                )
            except Exception as e:
                _json_response(self, {"ok": False, "error": str(e)}, 500)
                return
            if new_key:
                _json_response(self, {"ok": True, "key": new_key})
            else:
                err = _last_error() or "이슈 생성 실패"
                _json_response(self, {"ok": False, "error": err}, 500)

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
                    # Inherit required custom fields from parent so Jira does not 400.
                    # 사내 Jira 필수: customfield_10230(Start date), customfield_10900(End date),
                    # customfield_11100(주간보고 사항). 부모에 값이 있으면 그대로 상속한다.
                    try:
                        required_cfs = ("customfield_10230", "customfield_10900", "customfield_11100")
                        parent = provider._request(
                            "GET",
                            f"/rest/api/2/issue/{task_key}?fields={','.join(required_cfs)}",
                        )
                        pfields = (parent or {}).get("fields", {}) or {}
                        for cf in required_cfs:
                            val = pfields.get(cf)
                            if val is None:
                                continue
                            # Select-list options come back as {id, value, self, ...} —
                            # only id is needed (and accepted) for a write payload.
                            if isinstance(val, dict) and "id" in val:
                                sub_fields[cf] = {"id": val["id"]}
                            elif isinstance(val, list):
                                sub_fields[cf] = [
                                    {"id": v["id"]} if isinstance(v, dict) and "id" in v else v
                                    for v in val
                                ]
                            else:
                                sub_fields[cf] = val
                    except Exception:
                        # If we cannot fetch the parent, fall through and let Jira's
                        # own 400 surface the missing-field message to the caller.
                        pass
                    # User-supplied start/end take precedence over parent inheritance.
                    # Validated YYYY-MM-DD strings are stored verbatim; empty values
                    # leave whatever the parent inheritance set above untouched.
                    user_start = (body.get("start") or "").strip()
                    user_end = (body.get("end") or "").strip()
                    if not _is_valid_date(user_start) or not _is_valid_date(user_end):
                        raise ValueError("날짜 형식은 YYYY-MM-DD 여야 합니다")
                    if user_start and user_end and user_start > user_end:
                        raise ValueError("시작일이 종료일보다 늦을 수 없습니다")
                    if user_start:
                        sub_fields["customfield_10230"] = user_start
                    if user_end:
                        sub_fields["customfield_10900"] = user_end
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

        # POST /api/proxy/restart
        # Spawn a detached child first (so the response can report success/failure
        # honestly), reply, then close the listening socket and exit self. The new
        # process re-binds the same port via _ReusableHTTPServer.allow_reuse_address.
        # The browser polls /api/regenerate/status afterwards to confirm liveness.
        elif path == "/api/proxy/restart":
            child_pid: int | None = None
            spawn_err = ""
            try:
                flags = 0
                if sys.platform == "win32":
                    DETACHED_PROCESS = 0x00000008
                    CREATE_NEW_PROCESS_GROUP = 0x00000200
                    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                # Redirect child stdout/stderr so its startup banner & errors are
                # not silently dropped — useful when spawn appears to "succeed"
                # but the child crashes immediately on bind.
                log_path = REPO_ROOT / "reports" / ".proxy.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_fp = open(log_path, "ab", buffering=0)
                try:
                    proc = subprocess.Popen(
                        # -u: unbuffered stdout/stderr so the child's startup
                        # banner reaches .proxy.log immediately rather than
                        # sitting in Python's block buffer.
                        [sys.executable, "-u", str(Path(__file__).resolve())],
                        cwd=str(REPO_ROOT),
                        creationflags=flags,
                        stdin=subprocess.DEVNULL,
                        stdout=log_fp,
                        stderr=log_fp,
                        close_fds=True,
                    )
                    child_pid = proc.pid
                finally:
                    # Parent does not need the log fd — child inherited its own copy.
                    try: log_fp.close()
                    except Exception: pass
            except Exception as e:
                spawn_err = str(e)
            _json_response(self, {
                "ok": child_pid is not None,
                "pid": os.getpid(),
                "child_pid": child_pid,
                "error": spawn_err,
            })
            try:
                self.wfile.flush()
            except Exception:
                pass
            if child_pid is not None:
                import threading
                def _do_exit():
                    time.sleep(0.4)
                    # Release the listening socket explicitly so the child can
                    # bind without depending on TIME_WAIT / SO_REUSEADDR timing.
                    sock = getattr(self.server, "socket", None)
                    if sock is not None:
                        try: sock.close()
                        except Exception: pass
                    os._exit(0)
                threading.Thread(target=_do_exit, daemon=True).start()

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
