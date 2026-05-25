# AGENTS.md

## Scope

These instructions apply to the entire project under this directory.

## Versioning Rules

- The project must maintain an explicit version number.
- The current version must be updated for every user-visible or behavior-changing modification.
- `VERSION` is the source of truth for the current project version.
- Version changes must be reflected in:
  - `VERSION`
  - `README.md`
  - `CHANGELOG.md`
- The version format should follow `MAJOR.MINOR.PATCH`.

## Version Bump Guidance

- Bump `PATCH` for small fixes, documentation adjustments tied to behavior, or narrow script changes.
- Bump `MINOR` for new capabilities, new commands, new configuration behavior, or expanded supported workflows.
- Bump `MAJOR` for breaking interface changes, removed commands, or incompatible configuration changes.

## Change Logging Rules

- Every version update must add or update a matching entry in `CHANGELOG.md`.
- The changelog entry should briefly describe what changed and why it matters.
- When editing the project, update `VERSION` first, then sync the same value into `README.md` and `CHANGELOG.md`.

## Current Version

- Current version: `0.4.0`
