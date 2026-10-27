---
name: confluence-manager
description: Read, search, summarize, and export content from a self-hosted Confluence instance through its REST API. Use this skill whenever the user asks about internal documentation, wiki pages, a company knowledge base, runbooks, deployment guides, or internal doc URLs, including requests like "find the doc about X", "what does our wiki say about Y", "pull up that page", "export these pages to Markdown", or "list the child pages under Z". Read-only: it does not create or edit Confluence content.
---

# Self-hosted Confluence

Read-only access to a self-hosted Confluence (Server/Data Center) instance. The bundled script uses only the Python standard library, emits JSON on stdout, and is designed for question answering, Markdown export, and batch/tree operations.

## Setup

Create a user-level config once:

```bash
python3 scripts/confluence_api.py config init
python3 scripts/confluence_api.py config set CONFLUENCE_BASE_URL=https://confluence.example.com
# then open ~/.config/confluence-manager/.env and fill in CONFLUENCE_PAT
python3 scripts/confluence_api.py check
```

`config init` writes `~/.config/confluence-manager/.env` with mode `0600`. Edit that file directly for secrets; `config set CONFLUENCE_PAT=...` leaves the token in shell history. Use `config init --local` to create `./.env` in the current project instead.

Configuration precedence:

1. CLI arguments
2. Process environment variables
3. `--env-file <path>`
4. `./.env` in the current directory
5. `~/.config/confluence-manager/.env`

Authentication:

- **Bearer (recommended):** `CONFLUENCE_PAT`. A personal access token is not a password.
- **Basic:** `CONFLUENCE_USERNAME` + `CONFLUENCE_PASSWORD` (the account login password).
- `CONFLUENCE_AUTH_MODE=auto` (default) tries bearer first, then basic when both are configured.

## Commands

```bash
python3 scripts/confluence_api.py check
python3 scripts/confluence_api.py search -q "kubesphere 离线安装" --space SC --limit 5
python3 scripts/confluence_api.py get 12345 --section "安装步骤" --max-chars 4000
python3 scripts/confluence_api.py read 12345 67890 --max-chars 3000
python3 scripts/confluence_api.py tree --space SC --max-depth 2
python3 scripts/confluence_api.py export --ids 12345,67890 --out ./wiki-export
python3 scripts/confluence_api.py attachments 12345
python3 scripts/confluence_api.py download-attachments 12345 --out ./attachments
python3 scripts/confluence_api.py spaces --limit 25
python3 scripts/confluence_api.py config show
```

Command aliases `get-page`, `search-pages`, and `list-attachments` are still accepted.

### Search

`search` builds CQL from `-q`/`--query` (matches text or title), `--title`, `--space`, and `--label`; `--cql` passes a raw query through. Results are relevance-ordered by default. Use `--sort recent` only when the user explicitly wants the newest pages.

- `--limit N` — number of results for a single request (default 10, server cap 500).
- `--start N` — offset for manual pagination.
- `--all --max-results N` — collect every result up to `N` automatically. `--limit` then sets the per-request page size (default 100, cap 100); `--max-results` defaults to 500.
- `--explain` — include the CQL, endpoint, and timing.

Read `has_more` / `next_start` from the response. Do **not** infer pagination from `total > start + limit`; servers cap page sizes without changing `total`.

Each result includes `id`, `title`, `space`, `space_name`, `url`, `last_modified`, and a clean `excerpt`. Pick the target by title and excerpt, then fetch it by `id`.

### Get

`get` accepts a page id or a Confluence URL (`?pageId=...` or `/pages/<id>`), or `--title` with optional `--space`.

- `--format md` (default) returns Markdown converted from Confluence's rendered HTML.
- `--format text` returns flattened text for direct quoting.
- `--format storage|view|export_view` returns the raw body of that format.
- `--section "Heading"` returns only that heading's section; the error lists available headings when no match is found. Markdown/text output only.
- `--max-chars N` truncates the body and sets `truncated`.
- `--out FILE` writes the body to a file and nulls `body` in the JSON.
- `--with-attachments` includes attachment metadata.
- `--download-images DIR` saves inline images and reports absolute URLs. Markdown/text output only.

Credentials are only sent to the configured Confluence origin: images or attachments hosted elsewhere are downloaded without the PAT, and a redirect to another host is refused.

The response includes `id`, `title`, `space`, `version`, `updated`, `updated_by`, `url`, `ancestors`, `labels`, `source_format`, `body`, `truncated`, `images`, `attachment_count`, and optionally `attachments`.

### Read, tree, export

- `read <ids...>` fetches several pages concurrently; `--cql`/`-q` resolves ids from a search first. Per-page failures are returned as error entries, not raised.
- `tree --id <root>` or `tree --space <key>` walks child pages breadth-first. `truncated` means pagination or `--max-pages` cut the walk; `depth_limited` means `--max-depth` did.
- `export` writes Markdown files with YAML front matter to `<out>/<space>/<id>-<slug>.md` plus `manifest.json`, where `<slug>` is the slugified title. Targets come from `--cql`/`-q`, then `--ids`, then `--id`/`--space`. Always consider `--dry-run` first; existing files are skipped unless `--overwrite`. `--download-attachments` saves media under `<out>/attachments/<page-id>/`.

## Workflows

**Answer a question from the wiki**

1. `search -q "<keywords>" [--space KEY] --limit 5`.
2. Choose the best page by title and excerpt.
3. `get <id> --section "<heading>" --max-chars 4000`, or fetch the whole page when the structure is unknown.
4. Answer from `body`, cite the page title and `url`. Avoid dumping the full body unless the user asked for it.

**Export documentation**

1. Locate pages with `search` or `tree`.
2. `export ... --out DIR --dry-run` to confirm targets.
3. Run without `--dry-run`; report `written`, `skipped`, and `failed`.

**Batch processing**

Use `read` for a handful of pages and `tree` + `export --space` for an entire space. Keep `--max-depth` and `--max-pages` bounded.

**Fuzzy title requests**

If the user gives a title but not an id, use `get --title "<title>" --space <key>`; the response includes `match` (`exact` or `fuzzy`) and `ambiguous`/`candidates` when several pages match. Fall back to `search` when the match is fuzzy.

## Output contract

- Success: one JSON object on stdout.
- Failure: one JSON object on stderr with `error.type`, `error.message`, and often `error.status` and `error.hint`.
- JSON is compact by default; add `--pretty` for readability.
- Exit codes: `0` success, `1` unexpected, `2` configuration/usage, `3` auth or permission, `4` not found, `5` network, `6` API/server.

Read stderr before retrying. In particular:

- **2** — run `config show` or `check`; the base URL, credentials, or flags are wrong.
- **3** — the token is invalid/expired or the account lacks access. Do not retry automatically. Basic auth needs the login password, not a PAT.
- **4** — confirm the id, or search by title/excerpt.
- **5** — likely VPN or network connectivity; verify the base URL is reachable.
- **6** — server-side error; report status and message rather than guessing.

## Notes

- Prefer `CONFLUENCE_PAT` in the user config file over the `--pat` flag, which is visible in process lists and shell history.
- `check` reports auth and connectivity plus a `warnings` list (for example, a plain-HTTP base URL that would send credentials unencrypted). Surface those warnings to the user.
- Never print, log, or commit token or password values. `config show` only reports `pat_set`/`password_set`.
- Prefer this script over ad hoc `curl` so auth, pagination, and error handling stay consistent.
- The skill is read-only; it cannot create, update, or delete pages.
