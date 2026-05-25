# qc-confluence-skills

Local skill workspace for accessing a self-hosted Confluence instance.

## Version

`0.1.1`

## Structure

```text
.
├── CHANGELOG.md
├── README.md
└── skills
    └── qc-confluence-skills
        ├── SKILL.md
        └── scripts
            └── confluence_api.py
```

## Skill location

The actual skill lives here:

- `skills/qc-confluence-skills/SKILL.md`

The bundled script lives here:

- `skills/qc-confluence-skills/scripts/confluence_api.py`

## Configuration

The canonical project version is stored in `./VERSION`.

The script reads `./.env` from the current working directory by default.

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
python3 /Users/zhangpeng/opt/self-skills/qc-confluence-skills/skills/qc-confluence-skills/scripts/confluence_api.py search-pages --title "release notes"
```
