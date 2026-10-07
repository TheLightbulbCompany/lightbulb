"""Materializer for the AWS STS rail: writes the owner's ``.aws`` bundle so the
baked ``aws`` CLI pulls short-lived connector credentials on demand.

Three files under the profile's ``home/.aws/`` (``$HOME/.aws/`` for the turn's shell,
which is what ``AWS_CONFIG_FILE`` points at):

* ``config`` — a ``[default]`` section whose ``credential_process`` runs the helper
  below (see ``_HERMES_SECTION``). It carries NO
  ``role_arn``/``credential_source``/``external_id`` — a profile mixing ``role_arn``
  with other credential sources flips botocore into assume-role mode, and the
  ExternalId must never leave the backend.
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
from core.services.credentials.materializers.base import atomic_write, materializer_root

HELPER_FILENAME = "lightbulb-creds.mjs"
TOKEN_FILENAME = "token"
CACHE_FILENAME = "cache.json"

# botocore runs credential_process through
# ``shlex.split`` + ``Popen`` with NO shell and NO variable expansion, so an expansion has to be
# asked for explicitly; ``exec`` keeps it to one process. `node` is in the Hermes base image
# (/usr/local/bin/node), so the rail is otherwise unchanged.
#
# ``$HOME`` here is NOT this agent's profile home in general. It FREEZES at the first profile to
# run a shell in the microVM's process -- ``HOME`` is in ``agent.secret_scope._GLOBAL_ENV_EXACT``,
# so it resolves from ``os.environ`` rather than the per-profile scope (measured in the pinned
# image; see ``bridge/sync.py::cli_credential_env``). The FILE the CLI opens is pinned correctly
# per profile by ``AWS_CONFIG_FILE``, which rides ``terminal.env_passthrough`` and does
# re-resolve; but the helper, its ``token`` and its ``cache.json`` are reached from this line, so
# they are read off the FROZEN home -- i.e. possibly a sibling profile's copy.
#
# That is bounded, and only because the projection writes byte-identical copies to every roster
# profile (see ``credentials/hermes_cli.py``'s invariant): the sibling's helper, bearer token and
# cache are the SAME owner-level credential, and the token authorizes by owner, not by agent. If
# that invariant ever breaks, this line breaks with it and the helper must be reached by an
# absolute per-profile path instead.
_HERMES_CREDENTIAL_PROCESS = f"""sh -c 'exec /usr/bin/env node "$HOME/.aws/{HELPER_FILENAME}"'"""

# The projected profile is ``[default]``. In the microVM the bridge process's
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
    aws_dir = materializer_root() / ".aws"
    config = (
        f"[{_HERMES_SECTION}]\nregion = {credential['region']}\ncredential_process = {_HERMES_CREDENTIAL_PROCESS}\n"
    )
    helper = _HELPER_TEMPLATE.replace("__API_BASE__", settings.PUBLIC_API_BASE)
    token = json.dumps({"owner_id": owner_id, "token": credential["cli_token"]})
    atomic_write(aws_dir / "config", config.encode())
    atomic_write(aws_dir / HELPER_FILENAME, helper.encode(), mode=0o700)
    atomic_write(aws_dir / TOKEN_FILENAME, token.encode())


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    aws_dir = materializer_root() / ".aws"
    for name in ("config", HELPER_FILENAME, TOKEN_FILENAME, CACHE_FILENAME):
        (aws_dir / name).unlink(missing_ok=True)
