# Changelog

## 1.0.1 - 2026-10-27

- Security: downloads no longer leak the PAT. Credentials are sent only to the configured Confluence origin, and redirects to another host are refused instead of replayed with the `Authorization` header.
- Security: exported and downloaded file paths sanitise page ids, so an id containing `../` cannot escape the output directory.
- `--section` and `--download-images` now fail fast with a clear hint when combined with a raw `--format` (`storage`/`view`/`export_view`), instead of silently doing nothing.
- `has_more` for single-response listings (`attachments`, `spaces`) now uses the server-reported `size`, so an exhausted result set no longer looks paginated.
- `search --all` preserves the `explain` payload and passes the requested page size through bounded pagination.
- `--cql`/`-q` are documented and accepted on `read`; `search` accepts a positional query as well.
- Markdown conversion fixes: `colspan` cells pad their row, `<br>` becomes a hard line break, code blocks inside table cells stay fenced, self-closing `<ac:image/>` tags no longer drop surrounding text, and storage-format macros keep their body content.
- `--section` matches exact headings first, ignores `#` lines inside code fences, and lists candidate headings on a miss.
- Front matter stripping only removes a real YAML block; a leading `---` horizontal rule no longer swallows content.
- Root-relative and relative URLs keep the deployment's context path instead of collapsing to the origin.
- `check` reports a `warnings` list, including a plain-HTTP base URL that would send credentials unencrypted.
- Docs: the setup examples no longer paste `CONFLUENCE_PAT` into shell history, and `.env` examples keep comments on their own line.

## 1.0.0 - 2026-10-27

- Reworked the CLI around the real workflows: `check`, `search`, `get`, `read`, `tree`, `export`, `attachments`, `download-attachments`, `spaces`, and `config`. Legacy command names remain as aliases.
- `search` now builds CQL from natural-language queries, space, and label filters; returns clean excerpts, reliable `has_more`/`next_start` pagination, and relevance ordering by default (`--sort recent` opts into recency).
- `get` returns Markdown by default, sourced from `body.export_view`, with automatic fallback to `view`/`storage`; adds `--section`, `--max-chars`, `--format text`, `--out`, `--with-attachments`, and `--download-images`.
- Added batch reads (`read`), breadth-first tree walks (`tree`), and Markdown export with YAML front matter, dry runs, skip/overwrite handling, and a manifest (`export`).
- Added user-level configuration at `~/.config/confluence-manager/.env`, `config init|set|show|path`, and `check` for credential/connectivity diagnostics. Configuration is no longer tied to the current working directory.
- Authentication is now explicit: bearer PAT is preferred, basic auth requires the login password, and `auto` mode falls back only when both credential sets exist. `config show` never prints secret values.
- Errors are JSON on stderr with stable exit codes (`2` config, `3` auth/permission, `4` not found, `5` network, `6` API/server) and actionable hints.
- Hardened downloads with streamed writes, retries, atomic replacement, and filename sanitisation that blocks path traversal.
- Added a stdlib `unittest` suite covering configuration precedence, CQL construction, pagination, page fetching, Markdown conversion, tree walking, export planning, attachment naming, auth headers, and error mapping.
- Reworked `SKILL.md`, `README.md`, `AGENTS.md`, the env template, and evaluation prompts to match the new interface.

## 0.4.0 - 2026-05-25

- `get-page` now automatically resolves inline `<ac:image>` tags to Markdown image links with real attachment download URLs
- `get-page` response now includes `attachments` and `images` fields for complete visibility into page media
- Added `_resolve_body_images` static method with regex-based parsing for Confluence `body.storage` XML
- Added unit tests for image resolution (attachment, external URL, missing, multiline, no-op)

## 0.3.0 - 2026-05-25

- Optimized skill description to broaden trigger coverage for wiki/docs/knowledge base queries
- Added error handling guidance for 401, 403, 404, and network errors
- Added `--start` pagination parameter to `search-pages` command with `total` field in response
- Added `--compact` flag for compact JSON output to conserve tokens
- Added `.env` template file at `assets/env.template`
- Added unit tests and evaluation prompts

## 0.2.2 - 2026-05-25

- Renamed the skill from `confluence-reader` to `confluence-manager`
- Updated the skill path and documentation to match the broader scope name

## 0.2.1 - 2026-05-25

- Renamed the skill directory under `skills/` from `qc-confluence-skills` to `confluence-reader`
- Updated repository documentation to match the new skill path

## 0.2.0 - 2026-05-25

- Added support for reading Confluence configuration from process environment variables
- Defined configuration precedence across CLI arguments, environment variables, and `.env` files

## 0.1.1 - 2026-05-25

- Added `VERSION` as the canonical source of truth for project versioning
- Updated project rules and docs to require version synchronization across tracked files

## 0.1.0 - 2026-05-25

- Initial self-hosted Confluence skill created
- Added REST API helper script for page fetch, search, and attachments
- Switched configuration loading to `.env`
- Reorganized repository so skill content lives under `skills/`
