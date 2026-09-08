"""Every seeded bootstrap file, for every seeded agent, fits OpenClaw's cap.

OpenClaw injects a fixed set of workspace files into EVERY turn's context and
caps each one. Over the cap the file is TRUNCATED before the agent reads a byte
of it; the only signal is a `warn()` line in the container's own log, so nothing
reaches the backend, an alarm, or the owner.

Thresholds, read from the fork at the pinned tag v2026.8.1-isol8.3
(`src/agents/embedded-agent-helpers/bootstrap.ts`):

    DEFAULT_BOOTSTRAP_MAX_CHARS       = 20_000   per file
    USER_BOOTSTRAP_MAX_CHARS          =  4_000   USER.md only (tighter)
    DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS = 60_000   all bootstrap files together

The set of files this applies to is `WORKSPACE_BOOTSTRAP_FILENAMES`
(`src/agents/workspace.ts`). Note TOOLS.md is NOT on it any more — see
`test_no_seeded_agent_ships_a_file_named_tools_md`, which is the regression this
module exists for.

Why one module spanning both seeders: #2360 added an under-cap assertion for
Friday's AGENTS.md alone. Atlas's oversized AGENTS.md sat in 16 of 35 prod
owners' workspaces for months because nothing measured the other agent.
"""

import re
from pathlib import Path

from core.services import friday_identity
from core.services.atlas_seed import _ACP_DELEGATION_SECTION, _FILES_DIR, _is_staged
from core.services.paperclip_credential_rule import PAPERCLIP_CREDENTIAL_RULE

# src/agents/embedded-agent-helpers/bootstrap.ts
BOOTSTRAP_MAX_CHARS = 20_000
USER_BOOTSTRAP_MAX_CHARS = 4_000
BOOTSTRAP_TOTAL_MAX_CHARS = 60_000

# src/agents/workspace.ts — WORKSPACE_BOOTSTRAP_FILENAMES, the files OpenClaw
# reads into context. Anything a seeder writes under one of these names is
# spending the per-turn budget whether we meant it to or not.
BOOTSTRAP_FILENAMES = frozenset({"AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "BOOTSTRAP.md", "MEMORY.md"})

# A long-but-realistic org display name: Friday's templates substitute
# {owner_display} many times over, so the template length understates a real
# container's file by hundreds of chars.
LONG_OWNER = "Acme Manufacturing Group"


def _budget_for(filename: str) -> int:
    return USER_BOOTSTRAP_MAX_CHARS if filename == "USER.md" else BOOTSTRAP_MAX_CHARS


def _friday_seeded_files() -> dict[str, str]:
    """What pre_stage_friday_identity writes, rendered for a real owner."""
    return friday_identity._render_templates(
        {
            "kind": "org",
            "owner_display": LONG_OWNER,
            "org_name": LONG_OWNER,
            "owner_email": "owner@acme-manufacturing-group.example.com",
        }
    )


def _atlas_seeded_files() -> dict[str, str]:
    """What pre_stage_atlas writes, ASSEMBLED — not the raw vendored templates.

    AGENTS.md is staged as template + ACP delegation + the Paperclip credential
    rule (see pre_stage_atlas). Measuring the template alone passes while the
    file a container actually loads is 3,615 chars larger.
    """
    files: dict[str, str] = {}
    for abs_path in sorted(_FILES_DIR.rglob("*")):
        if not abs_path.is_file():
            continue
        rel_path = abs_path.relative_to(_FILES_DIR)
        if not _is_staged(rel_path) or len(rel_path.parts) != 1:
            continue
        files[rel_path.name] = abs_path.read_text(encoding="utf-8")
    files["AGENTS.md"] += _ACP_DELEGATION_SECTION + PAPERCLIP_CREDENTIAL_RULE
    return files


SEEDED_AGENTS = {"Friday": _friday_seeded_files, "Atlas": _atlas_seeded_files}


def test_every_seeded_bootstrap_file_fits_its_per_file_budget():
    over = []
    for agent, load in SEEDED_AGENTS.items():
        for filename, content in load().items():
            if filename not in BOOTSTRAP_FILENAMES:
                continue
            budget = _budget_for(filename)
            if len(content) >= budget:
                over.append(f"{agent}/{filename}: {len(content)} chars (budget {budget})")
    assert not over, (
        "seeded bootstrap file(s) at or over OpenClaw's per-file cap — the excess is "
        "TRUNCATED before the agent reads it, and the only signal is a warn() in the "
        "container's log: " + "; ".join(over) + ". Move reference material into a "
        "non-bootstrap file beside it and point at it; do not raise the cap."
    )


def test_every_seeded_agents_bootstrap_set_fits_the_total_budget():
    for agent, load in SEEDED_AGENTS.items():
        total = sum(len(content) for name, content in load().items() if name in BOOTSTRAP_FILENAMES)
        assert total < BOOTSTRAP_TOTAL_MAX_CHARS, (
            f"{agent}'s bootstrap files total {total} chars; over "
            f"{BOOTSTRAP_TOTAL_MAX_CHARS} OpenClaw drops whole files off the end."
        )


def test_both_seeders_are_actually_measured():
    """Guards the two loaders against going vacuously empty."""
    for agent, load in SEEDED_AGENTS.items():
        names = set(load()) & BOOTSTRAP_FILENAMES
        assert "AGENTS.md" in names, f"{agent} loader returned no AGENTS.md: {sorted(load())}"


def test_no_seeded_agent_ships_a_file_named_tools_md():
    """A workspace TOOLS.md is a booby trap, not an ordinary file.

    OpenClaw's boot doctor (`doctor --fix`, run on every container boot by
    apps/infra/openclaw/local-sqlite/isol8-local-sqlite-app.sh) CONCATENATES a
    workspace TOOLS.md into that workspace's AGENTS.md and unlinks the source
    (src/commands/doctor-tools-md-migration.ts). Atlas's 14.5 KB reference file
    merged into its 18.2 KB AGENTS.md produced a 32,933-byte AGENTS.md on 16 of
    35 prod owners — 12,700 chars past the cap, silently cut every turn. The
    per-file assertions above cannot see it: on disk each file is legal, and the
    merge happens inside the container.

    Reference material goes in a file the migration does not match.
    """
    staged = [
        p.relative_to(_FILES_DIR).as_posix()
        for p in sorted(_FILES_DIR.rglob("*"))
        if p.is_file() and _is_staged(p.relative_to(_FILES_DIR))
    ]
    assert not [rel for rel in staged if Path(rel).name == "TOOLS.md"], (
        "a seeded file is named TOOLS.md; OpenClaw's boot doctor will merge it into "
        "AGENTS.md and blow the 20,000-char bootstrap cap. Name it ATLAS-TOOLS.md (or "
        "anything else) and point AGENTS.md at it."
    )
    assert "ATLAS-TOOLS.md" in staged, "the renamed Atlas reference file disappeared"


def test_atlas_agents_md_routes_to_the_reference_file():
    """The content only helps if the agent knows to go read it."""
    agents_md = _atlas_seeded_files()["AGENTS.md"]
    assert "ATLAS-TOOLS.md" in agents_md
    assert not re.search(r"(?<!-)\bTOOLS\.md\b", agents_md), (
        "AGENTS.md still points at TOOLS.md, which no longer exists in the workspace"
    )
