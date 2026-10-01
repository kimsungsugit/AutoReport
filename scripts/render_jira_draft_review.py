#!/usr/bin/env python3
"""Render Jira plan drafts as one review page (HTML) — reads files, never Jira.

Why this exists: a draft is approved by a person before anything is written
to Jira, and reading 20-odd issues as terminal text or as one wide table is
hard.  This lays each task out as a card — body, completion comment, subtasks —
followed by the proposed changes to existing issues and the open decisions.

The task content shown is the plan as ``register_jira_plan.py`` would send it
(same ``validate_plan`` normalization), so what is reviewed is what is written.

Usage
-----
    python scripts/render_jira_draft_review.py \
        --spec reports/.tmp_jira_draft_A.json --spec reports/.tmp_jira_draft_B.json \
        --updates reports/.tmp_jira_update.json [--out reports/jira_draft_review.html]

Display-only draft keys (ignored by registration): top-level ``project``, and
per task ``existing_key`` — the issue this task already is in Jira, when the
draft only adds subtasks under it.

Updates file: ``{"confirmed": [{key, project, summary, period, from, to,
comment, basis?, needs_check?}], "needs_decision": [{key, project, summary,
state, finding, question}], "notes": [str]}``
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from design_system import full_head  # noqa: E402
from workflow.jira_plan_register import validate_plan  # noqa: E402

_WEEKDAYS = "월화수목금토일"
_STATUS_CLASS = {"할 일": "pending", "진행 중": "in-progress", "종료 요청": "done", "완료": "done"}

REVIEW_SCRIPT = r"""
<script>
(function() {
  var KEY = 'jira-draft-review:' + document.body.dataset.reviewId;
  var saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) {}
  var items = [].slice.call(document.querySelectorAll('[data-review]'));
  function tally() {
    var n = {'승인': 0, '수정': 0, '제외': 0, '': 0};
    items.forEach(function(el) {
      var sel = el.querySelector('select');
      if (sel) n[sel.value] = (n[sel.value] || 0) + 1;
    });
    document.getElementById('jr-tally').textContent =
      '승인 ' + n['승인'] + ' · 수정 ' + n['수정'] + ' · 제외 ' + n['제외'] + ' · 미정 ' + n[''];
  }
  items.forEach(function(el) {
    var id = el.dataset.review, sel = el.querySelector('select'), memo = el.querySelector('textarea');
    var s = saved[id] || {};
    if (sel && s.v) sel.value = s.v;
    if (memo && s.m) memo.value = s.m;
    function save() {
      saved[id] = {v: sel ? sel.value : '', m: memo ? memo.value : ''};
      try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {}
      tally();
    }
    if (sel) sel.addEventListener('change', save);
    if (memo) memo.addEventListener('input', save);
  });
  tally();
  window.jrCopy = function() {
    var lines = items.map(function(el) {
      var sel = el.querySelector('select'), memo = el.querySelector('textarea');
      var verdict = sel ? (sel.value || '미정') : '';
      var note = memo ? memo.value.trim() : '';
      return '[' + el.dataset.label + '] ' + [verdict, note].filter(Boolean).join(' — ');
    });
    var text = lines.join('\n');
    var toast = document.getElementById('jr-toast');
    function done(ok) {
      toast.textContent = ok ? '검수 결과를 복사했습니다. 대화창에 붙여 넣어 주세요.' : '복사에 실패했습니다.';
      toast.classList.add('show');
      setTimeout(function() { toast.classList.remove('show'); }, 2600);
    }
    function fallback() {
      var box = document.createElement('textarea');
      box.value = text;
      document.body.appendChild(box);
      box.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      document.body.removeChild(box);
      done(ok);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function() { done(true); }, fallback);
    } else {
      fallback();
    }
  };
  var btn = document.getElementById('theme-toggle');
  var theme = localStorage.getItem('dashboard-theme');
  if (theme) document.documentElement.setAttribute('data-theme', theme);
  function isDark() {
    var current = document.documentElement.getAttribute('data-theme');
    return current === 'dark' || (!current && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  btn.textContent = isDark() ? 'Light' : 'Dark';
  window.toggleTheme = function() {
    var next = isDark() ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('dashboard-theme', next);
    btn.textContent = next === 'dark' ? 'Light' : 'Dark';
  };
})();
</script>
"""


def esc(value: object) -> str:
    return html.escape(str(value or ""))


def fmt_day(iso: str) -> str:
    """'2026-09-08' → '09-08(화)' — the weekday is what a date review checks."""
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return esc(iso)
    return f"{d:%m-%d}({_WEEKDAYS[d.weekday()]})"


def fmt_period(start: str, end: str) -> str:
    if not start and not end:
        return ""
    return f'<span class="jr-period">{fmt_day(start)} ~ {fmt_day(end)}</span>'


def body_html(text: str) -> str:
    """Jira wiki body → HTML: '* ' lines become one list, the rest paragraphs."""
    bullets, out = [], []

    def flush() -> None:
        if bullets:
            out.append('<ul class="jr-desc">' + "".join(f"<li>{esc(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("* "):
            bullets.append(line[2:])
        else:
            flush()
            out.append(f'<p class="jr-sub-desc">{esc(line)}</p>')
    flush()
    return "".join(out)


def done_html(note: str) -> str:
    if not note:
        return ""
    return f'<p class="jr-done"><span class="jr-label">완료 댓글</span>{esc(note)}</p>'


def status_chip(name: str) -> str:
    return f'<span class="jira-status {_STATUS_CLASS.get(name, "pending")}">{esc(name)}</span>'


def review_html(review_id: str, label: str, verdict: bool = True, placeholder: str = "수정할 내용") -> str:
    select = ('<select aria-label="판정"><option value="">미정</option><option>승인</option>'
              '<option>수정</option><option>제외</option></select>') if verdict else ""
    return (f'<div class="jr-review" data-review="{esc(review_id)}" data-label="{esc(label)}">{select}'
            f'<textarea placeholder="{esc(placeholder)}" aria-label="메모"></textarea></div>')


def load_spec(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    plan, errors = validate_plan(raw)
    if errors:
        raise SystemExit(f"초안 오류 ({path.name}):\n  " + "\n  ".join(errors))
    # validate_plan keeps task order, so the display-only keys line up by index.
    for task, src in zip(plan["tasks"], raw.get("tasks") or []):
        task["existing_key"] = str(src.get("existing_key") or "").strip()
    plan["project"] = str(raw.get("project") or "").strip()
    return plan


def task_card(task: dict, number: int | None) -> str:
    subs = task.get("subtasks") or []
    reused = task["existing_key"]
    if reused:
        label = f"{reused} 부작업 추가"
        head = (f'<div class="jr-head"><span class="jr-no">+</span><h3 class="jr-title">{esc(task["summary"])}</h3></div>'
                f'<div class="jr-meta"><span class="badge">기존 {esc(reused)}</span></div>'
                f'<p class="jr-reused">이미 있는 작업입니다. 작업 자체는 바꾸지 않고 아래 부작업만 새로 추가합니다.</p>')
        body = ""
    else:
        label = f"신규 {number}"
        head = (f'<div class="jr-head"><span class="jr-no">{number}</span><h3 class="jr-title">{esc(task["summary"])}</h3></div>'
                f'<div class="jr-meta">{fmt_period(task["start"], task["end"])}'
                f'<span class="badge badge-ok">신규 · 완료 등록</span></div>')
        body = body_html(task["description"]) + done_html(task.get("done_note", ""))
    sub_items = "".join(
        f'<li class="jr-sub"><div class="jr-sub-head"><span class="jr-sub-title">{esc(s["summary"])}</span>'
        f'{fmt_period(s.get("start", ""), s.get("end", ""))}</div>'
        f'{body_html(s.get("description", ""))}{done_html(s.get("done_note", ""))}</li>'
        for s in subs)
    subs_block = (f'<p class="jr-subs-title">부작업 {len(subs)}건{" (신규)" if reused else ""}</p>'
                  f'<ul class="jr-subs">{sub_items}</ul>') if subs else ""
    tone = "tone-plan" if reused else "tone-daily"
    review = review_html(label, label + " · " + task["summary"])
    return f'<article class="card jr-task {tone}">{head}{body}{subs_block}{review}</article>'


def update_card(item: dict) -> str:
    comment = item.get("comment") or ""
    comment_html = (f'<p class="jr-comment"><span class="jr-label">남길 댓글</span>{esc(comment)}</p>' if comment
                    else '<p class="jr-comment none">댓글 없이 상태만 바꿉니다.</p>')
    basis = f'<p class="jr-sub-desc">{esc(item["basis"])}</p>' if item.get("basis") else ""
    check = (f'<p class="jr-check"><span class="jr-label">확인 필요</span>{esc(item["needs_check"])}</p>'
             if item.get("needs_check") else "")
    period = f'<span class="jr-period">{esc(item["period"])}</span>' if item.get("period") else ""
    # An item already written to Jira stays on the page as a record, without review controls.
    applied = item.get("applied")
    applied_badge = f'<span class="badge badge-ok">Jira 반영 완료 · {esc(applied)}</span>' if applied else ""
    review = "" if applied else review_html(
        item["key"], item["key"] + " " + item.get("from", "") + "→" + item.get("to", ""))
    return (f'<article class="card jr-task tone-weekly"><div class="jr-head"><span class="epic-key">{esc(item["key"])}</span>'
            f'<h3 class="jr-title">{esc(item.get("summary", ""))}</h3></div>'
            f'<div class="jr-change">{status_chip(item.get("from", ""))}<span class="jr-arrow">→</span>'
            f'{status_chip(item.get("to", ""))}{period}<span class="jr-count">{esc(item.get("project", ""))}</span>'
            f'{applied_badge}</div>'
            f'{comment_html}{basis}{check}{review}</article>')


def decision_card(item: dict, number: int) -> str:
    return (f'<article class="card jr-task tone-jira2"><div class="jr-head"><span class="jr-no">{number}</span>'
            f'<h3 class="jr-title">{esc(item.get("summary", ""))}</h3></div>'
            f'<div class="jr-change"><span class="epic-key">{esc(item["key"])}</span>'
            f'<span class="jr-count">{esc(item.get("project", ""))}</span></div>'
            f'<ul class="jr-facts"><li><span>현재 상태</span><div>{esc(item.get("state", ""))}</div></li>'
            f'<li><span>확인한 사실</span><div>{esc(item.get("finding", ""))}</div></li>'
            f'<li><span>결정할 것</span><div class="jr-ask">{esc(item.get("question", ""))}</div></li></ul>'
            f'{review_html("decision-" + item["key"], "결정 " + item["key"], verdict=False, placeholder="결정 내용 (예: 종료일 10-14 로 연장)")}'
            f'</article>')


def render(plans: list[dict], updates: dict, today: str) -> str:
    new_tasks = sum(1 for p in plans for t in p["tasks"] if not t["existing_key"])
    new_subs = sum(len(t.get("subtasks") or []) for p in plans for t in p["tasks"])
    confirmed = updates.get("confirmed") or []
    applied = sum(1 for item in confirmed if item.get("applied"))
    decisions = updates.get("needs_decision") or []
    notes = updates.get("notes") or []

    parts = [full_head(f"Jira 업데이트 초안 검수 — {today}"),
             f'<body data-review-id="{esc(today)}">',
             '<button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()">Light</button>',
             '<div class="wrap jr-wrap">',
             '<header class="hero"><p class="eyebrow">검수용 초안</p>',
             f'<h1>Jira 업데이트 초안 — {esc(today)}</h1>',
             '<div class="jr-kpis">',
             f'<div class="hero-kpi"><span>신규 등록</span><strong>{new_tasks + new_subs}건</strong>'
             f'<div>작업 {new_tasks} · 부작업 {new_subs}</div></div>',
             f'<div class="hero-kpi"><span>기존 항목 변경</span><strong>{len(confirmed) - applied}건</strong>'
             f'<div>상태 전환 · 댓글{f" · 반영 완료 {applied}건" if applied else ""}</div></div>',
             f'<div class="hero-kpi"><span>결정 필요</span><strong>{len(decisions)}건</strong><div>일정 · 완료 여부</div></div>',
             '</div></header>',
             '<p class="jr-notice"><b>이 문서만으로는 Jira에 아무것도 올라가지 않습니다.</b> '
             '항목마다 판정과 메모를 적은 뒤 맨 아래 「검수 결과 복사」를 눌러 대화창에 붙여 넣어 주세요. '
             '승인된 항목만 등록합니다.</p>',
             '<nav class="jr-nav"><a href="#new">1. 신규 등록</a><a href="#updates">2. 기존 항목 상태 반영</a>'
             '<a href="#decisions">3. 결정 필요</a></nav>',
             '<h2 class="section-title" id="new">1. 신규 등록 — 실제 한 일</h2>',
             '<p class="section-copy">마지막 등록 이후 실제로 한 일을 큰 작업과 부작업으로 묶었습니다. '
             '모두 완료된 일이라 완료 댓글과 함께 등록합니다.</p>']
    number = 0
    for plan in plans:
        epic_key = plan["tasks"][0]["epic"]
        epic_name = (plan.get("epics") or {}).get(epic_key, "")
        fresh = sum(1 for t in plan["tasks"] if not t["existing_key"])
        subs = sum(len(t.get("subtasks") or []) for t in plan["tasks"])
        parts.append(f'<div class="jr-project"><span class="epic-key">{esc(epic_key)}</span>'
                     f'<span>{esc(plan["project"] or epic_name)}</span>'
                     f'<span class="jr-count">{esc(epic_name) + " · " if plan["project"] and epic_name else ""}'
                     f'작업 {fresh}건 · 부작업 {subs}건</span></div>')
        for task in plan["tasks"]:
            if task["existing_key"]:
                parts.append(task_card(task, None))
            else:
                number += 1
                parts.append(task_card(task, number))

    parts += ['<h2 class="section-title" id="updates">2. 기존 항목 상태 반영</h2>',
              '<p class="section-copy">이미 Jira에 있는 항목 중 실적으로 상태가 분명한 것입니다.</p>']
    parts += [update_card(item) for item in confirmed] or ['<p class="section-copy">해당 없음</p>']

    parts += ['<h2 class="section-title" id="decisions">3. 결정이 필요한 항목</h2>',
              '<p class="section-copy">저장소만으로는 판단할 수 없어 초안에 넣지 않았습니다. 결정을 적어 주시면 반영합니다.</p>']
    parts += [decision_card(item, i) for i, item in enumerate(decisions, 1)] or ['<p class="section-copy">해당 없음</p>']

    if notes:
        parts.append('<ul class="jr-foot">' + "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul>")
    parts += ['<div class="jr-bar"><span id="jr-tally"></span>'
              '<button class="jira-btn" onclick="jrCopy()">검수 결과 복사</button></div>',
              '</div>', '<div class="jira-toast" id="jr-toast"></div>', REVIEW_SCRIPT, '</body>', '</html>']
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Jira 계획 초안을 검수용 HTML 한 장으로 만듭니다 (Jira 접근 없음).")
    parser.add_argument("--spec", action="append", required=True, help="초안 JSON 경로 (여러 번 지정 가능)")
    parser.add_argument("--updates", default=None, help="기존 항목 변경·결정 필요 목록 JSON (선택)")
    parser.add_argument("--date", default=date.today().isoformat(), help="제목에 넣을 날짜 (기본: 오늘)")
    parser.add_argument("--out", default=None, help="출력 HTML 경로 (기본: reports/jira_draft_review_<날짜>.html)")
    args = parser.parse_args()

    plans = [load_spec(Path(p)) for p in args.spec]
    updates = json.loads(Path(args.updates).read_text(encoding="utf-8")) if args.updates else {}
    out = Path(args.out) if args.out else REPO_ROOT / "reports" / f"jira_draft_review_{args.date}.html"
    out.write_text(render(plans, updates, args.date), encoding="utf-8")
    print("검수 페이지:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
