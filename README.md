# qc-confluence-skills

Local skill workspace for accessing a self-hosted Confluence instance.

## Version

`0.4.0`

## Structure

```text
.
├── CHANGELOG.md
├── README.md
└── skills
    └── confluence-manager
        ├── SKILL.md
        └── scripts
            └── confluence_api.py
```

## Skill location

The actual skill lives here:

- `skills/confluence-manager/SKILL.md`

The bundled script lives here:

- `skills/confluence-manager/scripts/confluence_api.py`

## Configuration

The canonical project version is stored in `./VERSION`.

Configuration priority is:

1. CLI arguments
2. Process environment variables
3. Values from `--env-file`
4. Values from `./.env`

The script reads `./.env` from the current working directory by default, and also supports standard process environment variables.

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

## Example

Run from a project directory that contains `.env`:

```bash
python3 /Users/zhangpeng/opt/self-skills/qc-confluence-skills/skills/confluence-manager/scripts/confluence_api.py search-pages --title "release notes"
```
