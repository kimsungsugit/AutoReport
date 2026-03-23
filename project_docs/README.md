# Project Documentation

This folder is for project management and engineering documents that should be tracked in GitHub.

It is intentionally separate from the existing `docs/` folder, which is currently excluded by `.gitignore` and contains source/reference binaries.

## Structure

- `daily_reports/`: Day-by-day work logs
- `weekly_reports/`: Weekly summaries
- `change_history/`: User-facing or internal change history
- `design/`: Design notes and technical decisions
- `change_requests/`: Planned or requested changes

## Operating Rules

1. Use Markdown for tracked project documents whenever possible.
2. Add a daily report when meaningful work is done on a given date.
3. Update `change_history/CHANGELOG.md` when behavior, APIs, workflows, or outputs change.
4. Add or update a design document before large structural changes.
5. Record requested changes in `change_requests/` before or during implementation if traceability matters.

## Naming Rules

- Daily report: `daily_reports/YYYY-MM-DD.md`
- Weekly report: `weekly_reports/YYYY-Www.md`
- Design document: `design/<topic>.md`
- Change request: `change_requests/YYYY-MM-DD-<topic>.md`

## Recommended Workflow

When code changes are merged, update the related documents in the same commit when possible:

- work completed -> daily report
- weekly summary -> weekly report
- released change -> changelog
- architecture or flow update -> design document
- incoming requirement or scope change -> change request
