# qc-confluence-skills

Claude Code skill for reading, searching, and exporting content from a self-hosted Confluence instance through its REST API.

## Version

`1.0.1`

## Structure

```text
.
├── AGENTS.md
├── CHANGELOG.md
├── README.md
├── VERSION
└── skills
    └── confluence-manager
        ├── SKILL.md
        ├── assets
        │   └── env.template
        ├── evals
        │   ├── evals.json
        │   └── test_script.py
        └── scripts
            └── confluence_api.py
```

- Skill definition: `skills/confluence-manager/SKILL.md`
- Core script: `skills/confluence-manager/scripts/confluence_api.py`
- Unit tests: `skills/confluence-manager/evals/test_script.py`

## Requirements

Python 3.8+ with the standard library only. No third-party packages are required.

## Configuration

Create a user-level config file once:

```bash
python3 skills/confluence-manager/scripts/confluence_api.py config init
python3 skills/confluence-manager/scripts/confluence_api.py config set CONFLUENCE_BASE_URL=https://confluence.example.com
# then open ~/.config/confluence-manager/.env and fill in CONFLUENCE_PAT
python3 skills/confluence-manager/scripts/confluence_api.py check
```

`config init` writes `~/.config/confluence-manager/.env` with mode `0600`. Edit that file directly for secrets: `config set CONFLUENCE_PAT=...` would leave the token in shell history. `config init --local` writes `./.env` instead.

Configuration precedence:

1. CLI arguments
2. Process environment variables
3. `--env-file <path>`
4. `./.env` in the current directory
5. `~/.config/confluence-manager/.env`

Required value:

```dotenv
CONFLUENCE_BASE_URL=https://confluence.example.com
```

Authentication (one of):

```dotenv
# Recommended: personal access token, bearer auth
CONFLUENCE_PAT=your-token

# Alternative: basic auth with the login password (a PAT is not a password)
CONFLUENCE_USERNAME=your-username
CONFLUENCE_PASSWORD=your-password
```

Optional values:

```dotenv
# auto (default) | bearer | basic
CONFLUENCE_AUTH_MODE=auto
CONFLUENCE_TIMEOUT=30
CONFLUENCE_RETRIES=2
```

Keep comments on their own line in `.env` files: dotenv parsers treat everything
after the value on the same line as part of the value.

A template is available at `skills/confluence-manager/assets/env.template`.

## Usage

```bash
SCRIPT=skills/confluence-manager/scripts/confluence_api.py

python3 "$SCRIPT" check
python3 "$SCRIPT" search -q "kubesphere 离线安装" --space SC --limit 5
python3 "$SCRIPT" get 12345 --section "安装步骤" --max-chars 4000
python3 "$SCRIPT" read 12345 67890
python3 "$SCRIPT" tree --space SC --max-depth 2
python3 "$SCRIPT" export --ids 12345,67890 --out ./wiki-export --dry-run
python3 "$SCRIPT" download-attachments 12345 --out ./attachments
```

The script prints one JSON object to stdout. Errors are printed to stderr and use stable exit codes: `2` configuration, `3` auth/permission, `4` not found, `5` network, `6` API/server.

## Tests

```bash
python3 -m unittest discover -s skills/confluence-manager/evals -p 'test_*.py' -v
```

The suite uses only the standard library and does not contact a live Confluence instance.

## Versioning

`VERSION` is the single source of truth. Every user-visible or behavior-changing modification updates `VERSION`, `README.md`, and `CHANGELOG.md`.
