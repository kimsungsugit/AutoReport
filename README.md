# AutoReport

Standalone startup reporting project extracted from the reporting tooling in `260105`.

## Included
- Daily, plan, weekly, monthly report generation
- Jira draft report generation
- Multi-project dashboard support
- HTML detail reports and portfolio dashboard

## Main Scripts
- `scripts/generate_periodic_reports.py`
- `scripts/generate_multi_project_reports.py`
- `scripts/run_startup_reports.ps1`

## Notes
- Configure project targets in `scripts/startup_projects.json`.
- Generated output is written under each configured output root.
