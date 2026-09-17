# Vendored: Hermes's OpenClaw->Hermes migration tool

`openclaw_to_hermes.py` in this directory is vendored **verbatim** from:

- Repository: [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent)
- Tag: `v2026.9.14` (package `0.21.3`)
- Commit SHA of that tag: `345cd2b057a452236de401d3534b8502a7465e8d`
- Path: `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py`

Fetched with:

```bash
gh api "repos/NousResearch/hermes-agent/contents/optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py?ref=v2026.9.14" \
  --jq .content | base64 -d > openclaw_to_hermes.py
```

## Why it's here

Ruling M6-R47 (the migration job, `apps/backend/scripts/migrate_workspace_to_hermes.py`):
rather than reimplementing Hermes's own OpenClaw-memory-entry extraction/merge semantics as a
backend-owned port, we vendor the translator itself and drive its `Migrator` class directly
(see `core/services/hermes_memory_migration.py`) — the actual algorithm, not a copy of it, is
what runs. Only the memory-related migration options (`memory`, `user-profile`,
`daily-memory`) are ever selected; every other option this tool supports (skills, channel
settings, config.yaml sections, secrets, etc.) is unused here and is Hermes CLI functionality
this platform does not drive.

## Rules

- **Never hand-edited.** This file is a byte-for-byte copy of the upstream source at the pinned
  tag. If Hermes ships a fix or behaviour change to this tool, bump the pin and re-fetch the
  whole file with the command above -- do not patch it in place.
- Only `core/services/hermes_memory_migration.py` may import from this package. It is
  vendored, not backend product code: `apps/backend/ruff.toml`'s `exclude` covers this
  directory, so it is never linted or reformatted by this repo's tooling (its own upstream CI
  is the source of truth for its style).

## Licence

`NousResearch/hermes-agent` is MIT-licensed at this tag. Full text, fetched the same way from
the repo's `LICENSE` file at `v2026.9.14`:

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
