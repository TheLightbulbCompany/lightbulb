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
schema for the chaos suite and then derives a JSON Schema from it
(``tests/chaos/_schema/gen_json_schema.mjs`` → ``openclaw_config.schema.json``,
sitting next to this module so the Docker build ships it). Both artifacts come
from the SAME vendored source, pinned to ``openclaw-version.json#upstream``;
``tests/chaos/test_schema_staleness.py`` fails if the tag drifts from the pinned
image or if the JSON artifact drifts from the zod bundle.

**Two modes, bounded both ways.** A wrong vendored schema must not brick config
delivery fleet-wide, and untrusted input must not skate:

* ``enforce=True`` — writes carrying UNTRUSTED input (marketplace seller slices
  via ``apply_deploy_aux_mutations``). Violations are rejected outright.
* everything else — audit by default: log loudly, meter, allow the write.
  ``settings.OPENCLAW_CONFIG_SCHEMA_MODE = "enforce"`` flips the rest once the
  audit metric has been quiet for a while.

**Schema unavailable degrades to audit, never to a write block.** A missing or
corrupt artifact is our bug, not the caller's; refusing every config write over
it would be a worse outage than the one this guards against.
"""

import json
import logging
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Any

from core.config import settings
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("openclaw_config.schema.json")

# Enough detail to diagnose from a log line without dumping a 300-error wall for
# a config that is wrong in one structural way.
_MAX_REPORTED_ERRORS = 5


class ConfigSchemaError(Exception):
    """The merged config failed the vendored OpenClaw schema and was rejected."""


@lru_cache(maxsize=1)
def _validator() -> Any | None:
    """Compiled validator, or None when the schema can't be loaded.

    Cached: compiling a ~280 KB JSON Schema on every config write would dominate
    the write. ``lru_cache`` also memoizes the failure, so a missing artifact
    logs once rather than once per write.
    """
    try:
        import jsonschema

        schema = json.loads(_SCHEMA_PATH.read_text())
        return jsonschema.Draft202012Validator(schema)
    except Exception as exc:
        logger.error(
            "openclaw config schema unavailable at %s (%s) — schema gate degraded to audit-only",
            _SCHEMA_PATH,
            exc,
        )
        return None


def schema_errors(config: dict) -> list[str] | None:
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
    validator = _validator()
    if validator is None:
        return None
    return [
        f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.validator}"
        for err in islice(validator.iter_errors(config), _MAX_REPORTED_ERRORS)
    ]


def gate_config(config: dict, *, owner_id: str, context: str, enforce: bool = False) -> None:
    """Apply the schema gate to a config about to be persisted.

    Raises ``ConfigSchemaError`` when the document is invalid AND the effective
    mode is enforce — i.e. ``enforce=True`` (untrusted input) or the fleet knob
    is set to ``"enforce"``. Otherwise logs + meters and returns.
    """
    errors = schema_errors(config)
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
