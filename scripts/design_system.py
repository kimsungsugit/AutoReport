"""Shared design system for all AutoReport HTML renderers.

Provides CSS variables, component classes, animations, and dark-mode support.
Import `DESIGN_CSS` and embed it inside <style> tags of any HTML output.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# SVG color palette (Python-side — inline SVG cannot use CSS var())
# ---------------------------------------------------------------------------
SVG_PALETTE = ["#264653", "#2a9d8f", "#e9c46a", "#f4a261", "#e76f51", "#6d597a"]
SVG_BG_DARK = "#12343b"          # matches --hero-a
SVG_STROKE = "#264653"           # default connector/arrow stroke
SVG_TEXT_DARK = "#1f2937"        # primary dark text on light fills
SVG_TEXT_DARKER = "#111827"      # count/label text
SVG_TEXT_LIGHT = "#fff"          # text on dark fills
SVG_TEXT_MUTED = "#4b5563"       # secondary/subtitle text
SVG_TEXT_ACCENT = "#7c2d12"      # warm brown accent text
SVG_IMPACT_STROKE = "#7c2d12"   # impact map connector color

# Subtitle tints (light pastel for each palette slot)
SVG_SUBTITLE_TINTS = ["#e6fffb", "#e6fffb", "#fef3c7", "#ffedd5", "#fee2e2", "#f3e8ff"]


def svg_text_color_for(fill: str) -> str:
    """Return appropriate text color for a given SVG fill background."""
    _DARK_FILLS = {"#264653", "#2a9d8f", "#6d597a", "#e76f51", "#12343b", "#7c2d12"}
    return SVG_TEXT_LIGHT if fill in _DARK_FILLS else SVG_TEXT_DARK


# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------
DESIGN_CSS = r"""
/* ========================================================================
   AutoReport Design System v2
   ======================================================================== */

/* --- Tokens ----------------------------------------------------------- */
:root {
  /* palette – warm parchment */
  --bg: #f5efe4;
  --bg-deep: #ede5d6;
  --paper: #fffdf9;
  --paper-alt: #f9f3e9;
  --ink: #17212b;
  --muted: #5f6b76;
  --subtle: #9ca3af;
  --line: #ddd2c1;
  --line-light: #ece3d4;

  /* accent */
  --accent: #0f4c5c;
  --accent-hover: #0b3a48;
  --accent-2: #d17a22;
  --accent-2-hover: #b86a1c;

  /* hero gradient */
  --hero-a: #12343b;
  --hero-b: #2c6e63;
  --hero-glow: rgba(209, 122, 34, 0.18);

  /* semantic */
  --ok: #1b7a5c;
  --ok-bg: #d8f3dc;
  --ok-ink: #1b4332;
  --warn: #d17a22;
  --warn-bg: #fef3c7;
  --warn-ink: #92400e;
  --danger: #c92a2a;
  --danger-bg: #fee2e2;
  --danger-ink: #991b1b;
  --info: #0f4c5c;
  --info-bg: #e0f2fe;
  --info-ink: #075985;

  /* tone bars (card left accent) */
  --tone-daily: #0f4c5c;
  --tone-plan: #d17a22;
  --tone-jira: #6c5ce7;
  --tone-jira2: #b56576;
  --tone-weekly: #2a9d8f;
  --tone-monthly: #8f5f3f;

  /* radii */
  --r-sm: 12px;
  --r-md: 18px;
  --r-lg: 24px;
  --r-xl: 28px;
  --r-pill: 999px;

  /* shadows */
  --shadow-sm: 0 4px 12px rgba(23,33,43,0.06);
  --shadow-md: 0 14px 30px rgba(23,33,43,0.08);
  --shadow-lg: 0 24px 60px rgba(18,52,59,0.18);
  --shadow-inner: inset 0 1px 0 rgba(255,255,255,0.65);
  --shadow-glow: 0 0 60px var(--hero-glow);

  /* motion */
  --ease: cubic-bezier(0.22, 1, 0.36, 1);
  --dur-fast: 0.15s;
  --dur: 0.25s;
  --dur-slow: 0.5s;
}

