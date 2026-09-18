"""Materializer for the AWS STS rail: writes the owner's ``.aws`` bundle so the
baked ``aws`` CLI pulls short-lived connector credentials on demand.

Three files under ``{EFS_MOUNT_PATH}/{owner}/.aws/`` (in-container:
``/home/node/.openclaw/.aws/`` — the per-user EFS access point roots
``/users/{owner}`` at ``/home/node/.openclaw``, and the task env
``AWS_CONFIG_FILE`` already points at ``config`` there):

* ``config`` — ``[profile lightbulb-connector]`` whose ``credential_process`` runs
  the helper below. The profile is **named**, never ``[default]``: a default
  profile would be picked up by the in-process Bedrock SDK's default chain.
  It carries NO ``role_arn``/``credential_source``/``external_id`` — a profile
  mixing ``role_arn`` with other credential sources flips botocore into
  assume-role mode, and the ExternalId must never touch EFS.
* ``lightbulb-creds.mjs`` — zero-dependency node ESM helper the CLI executes once
  per invocation: serves the cached credential while >10 min validity remains,
  else fetches from the backend credentials endpoint with the bearer token and
  rewrites the cache. ``/usr/bin/env node`` keeps the command PATH-robust.
* ``token`` — ``{"owner_id", "token"}`` bearer credential (0600). Server-side
  only its SHA-256 hash is retained, so reading Secrets Manager never yields a
  usable bearer.

The exec-env sanitizer strips every env credential channel from agent execs;
a config-file ``credential_process`` is the delivery mechanism that survives.
The files are agent-writable by design — the security boundary is the token +
backend validation, not file integrity (tampering is self-harm only)."""

from __future__ import annotations

import json

from core.config import settings
from core.services.credentials.materializers.base import atomic_write, is_projecting, owner_efs_root

# Must match the baked aws-exec-profile plugin's AWS_CONNECTOR_PROFILE
# (apps/infra/openclaw/plugins/aws-exec-profile/dist/resolve.js): the plugin
# injects AWS_PROFILE by name only when it finds this exact section header.
AWS_CONNECTOR_PROFILE = "lightbulb-connector"
HELPER_FILENAME = "lightbulb-creds.mjs"
TOKEN_FILENAME = "token"
CACHE_FILENAME = "cache.json"

# In-container absolute path of the .aws dir (see module docstring).
_CONTAINER_AWS_DIR = "/home/node/.openclaw/.aws"

# The AgentCore/Hermes form of the same line. There the .aws dir lives inside the agent's OWN
# profile dir, whose absolute path differs per agent, so the only stable way to name it is
# $HOME -- which ``TERMINAL_HOME_MODE: profile`` points at ``<profile>/home`` for every shell
# the turn spawns. botocore runs credential_process through ``shlex.split`` + ``Popen`` with NO
# shell and NO variable expansion, so the expansion has to be asked for explicitly; ``exec``
# keeps it to one process. Everything else about the rail is unchanged: `node` is in the
# Hermes base image (/usr/local/bin/node) and the helper reads its sibling ``token``/
# ``cache.json`` from its own dirname, which resolves inside the profile either way.
_HERMES_CREDENTIAL_PROCESS = f"""sh -c 'exec /usr/bin/env node "$HOME/.aws/{HELPER_FILENAME}"'"""

# On AgentCore the projected profile is ``[default]``, NOT the named one the module docstring
# above insists on for ECS -- and the reason that rule exists does not carry here. On ECS the
# fear is "a default profile would be picked up by the in-process Bedrock SDK's default chain":
# the OpenClaw process's own $HOME IS the EFS root holding this file, so a [default] section
# would silently re-credential OpenClaw's own Bedrock calls. In the microVM the bridge process's
# $HOME is ``/`` (verified in the pinned image) and only the CHILD shell's HOME is rewritten to
# ``<profile>/home`` -- so no in-process SDK can ever read this file, and there is nothing for a
# [default] section to capture. Making it the default is what lets the shell work with NO
# ``AWS_PROFILE`` at all, which is the whole point: a variable nothing has to deliver cannot be
# dropped by a filter, cached stale, or forgotten on a new rail.
_HERMES_SECTION = "default"

