"""Agent requirements model + scanner.

``requirements_scan`` reads an agent's effective config + workspace tree and
returns the external dependencies it needs (tools, skills, MCP servers,
credentials, external APIs), each classified into a buyer-facing disposition.

The scanner is PURE and MUST NEVER raise to its caller: a malformed agent must
degrade to ``([], scan_incomplete=True)``, never block a publish. Secret values
never enter a ``Requirement`` — only env-var *names*.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class Disposition(str, Enum):
    INCLUDED = "included"  # ships in the box (bundled tool/skill, or keyless)
    CONNECT = "connect"  # supported in the integrations / Channels tab
    BYO = "byo"  # buyer brings their own key
    UNAVAILABLE = "unavailable"  # gated (gog) or phantom (no implementation)


@dataclasses.dataclass
class Requirement:
    key: str
    display_name: str
    kind: str  # tool|skill|mcp|cli|channel|external_api|bundled
    disposition: Disposition
    env: list[str] = dataclasses.field(default_factory=list)
    detected_via: list[str] = dataclasses.field(default_factory=list)
    bundled: bool = False
    required: bool = True
    note: str | None = None
    reason: str | None = None

    def to_manifest(self) -> dict:
        d = dataclasses.asdict(self)
        d["disposition"] = self.disposition.value
        return d


# disposition sort order for a stable, buyer-legible ledger (worst first).
# Derived from the enum so a new Disposition can't silently fall to a default.
_DISPOSITION_ORDER = (
    Disposition.UNAVAILABLE,
    Disposition.BYO,
    Disposition.CONNECT,
    Disposition.INCLUDED,
)
_ORDER = {d.value: i for i, d in enumerate(_DISPOSITION_ORDER)}


def load_effective_config(directory, fallback: dict) -> dict:
    """The agent's effective config: its per-workspace ``openclaw.json`` if
    present, else ``fallback`` (the seller's global config). Never raises — a
    missing or malformed per-workspace file falls back.
    """
    cfg_path = Path(directory) / "openclaw.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return fallback
    return fallback


def build_manifest_requirements(reqs: list[Requirement], scan_incomplete: bool) -> dict:
    """Manifest fragment disclosing what an agent needs.

    ``requirements`` is the full classified ledger (every disposition). The
    ``requires.credentials`` block feeds the hire gate
    (``catalog_service.assert_credentials_satisfied``), but the scanner
    over-reports — it surfaces providers from prose/host mentions — so every
    detected ``connect`` row is marked ``optional`` and must NOT hard-block a
    hire. Genuine hard dependencies (a provider MCP shipped in the slice) still
    gate via the slice-MCP path. Channel rows gate through the Channels tab;
    ``byo`` / ``unavailable`` rows surface in ``requirements`` for the buyer
    ledger but never block.
    """
    return {
        "requirements": [r.to_manifest() for r in reqs],
        "scan_incomplete": scan_incomplete,
        "requires": {
            "credentials": [
                {"provider": r.key, "optional": True}
                for r in reqs
                if r.disposition == Disposition.CONNECT and r.kind != "channel"
            ]
        },
    }


def requirements_scan(
    effective_config: dict | None,
    agent_dir,
    *,
    bundled_mcps: set[str] | None = None,
    bundled_skills: set[str] | None = None,
) -> tuple[list[Requirement], bool]:
    """Scan an agent → ``(requirements, scan_incomplete)``. Never raises.

    Args:
        effective_config: the agent's per-workspace openclaw.json if present,
            else the global config. Drives the declared-read layer.
        agent_dir: ``workspaces/<agent_id>`` — pipelines/, scripts/, *.md.
        bundled_mcps / bundled_skills: what the seller chose to bundle (Bundle
            step); marks the matching requirement rows ``bundled=True``.
    """
    from core.services.marketplace.requirements_classify import classify_hits
    from core.services.marketplace.requirements_detect import detect_in_tree

    try:
        cfg = effective_config or {}
        config_skills = (cfg.get("skills") or {}).get("entries") or {}
        config_mcps = (cfg.get("mcp") or {}).get("servers") or {}
        hits = detect_in_tree(Path(agent_dir))
        reqs = classify_hits(
            hits,
            bundled_mcps=set(bundled_mcps or ()),
            bundled_skills=set(bundled_skills or ()),
            config_skills=config_skills,
            config_mcps=config_mcps,
        )
        reqs.sort(key=lambda r: (_ORDER.get(r.disposition.value, 9), r.key))
        return reqs, False
    except Exception:  # the scanner must never break publish
        logger.exception("requirements_scan failed for %s", agent_dir)
        return [], True
