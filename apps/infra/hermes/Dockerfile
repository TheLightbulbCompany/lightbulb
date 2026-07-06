# Extended Hermes image — the runtime image Isol8 launches for per-user Hermes
# containers, built on top of the Isol8 Hermes fork's published base image.
#
# This is the Hermes analog of apps/infra/openclaw/Dockerfile. It is deliberately
# MINIMAL: unlike the OpenClaw extended image (which bakes ~13 Go skill binaries
# plus externalized Slack/Discord/Bedrock/lobster/acpx/parallel plugins and the
# Paperclip skill), Hermes ships its own skills + provider mechanism inside the
# base image, so none of those OpenClaw bakes apply here. The only thing this
# layer adds today is the `aiohttp` runtime dependency the Hermes API server
# needs (see below).
#
# Bump the FROM tag in lockstep with hermes-version.json#upstream — the file
# lies in its label until then.

# =============================================================================
# Base: the Isol8 Hermes fork's published image (private GHCR package).
# =============================================================================
# ghcr.io/isol8ai/hermes-agent is a PRIVATE GHCR package (the Isol8 fork of
# Hermes, NOT ghcr.io/isol8ai/agent which is the OpenClaw fork). It is pulled
# only by the authenticated .github/workflows/build-hermes-image.yml workflow,
# which logs in to GHCR before this FROM resolves. `cdk synth` never pulls it.
#
# The base is Debian 13 (trixie) based, runs s6-overlay as PID 1 (`/init`),
# installs Hermes into a sealed, root-owned, read-only tree at /opt/hermes with
# its Python venv at /opt/hermes/.venv (built by uv + python3.13), and drops to
# the `hermes` user per-service via s6-setuidgid. The writable per-user state
# volume is /opt/data (HERMES_HOME); Isol8 mounts the per-owner EFS access point
# there at provision time.
FROM ghcr.io/isol8ai/hermes-agent:v2026.7.1-isol8.1

# The base image ends as USER root because s6-overlay's /init must run as PID 1
# (it drops to the hermes user per supervised service). We stay root: this layer
# writes into the sealed /opt/hermes venv (root-owned, read-only for the hermes
# user), and — unlike the OpenClaw extended image, whose base ends as USER node —
# we must NOT switch to a non-root user at the end or /init loses the privileges
# it needs to remap HERMES_UID and chown the data volume on boot.
USER root

# ---- Bake aiohttp into the Hermes venv ----
# The Hermes API server (gateway/platforms/api_server.py — the platform that
# serves the OpenAI-format /v1 endpoints Isol8's HermesRuntime adapter drives on
# port 8642) imports `aiohttp` lazily. If aiohttp is absent, the gateway logs
# "[api_server] aiohttp not installed" and SILENTLY skips starting the platform —
# nothing binds 8642, so both the ECS healthcheck and the backend's is_healthy
# TCP probe fail with no obvious error. aiohttp is only pulled in by Hermes'
# optional `[messaging]`/`[slack]`/`[matrix]`/… extras (see the fork's
# pyproject.toml), NOT by its core deps, so the published base image is not
# guaranteed to carry it — we bake it here so the API server always comes up.
#
# Pinned to 3.14.1 to match the exact version Hermes' own pyproject pins across
# every extra that needs aiohttp (it carries the batch of aiohttp CVE fixes noted
# there). Installing the same pin the lockfile already resolves keeps this bake a
# safe no-op if a future base tag already includes aiohttp, and avoids ABI drift.
#
# Installed with uv (baked into the base at /usr/local/bin/uv) targeting the
# sealed venv interpreter explicitly. uv appends only — it cannot shadow or
# downgrade a core module — so the sealed-venv guarantee holds. Build-time only:
# HERMES_DISABLE_LAZY_INSTALLS (a RUNTIME guard) does not affect this, and CI has
# PyPI access; aiohttp ships cp313 manylinux wheels, and the base already carries
# gcc/g++/python3-dev as a fallback compile path.
RUN uv pip install --no-cache-dir --python /opt/hermes/.venv/bin/python "aiohttp==3.14.1"
