"""Schema gate for openclaw.json — validate the MERGED document before it lands on EFS.

Why this exists (issue #941): every config writer funnels through
``config_patcher._locked_rmw``, whose only pre-write check was
``json.dumps(current)``. A schema-invalid config therefore persisted silently.
The running container stays healthy on its already-loaded copy, so nothing looks
wrong — until the next restart of any kind (image roll, reprovision, spot
replacement, OOM), when OpenClaw's strict zod load rejects the file and the
gateway crash-loops. Found live on dev during an FIS container-kill: pollution
that had sat on disk for days bricked the task the moment it was replaced, and
recovery needed manual EFS surgery.

**How the schema gets here.** The backend image is Python-only
(``apps/backend/Dockerfile`` is ``python:3.12-slim``), so it cannot run
OpenClaw's zod schema. ``scripts/vendor_openclaw_schema.sh`` bundles that zod
schema for the chaos suite and then derives the JSON Schema shipped beside this
module. The fleet default stays at ``openclaw_config.schema.json``; additive
candidate schemas live under ``openclaw_config_schemas/`` and become selectable
only when their exact identifier/path is added to ``_SCHEMA_PATHS``. The drift
tests regenerate every registered JSON artifact from its own bundle and verify
candidate provenance independently of the fleet image pin.

**Two modes, bounded both ways.** A wrong vendored schema must not brick config
delivery fleet-wide, and untrusted input must not skate:

* ``enforce=True`` — writes carrying UNTRUSTED input (marketplace seller slices
  via ``apply_deploy_aux_mutations``). Violations are rejected outright.
* everything else — audit by default: log loudly, meter, allow the write.
  ``settings.OPENCLAW_CONFIG_SCHEMA_MODE = "enforce"`` flips the rest once the
  audit metric has been quiet for a while.

**Schema unavailable has two intentional behaviors.** Callers that omit a
schema identifier retain the fleet-safe audit degradation: a missing or corrupt
default artifact does not block writes. Callers that explicitly select a
registered artifact fail closed when it is unknown or unavailable; explicit
selection never falls back to the fleet default.
"""

import json
import logging
from functools import lru_cache
import re
from itertools import islice
from pathlib import Path
from typing import Any

from core.config import settings
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("openclaw_config.schema.json")
_SCHEMA_PATHS: dict[str, Path] = {
    "2026.8.1-isol8.3": _SCHEMA_PATH,
    "2026.9.3-isol8.1": _SCHEMA_PATH.parent
    / "openclaw_config_schemas"
    / "2026.9.3-isol8.1.json",
}

# Enough detail to diagnose from a log line without dumping a 300-error wall for
# a config that is wrong in one structural way.
_MAX_REPORTED_ERRORS = 5
# Prune passes for `prune_schema_rejected`. Each pass removes >=1 key and a pass
# that removes nothing exits, so this is a runaway backstop, not a tuning knob.
_MAX_PRUNE_PASSES = 10


class ConfigSchemaError(Exception):
    """The selected schema was unavailable or rejected the merged config."""


@lru_cache(maxsize=None)
def _validator(schema_id: str | None = None) -> Any | None:
    """Compiled selected validator, or None when the default can't be loaded.

    Cached: compiling a ~280 KB JSON Schema on every config write would dominate
    the write. Explicit selection resolves only registered identifiers and fails
    closed; omitted selection preserves the existing audit degradation.
    """
    if schema_id is None:
        schema_path = _SCHEMA_PATH
    else:
        schema_path = _SCHEMA_PATHS.get(schema_id)
        if schema_path is None:
            raise ConfigSchemaError("required OpenClaw config schema is unavailable") from None

    try:
        import jsonschema

        schema = json.loads(schema_path.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        return jsonschema.Draft202012Validator(schema)
    except Exception as exc:
        if schema_id is None:
            logger.error(
                "openclaw config schema unavailable at %s (%s) — schema gate degraded to audit-only",
                schema_path,
                type(exc).__name__,
            )
            return None
        logger.error(
            "required openclaw config schema unavailable at %s (%s)",
            schema_path,
            type(exc).__name__,
        )
        raise ConfigSchemaError("required OpenClaw config schema is unavailable") from None


# Mirrors OpenClaw's ``migratePersistedImplicitMainRoster`` (src/config/legacy.roster.ts),
# which runs at validation-core.ts:423 BEFORE ``OpenClawSchema.safeParse``. Without
# it this gate is stricter than OpenClaw itself: 2026.8.1's ``AgentsSchema`` is a
# strict object accepting only {ownership, defaults, entries}, so a legacy
# ``agents.list`` document fails ``agents: additionalProperties`` here even though
# OpenClaw loads it fine after converting the list. Every container still carries
# the list shape on EFS until boot ``doctor --fix`` rewrites it, so gating the RAW
# document would block us from patching precisely those configs.
#
# Conservative on purpose: when any id is unusable we leave the document alone and
# let it be validated as-is, which is exactly what the upstream migration does (it
# returns the config unchanged rather than guessing).
_AGENT_ID_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,63}$")


def _normalized_for_validation(config: dict) -> dict:
    agents = config.get("agents")
    if not isinstance(agents, dict) or "entries" in agents:
        return config
    roster = agents.get("list")
    if not isinstance(roster, list) or not roster:
        return config
    entries: dict[str, dict] = {}
    for item in roster:
        if not isinstance(item, dict):
            return config
        agent_id = item.get("id")
        # normalizeAgentId is trim + lowercase + charset sanitise; the upstream
        # migration bails unless the id is ALREADY its own normal form.
        if (
            not isinstance(agent_id, str)
            or agent_id != agent_id.strip()
            or agent_id != agent_id.lower()
            or not _AGENT_ID_RE.match(agent_id)
            or agent_id in entries
        ):
            return config
        entries[agent_id] = {k: v for k, v in item.items() if k != "id"}
    migrated_agents = {k: v for k, v in agents.items() if k != "list"}
    migrated_agents["entries"] = entries
    return {**config, "agents": migrated_agents}