/* --- Dark mode -------------------------------------------------------- */
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1419;
    --bg-deep: #161c22;
    --paper: #1c2530;
    --paper-alt: #22303c;
    --ink: #e7e9ea;
    --muted: #8b98a5;
    --subtle: #6b7b8d;
    --line: #2f3b47;
    --line-light: #384956;
    --hero-a: #0d2a30;
    --hero-b: #1a4d44;
    --hero-glow: rgba(209, 122, 34, 0.12);
    --accent: #5eb8cc;
    --accent-hover: #7ec8d8;
    --accent-2: #e8a04c;
    --accent-2-hover: #f0b56a;
    --ok-bg: #0d3320;
    --ok-ink: #6ee7a8;
    --warn-bg: #3d2800;
    --warn-ink: #fbbf24;
    --danger-bg: #3b0d0d;
    --danger-ink: #fca5a5;
    --info-bg: #0a2e3e;
    --info-ink: #7dd3fc;
    --shadow-sm: 0 4px 12px rgba(0,0,0,0.25);
    --shadow-md: 0 14px 30px rgba(0,0,0,0.35);
    --shadow-lg: 0 24px 60px rgba(0,0,0,0.45);
    --shadow-inner: inset 0 1px 0 rgba(255,255,255,0.04);
  }
}

/* --- Reset & base ----------------------------------------------------- */
*, *::before, *::after { box-sizing: border-box; }

body {
  margin: 0;
  font-family: "Segoe UI", "Noto Sans KR", system-ui, -apple-system, sans-serif;
  color: var(--ink);
  background:
    radial-gradient(ellipse 80% 50% at 20% -10%, rgba(44,110,99,0.22), transparent),
    radial-gradient(ellipse 60% 40% at 85% 5%, var(--hero-glow), transparent),
    var(--bg);
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}

.wrap {
  max-width: 1380px;
  margin: 0 auto;
  padding: 32px 28px 60px;
}

/* --- Fade-in animation ------------------------------------------------ */
@keyframes fadeUp {
  from { opacity: 0; transform: translateY(18px); }
  to   { opacity: 1; transform: translateY(0); }
}
.anim-in {
  animation: fadeUp var(--dur-slow) var(--ease) both;
}
.anim-in:nth-child(2) { animation-delay: 0.06s; }
.anim-in:nth-child(3) { animation-delay: 0.12s; }
.anim-in:nth-child(4) { animation-delay: 0.18s; }
.anim-in:nth-child(5) { animation-delay: 0.24s; }
.anim-in:nth-child(6) { animation-delay: 0.30s; }

