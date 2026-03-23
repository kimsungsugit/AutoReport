# Design Document - Morning Report Workflow

## Background

- The repository is updated daily in GitHub, and the next morning a short work summary should be visible when the PC starts

## Problem

- Recent work is stored in Git history, but there is no simple morning briefing generated from it

## Goals

- Generate a readable morning report from recent Git activity
- Include commits, changed files, and current working tree state
- Make execution simple enough to attach to Windows startup

## Non-Goals

- Replace full release notes or changelog management
- Depend on GitHub API access for basic reporting

## Proposed Design

- Add `scripts/generate_morning_report.py` to read local Git history
- Default report range to the previous day starting at `00:00`
- Write the generated Markdown report to `reports/morning_brief/`
- Add `scripts/run_morning_report.ps1` as the startup-friendly entry point

## Risks

- If local refs are stale, the report reflects local Git state rather than freshly fetched remote state
- Commit messages with low quality reduce summary quality

## Validation

- Run the PowerShell wrapper locally
- Confirm the report file is created and contains recent commits

## Open Questions

- Whether startup should also open the Markdown file automatically
- Whether the report should later include PR or issue links