def schema_errors(config: dict, *, schema_id: str | None = None) -> list[str] | None:
    """Validate ``config``. Returns None when the schema is unavailable,
    otherwise a (possibly empty) list of violations as ``path: rule``.

    NEVER include ``err.message``. jsonschema embeds ``repr(instance)`` in it —
    the offending VALUE, and for ``anyOf``/``type`` failures on a subtree that is
    the whole subtree. openclaw.json carries real secrets (``gateway.auth.token``
    is written literally; MCP/plugin entries carry ``env`` / ``apiKey``), and
    because the gate validates the whole merged document, a violation anywhere
    could drag an unrelated secret into a log line — and, via
    ``routers/config.py``, into an HTTP response body. The JSON path plus the
    schema rule that failed localizes the problem exactly without ever echoing
    owner data back; ``err.validator`` is schema-side, so it is ours, not theirs.
    """
    validator = _validator(schema_id)
    if validator is None:
        return None
    config = _normalized_for_validation(config)
    return [
        f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.validator}"
        for err in islice(validator.iter_errors(config), _MAX_REPORTED_ERRORS)
    ]


def prune_schema_rejected(base: dict, patch: dict, *, schema_id: str | None = None) -> dict:
    """Drop from ``patch`` every key the deployed schema rejects once ``patch``
    is deep-merged onto ``base``. Returns a new patch; ``patch`` is untouched.

    Exists for the config REGEN on an image roll. The regen renders a fresh,
    version-correct config, then restores the runtime-appended state the
    generator does not know about (``_runtime_only_delta``). That restore
    preserves any key the regen did not emit — which is right for genuinely
    runtime-only state (``identity``, ``bindings``) and WRONG across an
    upstream bump, where "the regen no longer emits it" also describes every
    key the new schema DELETED. Going 7.2 -> 8.1 the restore put back root
    ``audit``/``web`` and, through the co-owned recursion,
    ``skills.workshop.autonomous.enabled`` — resurrecting exactly the keys the
    regen existed to remove, and crash-looping the container the roll was
    meant to fix.

    Schema-driven rather than a removal list: the 7.2 -> 8.1 delta alone is
    170 removed subtree roots, 169 of them fatal, plus an open-map->strict
    flip (``env.*``) and a type narrowing (``diagnostics.otel.captureContent``)
    that no key-name blacklist can express. The schema already encodes all of
    it, and is re-vendored on every bump.

    Never logs or returns instance data — same reasoning as ``schema_errors``:
    openclaw.json carries live secrets.
    """
    validator = _validator(schema_id)
    if validator is None or not patch:
        return patch

    pruned = json.loads(json.dumps(patch))  # deep copy; config is JSON by construction

    # Bounded: each pass removes >=1 key, and a pass that removes nothing exits.
    for _ in range(_MAX_PRUNE_PASSES):
        merged = _deep_merge(base, pruned)
        removed = False
        for err in validator.iter_errors(_normalized_for_validation(merged)):
            if err.validator != "additionalProperties" or not isinstance(err.instance, dict):
                continue
            allowed = set((err.schema or {}).get("properties") or {})
            target = pruned
            for step in err.absolute_path:
                if not isinstance(target, dict) or step not in target:
                    target = None
                    break
                target = target[step]
            if not isinstance(target, dict):
                continue
            for key in [k for k in target if k not in allowed]:
                del target[key]
                removed = True
        if not removed:
            break

    return _strip_empty(pruned)


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        current = out.get(key)
        out[key] = _deep_merge(current, value) if isinstance(value, dict) and isinstance(current, dict) else value
    return out


def _strip_empty(node: dict) -> dict:
    """Drop dict branches left empty by pruning, so the restore patch does not
    write bare ``{}`` sections the generator deliberately omitted."""
    out = {}
    for key, value in node.items():
        if isinstance(value, dict):
            nested = _strip_empty(value)
            if nested:
                out[key] = nested
        else:
            out[key] = value
    return out


def gate_config(
    config: dict,
    *,
    owner_id: str,
    context: str,
    enforce: bool = False,
    schema_id: str | None = None,
) -> None:
    """Apply the schema gate to a config about to be persisted.

    Raises ``ConfigSchemaError`` when the document is invalid AND the effective
    mode is enforce — i.e. ``enforce=True`` (untrusted input) or the fleet knob
    is set to ``"enforce"``. Otherwise logs + meters and returns.
    """
    errors = schema_errors(config, schema_id=schema_id)
    if errors is None:
        put_metric("config.schema.unavailable")
        return
    if not errors:
        return

    enforcing = enforce or settings.OPENCLAW_CONFIG_SCHEMA_MODE.strip().lower() == "enforce"
    detail = "; ".join(errors)
    if enforcing:
        put_metric("config.schema.rejected", dimensions={"untrusted": str(enforce).lower()})
        logger.error(
            "REJECTED schema-invalid openclaw.json write for owner %s (%s): %s",
            owner_id,
            context,
            detail,
        )
        raise ConfigSchemaError(f"config would not load in OpenClaw and was rejected ({context}): {detail}")

    put_metric("config.schema.violation")
    logger.error(
        "SCHEMA-INVALID openclaw.json written for owner %s (%s) — this config will "
        "crash-loop the container on its next restart: %s",
        owner_id,
        context,
        detail,
    )