_HELPER_TEMPLATE = r"""#!/usr/bin/env node
// Lightbulb AWS credential_process helper — materialized by the Lightbulb backend.
// Prints credential_process JSON: cache while fresh, else fetch from the
// backend with the sibling bearer token. The token is the security boundary.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const API_BASE = "__API_BASE__";
const DIR = dirname(fileURLToPath(import.meta.url));
const CACHE = join(DIR, "cache.json");
const REFRESH_MARGIN_MS = 10 * 60 * 1000;

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}
function msLeft(creds) {
  return creds && creds.Expiration ? new Date(creds.Expiration).getTime() - Date.now() : -1;
}
function emit(creds) {
  process.stdout.write(JSON.stringify(creds));
}

const cached = readJson(CACHE);
if (msLeft(cached) > REFRESH_MARGIN_MS) {
  emit(cached);
  process.exit(0);
}

const tok = readJson(join(DIR, "token"));
if (!tok || !tok.token) {
  console.error("lightbulb: AWS connection token missing — reconnect AWS in Lightbulb settings");
  process.exit(1);
}

let creds = null;
try {
  const resp = await fetch(`${API_BASE}/api/v1/connections/aws/credentials`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${tok.token}` },
    body: JSON.stringify({ owner_id: tok.owner_id }),
    signal: AbortSignal.timeout(10_000),
  });
  if (resp.ok) {
    // Shape-check before accepting: a truthy-but-malformed 200 body (e.g. {})
    // must fall through to the stale-cache/error path, never be emitted or
    // cached as credentials.
    const parsed = await resp.json();
    if (parsed && parsed.Version === 1 && parsed.AccessKeyId) {
      creds = parsed;
    } else {
      console.error("lightbulb: credential service returned a malformed credential payload");
    }
  } else {
    console.error(`lightbulb: credential service returned ${resp.status}`);
  }
} catch (err) {
  console.error(`lightbulb: credential service unreachable (${(err && err.name) || err})`);
}

if (!creds) {
  if (msLeft(cached) > 0) {
    emit(cached); // stale-but-valid cache beats failing the CLI call
    process.exit(0);
  }
  console.error("lightbulb: no valid AWS credentials — Lightbulb credential service unavailable");
  process.exit(1);
}

try {
  writeFileSync(CACHE, JSON.stringify(creds), { mode: 0o600 });
} catch {
  // Cache write failure is non-fatal — creds still go to stdout.
}
emit(creds);
"""


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    aws_dir = owner_efs_root(owner_id) / ".aws"
    projecting = is_projecting()
    process = (
        _HERMES_CREDENTIAL_PROCESS if projecting else f"/usr/bin/env node {_CONTAINER_AWS_DIR}/{HELPER_FILENAME}"
    )
    section = _HERMES_SECTION if projecting else f"profile {AWS_CONNECTOR_PROFILE}"
    config = f"[{section}]\nregion = {credential['region']}\ncredential_process = {process}\n"
    helper = _HELPER_TEMPLATE.replace("__API_BASE__", settings.PUBLIC_API_BASE)
    token = json.dumps({"owner_id": owner_id, "token": credential["cli_token"]})
    atomic_write(aws_dir / "config", config.encode())
    atomic_write(aws_dir / HELPER_FILENAME, helper.encode(), mode=0o700)
    atomic_write(aws_dir / TOKEN_FILENAME, token.encode())


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    aws_dir = owner_efs_root(owner_id) / ".aws"
    for name in ("config", HELPER_FILENAME, TOKEN_FILENAME, CACHE_FILENAME):
        (aws_dir / name).unlink(missing_ok=True)
