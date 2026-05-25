---
name: qc-confluence-skills
description: Fetch article content from a self-hosted Confluence instance using its REST API and a personal access token. Use when the user wants to read, search, or download Confluence page content from https://cwiki.yunify.com or another compatible self-hosted Confluence deployment.
---

# Self-hosted Confluence Fetch

Use this skill when the task is to read or search Confluence content from a self-hosted instance with a personal access token.

## Configuration

Create a `.env` file in the project directory where the script will be run.

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
2. Pick the target page from the JSON results
3. Run `get-page --id ...`

The script defaults to `body.storage`, which is the safest format for downstream processing. Use `--body-format view` only when rendered HTML is specifically needed.

## Output

The script prints JSON to stdout. Treat that as the interface contract.

Important fields for `get-page`:

- `id`
- `title`
- `type`
- `status`
- `space`
- `version`
- `webui`
- `body`

For downloads, the script returns JSON containing the saved files.

## Notes

- Do not ask the user for a password when `CONFLUENCE_PAT` is present in `.env`.
- Prefer the script over ad hoc `curl` unless debugging auth or endpoint behavior.
- If auth fails under `auto`, inspect the error and retry with `CONFLUENCE_AUTH_MODE=basic` plus `CONFLUENCE_USERNAME`.
