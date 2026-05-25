---
name: confluence-manager
description: Read, search, and download content from a self-hosted Confluence instance via its REST API. Use this skill whenever the user wants to access, query, or retrieve documentation, wiki pages, or knowledge base content — especially when they mention Confluence, internal docs, company wiki, or reference URLs like cwiki.yunify.com. Also use it when the user asks to "check the wiki", "find the doc about X", "pull up the internal page", or similar queries that imply searching an internal documentation system. Future versions will also support creating and updating Confluence content.
---

# Self-hosted Confluence Fetch

Use this skill when the task is to read or search Confluence content from a self-hosted instance with a personal access token.

## Configuration

Create a `.env` file in the project directory where the script will be run, or provide the same values through process environment variables.

Required values:

```dotenv
CONFLUENCE_BASE_URL=https://cwiki.yunify.com
CONFLUENCE_PAT=your-token
```

Optional values:

```dotenv
CONFLUENCE_AUTH_MODE=auto
CONFLUENCE_USERNAME=your-name
```

Configuration priority is:

1. CLI arguments
2. Process environment variables
3. Values from `--env-file`
4. Values from `./.env`

By default the script reads `./.env` from the current working directory. You can point to a different file with `--env-file`.

## Primary tool

Run the bundled script:

```bash
python3 scripts/confluence_api.py ...
```

## Common commands

Get a page by id with storage body:

```bash
python3 scripts/confluence_api.py get-page --id 12345
```

Search by title:

```bash
python3 scripts/confluence_api.py search-pages --title "release notes"
```

Search with CQL:

```bash
python3 scripts/confluence_api.py search-pages --cql 'type=page AND title~"release"'
```

Paginate through results (the response includes `total` and `start` fields):

```bash
python3 scripts/confluence_api.py search-pages --title "release notes" --start 0 --limit 10
python3 scripts/confluence_api.py search-pages --title "release notes" --start 10 --limit 10
```

Download attachments metadata:

```bash
python3 scripts/confluence_api.py list-attachments --id 12345
```

Download attachments to a directory:

```bash
python3 scripts/confluence_api.py download-attachments --id 12345 --out ./tmp/attachments
```

## Workflow

Prefer `get-page --id` when the page id is known. It is stable and avoids ambiguous title matches.

When the id is unknown:

1. Run `search-pages` with `--title` or `--cql`
2. Check `total` in the response — if `total > limit + start`, more pages are available
3. Pick the target page from the JSON results
4. Run `get-page --id ...`

To fetch all results when `total` exceeds `limit`, loop `search-pages` with `--start` incremented by `limit` each call until all results are retrieved.

The script defaults to `body.storage`, which is the safest format for downstream processing. Use `--body-format view` only when rendered HTML is specifically needed.

## Output

The script prints JSON to stdout. Treat that as the interface contract.

For large result sets, use `--compact` to produce single-line JSON and conserve tokens:

```bash
python3 scripts/confluence_api.py search-pages --title "docs" --limit 50 --compact
```

Important fields for `get-page`:

- `id`
- `title`
- `type`
- `status`
- `space`
- `version`
- `webui`
- `body` — inline `<ac:image>` tags are automatically replaced with Markdown image links `![alt](url)` using real attachment download URLs
- `attachments` — full list of page attachments with download URLs
- `images` — metadata of image references found in the body (`filename`, `url`, `alt`, `type`)
- `raw` — the untouched Confluence API response (includes original `body.storage`)

For `search-pages`, the response includes `total`, `start`, `limit`, and `results`. Use `total` and `start` to determine whether more pages exist.

For downloads, the script returns JSON containing the saved files.

## Error handling

When the script returns an error, match the HTTP status and suggest a recovery action:

- **401 Unauthorized** — The PAT is missing, expired, or invalid. Ask the user to check `CONFLUENCE_PAT` in `.env`. Do not retry automatically. If `CONFLUENCE_AUTH_MODE=auto` failed with 401, suggest switching to `CONFLUENCE_AUTH_MODE=basic` and providing `CONFLUENCE_USERNAME` as a fallback.
- **403 Forbidden** — The PAT is valid but the account lacks permission for the requested resource. Tell the user which resource was denied and suggest checking permissions in Confluence.
- **404 Not Found** — The page id or title does not match anything. Confirm the id with the user, or broaden the search if using `--title`.
- **Network errors** (connection refused, timeout) — Report the Confluence base URL and suggest the user verify VPN or network connectivity. Do not retry more than once.

In all error cases, present the error clearly to the user rather than silently swallowing it. The script writes errors to stderr — always check stderr output.

## Notes

- Do not ask the user for a password when `CONFLUENCE_PAT` is present in `.env`.
- Prefer the script over ad hoc `curl` unless debugging auth or endpoint behavior.
