# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Claude Code skill (`confluence-manager`) for reading, searching, and downloading content from a self-hosted Confluence instance via its REST API.

- Skill definition: `skills/confluence-manager/SKILL.md`
- Core script: `skills/confluence-manager/scripts/confluence_api.py`
- Unit tests: `skills/confluence-manager/evals/test_script.py`
- Eval config: `skills/confluence-manager/evals/evals.json`

## Architecture

### `confluence_api.py`

Single-file Python script with a `ConfluenceClient` class. Key methods:

- `get_page(page_id)` — fetches page content (`body.storage` by default). Automatically calls `list_attachments` on the same page and resolves inline `<ac:image>` tags into Markdown image links with real download URLs. Adds `attachments` and `images` fields to the response.
- `search_pages(title/cql)` — search without attachment resolution (use resulting `id` with `get_page`).
- `list_attachments(page_id)` / `download_attachments(page_id, out_dir)` — attachment metadata and bulk download.

Auth priority: CLI args → env vars → `--env-file` → `./.env`. Supports bearer and basic auth with fallback.

### `confluence_api.py` → `SKILL.md`

The skill definition (`SKILL.md`) is the user-facing contract. The Python script is the bundled tool Claude Code invokes. When adding new CLI flags, response fields, or commands, update both files.

## Development Commands

Run tests (from repo root):

```bash
python3 -m pytest skills/confluence-manager/evals/test_script.py -v
```

Run a single test:

```bash
python3 -m pytest skills/confluence-manager/evals/test_script.py::TestResolveBodyImages -v
```

Run the script directly (requires a `.env` or env vars):

```bash
python3 skills/confluence-manager/scripts/confluence_api.py get-page --id 12345
```

## Versioning

`VERSION` is the single source of truth. Every user-visible or behavior-changing modification must:

1. Update `VERSION`
2. Sync the version into `README.md`
3. Add/update an entry in `CHANGELOG.md`

Version format: `MAJOR.MINOR.PATCH`

- **PATCH** — small fixes, narrow script changes, doc adjustments tied to behavior
- **MINOR** — new capabilities, new commands, new config behavior
- **MAJOR** — breaking interface changes, removed commands, incompatible config changes

Workflow: edit `VERSION` first, then sync `README.md`, then update `CHANGELOG.md`.

## Design Constraints

- The script outputs JSON to stdout. That is the interface contract.
- All API responses must remain JSON serialisable.
- Body parsing is regex-based and targets Confluence's `body.storage` XML format.
- Skill description is curated for trigger coverage — do not over-specify Confluence-specific hostnames in `SKILL.md`.
