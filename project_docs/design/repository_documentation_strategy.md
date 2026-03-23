# Design Document - Repository Documentation Strategy

## Background

- The repository was published to GitHub without tracking operational project documents

## Problem

- The existing `docs/` area is excluded from Git and includes large binary artifacts
- Daily work history, weekly summaries, and change traces are not organized for GitHub collaboration

## Goals

- Track lightweight project documents in GitHub
- Keep source/reference binaries separated from collaboration documents
- Standardize where project history and design notes are stored

## Non-Goals

- Move all legacy binary documents into Git
- Replace existing source document storage workflows

## Proposed Design

- Keep the existing `docs/` folder excluded
- Add a new `project_docs/` folder for Markdown-based tracked documents
- Organize by document purpose: daily reports, weekly reports, change history, design, and change requests
- Link the new documentation area from the root `README.md`

## Risks

- Documents may become stale if update rules are not followed consistently

## Validation

- Confirm new files are visible in Git status
- Confirm the repository README points to the tracked documentation area

## Open Questions

- Whether release notes should remain inside `project_docs/change_history/` or also be exposed from the repository root
