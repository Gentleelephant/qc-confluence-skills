# Changelog

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