/* --- Hero ------------------------------------------------------------- */
.hero {
  position: relative;
  overflow: hidden;
  padding: 36px;
  background: linear-gradient(135deg, var(--hero-a), var(--hero-b));
  color: #fff;
  border-radius: var(--r-xl);
  box-shadow: var(--shadow-lg);
  margin-bottom: 26px;
}
.hero::before {
  content: "";
  position: absolute;
  inset: -40% -20% auto auto;
  width: 420px; height: 420px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(255,255,255,0.10), transparent 65%);
  pointer-events: none;
}
.hero::after {
  content: "";
  position: absolute;
  inset: auto auto -60px -80px;
  width: 300px; height: 300px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(209,122,34,0.15), transparent 60%);
  pointer-events: none;
}
.eyebrow {
  display: inline-block;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  padding: 7px 14px;
  border: 1px solid rgba(255,255,255,0.22);
  border-radius: var(--r-pill);
  margin-bottom: 14px;
  backdrop-filter: blur(4px);
  background: rgba(255,255,255,0.06);
}
.hero h1 {
  margin: 0 0 10px;
  font-size: clamp(28px, 4vw, 42px);
  line-height: 1.05;
  font-weight: 800;
}
.hero p {
  margin: 0;
  opacity: 0.92;
  max-width: 760px;
  font-size: 15px;
}
.hero-grid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 20px;
  align-items: end;
}
.hero-kpis {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.hero-kpi {
  background: rgba(255,255,255,0.08);
  border: 1px solid rgba(255,255,255,0.12);
  border-radius: var(--r-md);
  padding: 16px;
  backdrop-filter: blur(12px);
  transition: background var(--dur) var(--ease);
}
.hero-kpi:hover { background: rgba(255,255,255,0.14); }
.hero-kpi span {
  display: block;
  opacity: 0.8;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.hero-kpi strong {
  font-size: 28px;
  font-weight: 800;
}
.hero-links {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 20px;
}
.hero-links a {
  text-decoration: none;
  color: var(--hero-a);
  background: rgba(255,255,255,0.92);
  border: 1px solid rgba(255,255,255,0.35);
  padding: 11px 18px;
  border-radius: var(--r-pill);
  font-weight: 700;
  font-size: 13px;
  transition: all var(--dur) var(--ease);
}
.hero-links a:hover {
  background: #fff;
  transform: translateY(-1px);
  box-shadow: 0 6px 20px rgba(0,0,0,0.12);
}

/* --- Card ------------------------------------------------------------- */
.card {
  position: relative;
  background: linear-gradient(180deg, var(--paper), var(--paper-alt));
  border: 1px solid var(--line);
  border-radius: var(--r-xl);
  padding: 24px;
  margin-bottom: 22px;
  box-shadow: var(--shadow-md);
  transition: box-shadow var(--dur) var(--ease), transform var(--dur) var(--ease);
}
.card:hover {
  box-shadow: var(--shadow-lg);
  transform: translateY(-2px);
}
.card::before {
  content: "";
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 5px;
  border-radius: var(--r-xl) 0 0 var(--r-xl);
  background: var(--card-accent, var(--accent));
}
.tone-daily  { --card-accent: var(--tone-daily); }
.tone-plan   { --card-accent: var(--tone-plan); }
.tone-jira   { --card-accent: var(--tone-jira); }
.tone-jira2  { --card-accent: var(--tone-jira2); }
.tone-weekly { --card-accent: var(--tone-weekly); }
.tone-monthly{ --card-accent: var(--tone-monthly); }

.card-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 18px;
}
.card h2 {
  margin: 0 0 6px;
  font-size: 26px;
  line-height: 1.15;
  font-weight: 800;
}

/* --- Stats bar -------------------------------------------------------- */
.stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.stats div {
  background: linear-gradient(180deg, var(--paper), var(--paper-alt));
  border-radius: var(--r-md);
  padding: 16px;
  border: 1px solid var(--line);
  box-shadow: var(--shadow-inner);
  transition: transform var(--dur-fast) var(--ease);
}
.stats div:hover { transform: scale(1.02); }
.stats span {
  display: block;
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  margin-bottom: 8px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.stats strong {
  font-size: 28px;
  line-height: 1;
  font-weight: 800;
}

/* --- Panel (generic content box) -------------------------------------- */
.panel {
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 20px;
  background: linear-gradient(180deg, var(--paper), var(--paper-alt));
  overflow: auto;
  box-shadow: var(--shadow-inner);
}
.panel h3 {
  margin: 0 0 12px;
  font-size: 17px;
  font-weight: 700;
}
.panel ul { margin: 0; padding-left: 20px; }
.panel li { margin-bottom: 8px; line-height: 1.5; }

/* --- Grid layouts ----------------------------------------------------- */
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.grid-1-2 {
  display: grid;
  grid-template-columns: 1.2fr .8fr;
  gap: 16px;
  margin-bottom: 16px;
}
.grid-3 {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.stack { display: grid; gap: 16px; margin-bottom: 16px; }

/* --- Facet badges ----------------------------------------------------- */
.facet-group-inline { display: grid; gap: 8px; margin: 0 0 18px; }
.facet-strip { display: flex; gap: 10px; flex-wrap: wrap; }
.facet-badge {
  display: inline-flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px;
  border-radius: var(--r-md);
  background: linear-gradient(180deg, var(--paper), var(--paper-alt));
  border: 1px solid var(--line);
  min-width: 170px;
  box-shadow: var(--shadow-inner);
  transition: transform var(--dur-fast) var(--ease);
}
.facet-badge:hover { transform: translateY(-1px); }
.facet-badge-primary {
  background: linear-gradient(180deg, #fff1dd, #ffe3b8);
  border-color: #f59e0b;
}
.facet-badge-support {
  background: linear-gradient(180deg, var(--paper), #f5efe5);
  border-color: #d6c6aa;
}
.facet-badge strong {
  font-size: 12px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #7c2d12;
}
.facet-badge em {
  font-style: normal;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

/* --- Task board (Jira-style) ------------------------------------------ */
.task-board {
  display: grid;
  grid-template-columns: 1.1fr 1fr 1fr;
  gap: 14px;
  margin: 0 0 18px;
}
.task-box {
  position: relative;
  border-radius: var(--r-lg);
  padding: 18px;
  border: 1px solid var(--line);
  background: linear-gradient(180deg, var(--paper), var(--paper-alt));
  box-shadow: var(--shadow-inner);
}
.task-box.parent { background: linear-gradient(180deg, #f0fafa, #e8f5f2); }
.task-box.child  { background: linear-gradient(180deg, #fff9ef, #fbf1de); }
.task-box.result { background: linear-gradient(180deg, #fff4f1, #faece8); }
.task-box h4 {
  margin: 0 0 10px;
  font-size: 18px;
  line-height: 1.2;
  font-weight: 700;
}
.task-box p { margin: 0 0 10px; color: var(--muted); line-height: 1.5; }
.task-box ul { margin: 0; padding-left: 20px; }
.task-box li { margin-bottom: 8px; line-height: 1.5; }
.task-label {
  display: inline-block;
  margin-bottom: 10px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
  background: rgba(255,255,255,0.6);
  border: 1px solid var(--line);
  border-radius: var(--r-pill);
  padding: 6px 10px;
}

/* --- Badges / pills --------------------------------------------------- */
.badge {
  display: inline-flex;
  align-items: center;
  padding: 7px 12px;
  border-radius: var(--r-pill);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  border: 1px solid var(--line);
  background: var(--paper);
}
.badge-ok   { background: var(--ok-bg); color: var(--ok-ink); border-color: #a7f3d0; }
.badge-warn { background: var(--warn-bg); color: var(--warn-ink); border-color: #fde68a; }

/* --- Mini chips (portfolio cards) ------------------------------------- */
.mini-chip {
  display: inline-flex;
  align-items: center;
  padding: 6px 10px;
  border-radius: var(--r-pill);
  font-size: 11px;
  font-weight: 700;
  border: 1px solid var(--line);
  background: var(--paper);
}
.mini-chip.primary { background: #fff1dd; border-color: #f59e0b; color: #9a3412; }
.mini-chip.support { background: var(--paper-alt); border-color: #d6c6aa; color: var(--muted); }

/* --- File link button ------------------------------------------------- */
.file-link {
  color: var(--accent);
  text-decoration: none;
  font-weight: 700;
  padding: 10px 16px;
  border-radius: var(--r-pill);
  background: var(--paper-alt);
  border: 1px solid var(--line);
  white-space: nowrap;
  font-size: 13px;
  transition: all var(--dur) var(--ease);
}
.file-link:hover {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}

/* --- Actions bar ------------------------------------------------------ */
.actions {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin: 0 0 20px;
}
.actions a, .btn {
  text-decoration: none;
  color: var(--accent);
  background: var(--paper-alt);
  border: 1px solid var(--line);
  padding: 10px 16px;
  border-radius: var(--r-pill);
  font-weight: 700;
  font-size: 13px;
  cursor: pointer;
  transition: all var(--dur) var(--ease);
}
.actions a:hover, .btn:hover {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
  transform: translateY(-1px);
}

/* --- Checklist -------------------------------------------------------- */
.check-label {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  cursor: pointer;
}
.check-input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}
.check-box {
  width: 20px; height: 20px;
  border: 2px solid var(--accent-2);
  border-radius: 6px;
  flex: 0 0 auto;
  margin-top: 2px;
  background: linear-gradient(180deg, #fff9ef, #f8eddc);
  transition: all var(--dur-fast) var(--ease);
}
.check-input:checked + .check-box {
  background: linear-gradient(180deg, #e8faf6, #dff5ef);
  border-color: var(--ok);
  position: relative;
}
.check-input:checked + .check-box::after {
  content: "";
  position: absolute;
  left: 5px; top: 1px;
  width: 5px; height: 10px;
  border: solid #166534;
  border-width: 0 2.5px 2.5px 0;
  transform: rotate(45deg);
}
.check-text { flex: 1; }
.check-input:checked ~ .check-text {
  color: var(--muted);
  text-decoration: line-through;
}

/* --- Tables ----------------------------------------------------------- */
.table-wrap { overflow: auto; border-radius: var(--r-lg); border: 1px solid var(--line); }
table { width: 100%; border-collapse: collapse; background: var(--paper); }
th, td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--line-light);
  text-align: left;
  vertical-align: top;
}
th {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
  background: var(--paper-alt);
  position: sticky;
  top: 0;
}
tr:hover td { background: rgba(15,76,92,0.03); }

/* --- Code ------------------------------------------------------------- */
code {
  background: var(--bg-deep);
  padding: 2px 7px;
  border-radius: 8px;
  font-size: 0.9em;
}

/* --- SVG charts ------------------------------------------------------- */
.chart-wrap {
  background: linear-gradient(180deg, var(--paper), var(--paper-alt));
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 22px;
}
.chart-wrap h3 { margin: 0 0 12px; font-size: 17px; font-weight: 700; }
.chart, .flow { width: 100%; height: auto; }

/* --- SVG light wrapper (stays light even in dark mode) ---------------- */
.chart-wrap--light {
  background: linear-gradient(180deg, var(--paper), var(--paper-alt));
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 22px;
}
.chart-wrap--light h3 { margin: 0 0 12px; font-size: 17px; font-weight: 700; }
@media (prefers-color-scheme: dark) {
  .chart-wrap--light {
    background: linear-gradient(180deg, #f5f0e8, #ede5d6);
    color: #17212b;
    border-color: #ddd2c1;
  }
}

/* --- Detail panel (used in detail HTML) ------------------------------- */
.detail-panel {
  background: linear-gradient(180deg, var(--paper), var(--paper-alt));
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 22px;
  box-shadow: var(--shadow-md);
}
.detail-panel h3 { margin: 0 0 12px; font-size: 20px; font-weight: 700; }
.detail-panel ul { margin: 0; padding-left: 20px; }
.detail-panel li { margin-bottom: 8px; line-height: 1.5; }

/* --- Tooltip ---------------------------------------------------------- */
[data-tooltip] {
  position: relative;
  cursor: help;
}
[data-tooltip]::after {
  content: attr(data-tooltip);
  position: absolute;
  bottom: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%) scale(0.96);
  padding: 8px 12px;
  border-radius: var(--r-sm);
  background: var(--ink);
  color: #fff;
  font-size: 12px;
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease);
}
[data-tooltip]:hover::after {
  opacity: 1;
  transform: translateX(-50%) scale(1);
}

/* --- Section titles --------------------------------------------------- */
.section-title {
  margin: 36px 0 14px;
  font-size: 26px;
  font-weight: 800;
  line-height: 1.15;
}
.section-copy {
  margin: 0 0 20px;
  color: var(--muted);
  line-height: 1.6;
  max-width: 720px;
}

/* --- Portfolio grid --------------------------------------------------- */
.portfolio-grid { display: grid; grid-template-columns: 1fr; gap: 20px; }
.project-board {
  background: linear-gradient(180deg, var(--paper), var(--paper-alt));
  border: 1px solid var(--line);
  border-radius: var(--r-xl);
  padding: 24px;
  box-shadow: var(--shadow-md);
  margin-bottom: 20px;
}
.project-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 18px;
}
.project-head h2 { margin: 0 0 6px; }
.project-head p { margin: 0; color: var(--muted); font-size: 13px; }
.project-links { display: flex; gap: 10px; flex-wrap: wrap; }
.project-links a {
  text-decoration: none;
  color: var(--accent);
  background: var(--paper-alt);
  border: 1px solid var(--line);
  padding: 10px 14px;
  border-radius: var(--r-pill);
  font-weight: 700;
  font-size: 13px;
  transition: all var(--dur) var(--ease);
}
.project-links a:hover {
  background: var(--accent);
  color: #fff;
}

/* --- Hero meta grid (detail report) ----------------------------------- */
.meta {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin: 20px 0 0;
}
.meta div {
  background: rgba(255,255,255,.1);
  border: 1px solid rgba(255,255,255,.14);
  border-radius: var(--r-md);
  padding: 14px;
}
.meta span { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; opacity: .8; margin-bottom: 8px; }
.meta strong { font-size: 22px; }
.mini-meta { margin: -4px 0 10px; color: var(--muted); font-size: 12px; }

/* --- Grid aliases ----------------------------------------------------- */
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.grid-wide { display: grid; grid-template-columns: 1.2fr .8fr; gap: 16px; margin-bottom: 16px; }
.chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.content-stack { display: grid; gap: 16px; margin-bottom: 16px; }

/* --- Image slots & evidence ------------------------------------------- */
.image-panel { grid-column: 1 / -1; }
.image-slots { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.image-slot {
  min-height: 240px;
  border: 2px dashed var(--line);
  border-radius: var(--r-lg);
  padding: 18px;
  background: linear-gradient(180deg, var(--paper), var(--paper-alt));
  display: flex; flex-direction: column; gap: 12px;
}
.image-slot span { display: block; font-size: 16px; font-weight: 700; margin-bottom: 4px; }
.image-slot small { color: var(--muted); line-height: 1.5; }
.evidence-meta strong { display: block; margin-bottom: 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--accent-2); }
.evidence-meta ul { margin: 0; padding-left: 18px; }
.evidence-meta li { margin-bottom: 6px; line-height: 1.45; }
.evidence-meta a { color: var(--accent); font-weight: 700; text-decoration: none; }
.evidence-placeholder { margin-top: auto; border: 1px dashed var(--line); border-radius: 14px; padding: 12px; font-size: 12px; color: var(--muted); background: rgba(255,255,255,.65); }

/* --- Timeline --------------------------------------------------------- */
.timeline { display: grid; gap: 14px; }
.timeline-step { display: grid; grid-template-columns: auto 1fr; gap: 14px; align-items: start; }
.timeline-marker { width: 34px; height: 34px; border-radius: 50%; background: var(--hero-a); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; }
.timeline-copy strong { display: block; margin-bottom: 4px; }
.timeline-copy span { color: var(--muted); font-size: 12px; }

/* --- Area cards ------------------------------------------------------- */
.area-section { margin-top: 16px; }
.area-stack { display: grid; gap: 14px; }
.area-card { border: 1px solid var(--line); border-radius: var(--r-lg); padding: 18px; background: linear-gradient(180deg, var(--paper), var(--paper-alt)); transition: transform var(--dur-fast) var(--ease); }
.area-card:hover { transform: translateY(-2px); }
.area-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 12px; }
.area-head h3 { margin: 0; font-size: 20px; }
.area-head span { color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; }
.area-badges { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 12px; }
.area-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
.area-grid h4 { margin: 0 0 8px; font-size: 14px; color: var(--accent-2); text-transform: uppercase; letter-spacing: .06em; }
.area-grid ul { margin: 0; padding-left: 18px; }
.area-grid li { margin-bottom: 6px; line-height: 1.45; }

/* --- Mini badges (priority/impact/risk) ------------------------------- */
.mini-badge { display: inline-flex; align-items: center; padding: 7px 10px; border-radius: var(--r-pill); font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; border: 1px solid var(--line); background: var(--paper); }
.priority-high, .impact-high, .risk-high { background: var(--danger-bg); color: var(--danger-ink); border-color: #fecaca; }
.priority-medium, .impact-medium, .risk-medium { background: var(--warn-bg); color: var(--warn-ink); border-color: #fde68a; }
.priority-low, .impact-low, .risk-low { background: var(--ok-bg); color: var(--ok-ink); border-color: #bbf7d0; }
.owner { background: var(--info-bg); color: var(--info-ink); border-color: #bae6fd; }
.status { background: #ede9fe; color: #5b21b6; border-color: #ddd6fe; }

/* --- Facet variants (detail report) ----------------------------------- */
.facet-groups { display: grid; gap: 14px; margin: 0 0 18px; }
.facet-group h3 { margin: 0 0 8px; font-size: 13px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
.facet { display: inline-flex; flex-direction: column; gap: 4px; min-width: 170px; padding: 12px 14px; border-radius: var(--r-md); border: 1px solid var(--line); background: linear-gradient(180deg, var(--paper), var(--paper-alt)); transition: transform var(--dur-fast) var(--ease); }
.facet:hover { transform: translateY(-1px); }
.facet-primary { background: linear-gradient(180deg, #fff1dd, #ffe3b8); border-color: #f59e0b; }
.facet-support { background: linear-gradient(180deg, var(--paper), #f5efe5); border-color: #d6c6aa; }
.facet strong { font-size: 13px; text-transform: uppercase; color: var(--accent-2); }
.facet em { font-style: normal; color: var(--muted); font-size: 12px; line-height: 1.45; }

/* --- Facet chips (portfolio) ------------------------------------------ */
.facet-zone { display: grid; gap: 8px; margin: 0 0 14px; }
.facet-title { font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
.facet-row { display: flex; gap: 8px; flex-wrap: wrap; }
.facet-chip { display: inline-flex; align-items: center; padding: 8px 10px; border-radius: var(--r-pill); font-size: 12px; font-weight: 700; border: 1px solid var(--line); max-width: 100%; overflow-wrap: anywhere; word-break: break-word; transition: transform var(--dur-fast) var(--ease); }
.facet-chip:hover { transform: translateY(-1px); }
.facet-chip.primary { background: #fff1dd; border-color: #f59e0b; color: #9a3412; }
.facet-chip.support { background: var(--paper-alt); border-color: #d6c6aa; color: var(--muted); }

/* --- State badges ----------------------------------------------------- */
.state { display: inline-flex; align-items: center; border-radius: var(--r-pill); padding: 6px 10px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; }
.state.done { background: var(--ok-bg); color: var(--ok-ink); }
.state.planned { background: var(--warn-bg); color: var(--warn-ink); }

/* --- Subtask row (dashboard) ------------------------------------------ */
.subtask-row { display: grid; grid-template-columns: auto 1fr auto; gap: 12px; align-items: start; }
.subtask-copy strong { display: block; margin-bottom: 4px; font-size: 14px; }
.subtask-copy span { display: block; color: var(--muted); font-size: 12px; }
.check { width: 26px; height: 26px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--line); background: var(--paper); color: var(--subtle); font-weight: 700; }
.check.done { background: var(--ok); border-color: var(--ok); color: #fff; }

/* --- Task columns (portfolio) ----------------------------------------- */
.task-col { min-width: 0; border: 1px solid var(--line); border-radius: var(--r-lg); padding: 18px; background: linear-gradient(180deg, var(--paper), var(--paper-alt)); }
.task-col.parent { background: linear-gradient(180deg, #f0fafa, #e8f5f2); }
.task-col.subtasks { background: linear-gradient(180deg, #fff9ef, #fbf1de); }
.task-col.result { background: linear-gradient(180deg, #fff4f1, #faece8); }
.task-col h3 { margin: 0 0 10px; font-size: 20px; line-height: 1.2; }
.task-col p { margin: 0 0 12px; color: var(--muted); line-height: 1.55; }
.task-col ul { margin: 0; padding-left: 20px; }
.task-col li { margin-bottom: 8px; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word; }

/* --- Snapshot / source blocks ----------------------------------------- */
.snapshot-block { margin: 0 0 14px; padding: 12px 14px; border-radius: var(--r-md); background: rgba(255,255,255,.65); border: 1px solid var(--line); }
.snapshot-block ul { margin: 0; padding-left: 18px; }
.snapshot-block li { margin-bottom: 6px; line-height: 1.5; color: var(--muted); }
.source-box { margin: 0 0 14px; padding: 12px 14px; border-radius: var(--r-md); background: var(--paper-alt); border: 1px solid var(--line); }
.source-title { margin: 0 0 8px; font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
.source-box ul { margin: 0; padding-left: 18px; }
.source-box li { margin-bottom: 6px; color: var(--muted); line-height: 1.5; }

/* --- Work type / task tags -------------------------------------------- */
.work-type { display: inline-flex; margin-bottom: 10px; padding: 8px 12px; border-radius: var(--r-pill); background: #fff1dd; border: 1px solid #f59e0b; font-size: 12px; font-weight: 800; color: #9a3412; }
.work-type-line { margin: 14px 0 8px; font-size: 13px; color: var(--muted); }
.task-tag { display: inline-block; margin-bottom: 10px; padding: 6px 10px; border-radius: var(--r-pill); border: 1px solid var(--line); background: rgba(255,255,255,.7); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
.task-links { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
.task-links a { text-decoration: none; color: var(--accent-2); background: #fff7ed; border: 1px solid #f5d0a9; padding: 9px 12px; border-radius: var(--r-pill); font-weight: 700; transition: all var(--dur) var(--ease); }
.task-links a:hover { background: var(--accent-2); color: #fff; }

/* --- Subtask checklist (portfolio) ------------------------------------ */
.subtask-list { list-style: none; padding-left: 0; }
.subtask-item { list-style: none; margin-bottom: 12px; }
.subtask-item strong { display: block; margin-bottom: 4px; font-size: 14px; overflow-wrap: anywhere; word-break: break-word; }
.subtask-item span { display: block; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; word-break: break-word; }

/* --- Links bar -------------------------------------------------------- */
.links { display: flex; gap: 12px; flex-wrap: wrap; }
.links a { text-decoration: none; color: var(--accent); background: var(--info-bg); border: 1px solid var(--line); padding: 10px 14px; border-radius: var(--r-pill); font-weight: 600; transition: all var(--dur) var(--ease); }
.links a:hover { background: var(--accent); color: #fff; }

/* --- Card head (portfolio) -------------------------------------------- */
.head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
.head h2 { margin: 0 0 6px; }
.head p { margin: 0; color: var(--muted); font-size: 13px; }
.message { margin: 16px 0; line-height: 1.5; }
.summary-line { margin: 0 0 12px; line-height: 1.6; color: var(--ink); font-weight: 600; }
.mini-facets { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 14px; }

/* --- Overview cards (history) ----------------------------------------- */
.overview { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; margin-bottom: 22px; }
.overview-card { background: var(--paper); border: 1px solid var(--line); border-radius: var(--r-lg); padding: 22px; box-shadow: var(--shadow-md); transition: transform var(--dur-fast) var(--ease); }
.overview-card:hover { transform: translateY(-2px); }
.overview-head { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; margin-bottom: 10px; }
.overview-head h2 { margin: 0 0 4px; }
.overview-head p { margin: 0; color: var(--muted); font-size: 13px; }
.overview-card ul { margin: 0 0 14px; padding-left: 18px; }
.overview-card li { margin-bottom: 6px; line-height: 1.5; }
.mini-status { padding: 8px 12px; border-radius: var(--r-pill); background: var(--warn-bg); border: 1px solid #e6c8a2; font-size: 12px; font-weight: 700; color: var(--warn-ink); }
.table-panel { background: var(--paper); border: 1px solid var(--line); border-radius: var(--r-xl); padding: 24px; box-shadow: var(--shadow-md); margin-bottom: 18px; overflow: auto; }
td a { color: var(--accent); font-weight: 700; text-decoration: none; }

/* --- Chart large ------------------------------------------------------ */
.chart-large { min-height: 320px; }

/* --- Accessibility ---------------------------------------------------- */
.check-input:focus-visible + .check-box {
  outline: 3px solid var(--accent);
  outline-offset: 2px;
}
code { overflow-wrap: anywhere; word-break: break-word; }
.panel li, .detail-panel li, .task-box li, .task-col li { overflow-wrap: anywhere; word-break: break-word; }
.check-label { min-height: 44px; padding: 4px 0; }
.actions a, .btn, .file-link { min-height: 44px; display: inline-flex; align-items: center; }
.skip-link {
  position: absolute; left: -9999px; top: auto;
  padding: 12px 18px; background: var(--accent); color: #fff;
  border-radius: var(--r-md); font-weight: 700; z-index: 999;
  text-decoration: none;
}
.skip-link:focus { left: 16px; top: 16px; }

/* --- Reduced motion --------------------------------------------------- */
@media (prefers-reduced-motion: reduce) {
  .anim-in { animation: none; }
  *, *::before, *::after { transition-duration: 0.01ms !important; }
}

/* --- Responsive ------------------------------------------------------- */
@media (max-width: 960px) {
  .hero-grid { grid-template-columns: 1fr; }
  .grid, .grid-2, .grid-1-2, .grid-wide { grid-template-columns: 1fr; }
  .grid-3 { grid-template-columns: 1fr; }
  .task-board { grid-template-columns: 1fr; }
  .stats { grid-template-columns: 1fr 1fr; }
  .card-head { flex-direction: column; align-items: flex-start; }
  .meta { grid-template-columns: 1fr 1fr; }
  .chart-grid { grid-template-columns: 1fr; }
  .image-slots { grid-template-columns: 1fr; }
  .area-grid { grid-template-columns: 1fr; }
  .overview { grid-template-columns: 1fr; }
}
@media (max-width: 600px) {
  .wrap { padding: 16px 12px 32px; }
  .hero { padding: 22px; }
  .stats { grid-template-columns: 1fr; }
  body { font-size: 15px; }
  .chart-wrap, .chart-wrap--light { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .chart, .flow { min-height: 180px; }
}

/* --- Print ------------------------------------------------------------ */
@media print {
  body { background: #fff; }
  .wrap { max-width: none; padding: 0; }
  .hero, .card, .panel, .detail-panel, .chart-wrap, .project-board {
    box-shadow: none;
    break-inside: avoid;
  }
  .actions a, .btn { border-color: #bbb; }
}
"""

# ---------------------------------------------------------------------------
# Checklist persistence JS (localStorage based)
# ---------------------------------------------------------------------------
CHECKLIST_JS = r"""
<script>
(function () {
  const PREFIX = "autoreport-check:";
  document.querySelectorAll(".check-input[data-checklist-id]").forEach(function (el) {
    var key = PREFIX + el.dataset.checklistId;
    try { el.checked = window.localStorage.getItem(key) === "1"; } catch (e) {}
    el.addEventListener("change", function () {
      try { window.localStorage.setItem(key, el.checked ? "1" : "0"); } catch (e) {}
    });
  });
})();
</script>
"""


def css_tag() -> str:
    """Return the design system CSS wrapped in a <style> tag."""
    return f"<style>\n{DESIGN_CSS}\n</style>"


def full_head(title: str, extra_css: str = "") -> str:
    """Return a complete <head> block with charset, viewport, design system, and optional extra CSS."""
    parts = [
        '<!doctype html>',
        '<html lang="ko">',
        '<head>',
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        f'  <title>{title}</title>',
        f'  <style>\n{DESIGN_CSS}\n{extra_css}\n  </style>',
        '</head>',
    ]
    return "\n".join(parts)
