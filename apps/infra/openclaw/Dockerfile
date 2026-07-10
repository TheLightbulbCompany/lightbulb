# Extended OpenClaw image — bundles Linux skill binaries so they're available
# at runtime without paying install cost on every container cold-start.
#
# Multi-stage build:
#   builder -> compiles all Go binaries (Go toolchain + build-essential live here)
#   final   -> upstream openclaw + runtime-only apt packages + copied binaries
#
# Splitting the stages keeps the ~600 MB Go toolchain and ~500 MB build-essential
# out of the final image. Targets ~2-3 GB instead of ~8.6 GB so the image fits
# within Fargate's default 20 GiB ephemeral storage with room to spare.
#
# Bump UPSTREAM = `openclaw-version.json#upstream` field. Keep the FROM lines
# below in sync with that field manually until automation lands.

# =============================================================================
# Stage 1: builder — compiles every Go binary, produces /out/bin
# =============================================================================
FROM debian:bookworm-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      build-essential ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# Pin Go from go.dev — Debian 12's apt golang-go is 1.19, but several
# steipete/* tools have go.mod directives requiring Go ≥1.24.
RUN GO_VER=1.24.2 && \
    curl -sL https://go.dev/dl/go${GO_VER}.linux-amd64.tar.gz \
      | tar -xz -C /usr/local

ENV PATH=/usr/local/go/bin:$PATH \
    GOPATH=/opt/go \
    GOBIN=/out/bin
# CGO_ENABLED is left at Go's default (=1) so packages with C-binding deps
# build correctly. wacli@v0.8.1 (and any future tool that pulls in
# go-sqlite3 or similar) emits a `wacli_requires_cgo_enabled_1_for_go_sqlite3`
# stub-import compile error when CGO is off. Pure-Go tools build identically
# at CGO=1 vs CGO=0; build-essential is already installed above (Layer 1)
# so gcc is available, and the final image FROMs Debian-based openclaw so
# dynamic libc linking works at runtime.

RUN mkdir -p /out/bin

# Steipete + community Go tools — `go install` writes binaries into GOBIN (=/out/bin).
# Module-path subtlety: github transferred most of these repos from steipete/*
# to openclaw/* in early 2026, so browser URLs 301-redirect. But `go install`
# resolves the canonical path from each release's go.mod, NOT from github
# redirects. So per tool we have to use whichever path is declared at @latest:
#   wacli@v0.8.1 → openclaw/wacli (released after rename)
#   songsee/gogcli/goplaces/spogo @latest → still steipete/* (released before
#     rename; their go.mod hasn't been bumped to the new path)
# When a future release of those four ships with `module github.com/openclaw/*`
# in its go.mod, flip them too. Verify with:
#   curl -s https://raw.githubusercontent.com/<owner>/<tool>/HEAD/go.mod | head -1
RUN go install github.com/steipete/eightctl/cmd/eightctl@latest && \
    go install github.com/Hyaxia/blogwatcher/cmd/blogwatcher@latest && \
    go install github.com/steipete/ordercli/cmd/ordercli@latest && \
    go install github.com/steipete/sonoscli/cmd/sonos@latest && \
    go install github.com/openclaw/wacli/cmd/wacli@latest && \
    go install github.com/steipete/gifgrep/cmd/gifgrep@latest && \
    go install github.com/steipete/blucli/cmd/blu@latest && \
    go install github.com/steipete/songsee/cmd/songsee@latest && \
    go install github.com/steipete/gogcli/cmd/gog@latest && \
    go install github.com/steipete/goplaces/cmd/goplaces@latest && \
    go install github.com/steipete/spogo/cmd/spogo@latest

# notesmd-cli (formerly obsidian-cli) — `go build` from a cloned repo.
RUN git clone --depth=1 https://github.com/Yakitrak/notesmd-cli.git /tmp/notesmd && \
    cd /tmp/notesmd && go build -o /out/bin/notesmd-cli .

# openhue — `go build` from repo root (skipping `make build`/GoReleaser).
RUN git clone --depth=1 https://github.com/openhue/openhue-cli.git /tmp/openhue && \
    cd /tmp/openhue && go build -o /out/bin/openhue .

# =============================================================================
# Stage 2: final — runtime-only image
# =============================================================================
# Fat (non-slim) variant: bundles plugin runtime deps so first-use of
# browser/codex/channels doesn't pay a ~6min lazy npm-install on cold start.
FROM ghcr.io/isol8ai/agent:2026.7.1-beta.3-isol8.1
# Base bundles the native `meta` provider (extensions/meta, enabledByDefault) —
# it is NOT in the fork's package.json `files` prune list, so it ships in the base
# image and needs no bake layer here (unlike slack/discord/bedrock/lobster/parallel/acpx
# below, which upstream prunes and we re-bake). meta/muse-spark-1.1, MODEL_API_KEY.
#
# PLUGIN-PIN POLICY (read before bumping the @openclaw/* pins below): the baked
# plugins pin to the latest PUBLISHED 7.1-line plugin dist on npm, which is
# 2026.7.1-beta.2 as of this bump — beta.3 plugin dists are NOT published yet
# (`npm view @openclaw/slack@2026.7.1-beta.3` 404s). So the pins sit one beta
# behind the beta.3 base host. That is safe: the plugins declare pluginApi
# compat `>=2026.6.11`, which the beta.3 host satisfies, and the previous fleet
# ran a much larger skew (acpx@2026.6.1 on a 2026.6.10 base). "Lockstep with the
# FROM tag" in the per-plugin comments therefore means "latest published dist of
# the FROM tag's release line", not an exact version match. Bump to
# 2026.7.1-beta.3 in a follow-up once those dists publish. The dev canary boots
# the built image to confirm every baked plugin converges on the beta.3 host.

USER root

# ---- Layer 1: runtime apt packages ----
# Dropped from upstream extended image: build-essential, make (build-only).
# Keeps git/curl/wget/gnupg — runtime tools (skills shell out, clawhub uses git).
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      ffmpeg jq ripgrep tmux poppler-utils \
      socat python3-pip sqlite3 python3 \
      curl wget ca-certificates gnupg git unzip \
    && rm -rf /var/lib/apt/lists/*

# ---- AWS CLI v2 (official bundled installer; apt/pip ship only v1) ----
RUN curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-2.17.0.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscliv2.zip

# ---- Layer 2: 1Password CLI (apt repo) ----
RUN curl -sS https://downloads.1password.com/linux/keys/1password.asc \
      | gpg --dearmor -o /usr/share/keyrings/1password-archive-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/amd64 stable main" \
      > /etc/apt/sources.list.d/1password.list && \
    apt-get update -qq && apt-get install -y --no-install-recommends 1password-cli && \
    rm -rf /var/lib/apt/lists/*

# ---- Layer 3: GitHub CLI (apt repo) ----
# Was previously a `curl … | tar xz` pipe of the GitHub release tarball,
# which broke on the bookworm-slim base when the gh tarball download went
# through GitHub's release-assets redirect. The apt-repo path matches the
# 1Password setup just above and is what GitHub officially recommends.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update -qq && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

# ---- Layer 4: uv + uvx (Python package manager) ----
# The astral.sh installer drops BOTH `uv` and `uvx` into /tmp/.local/bin/.
# uv runs the package manager; uvx runs a one-shot tool from a transient
# venv (e.g. `uvx arxiv-mcp-server`). Without uvx on PATH, any
# user-configured MCP server invoked as `uvx <package>` fails at spawn
# with "uvx: not found" — bundle-mcp reports "Connection closed" and
# re-attempts on every session prep, eventually driving the container
# into ECS-healthcheck-unhealthy replacement.
RUN curl -sSL https://astral.sh/uv/install.sh | HOME=/tmp sh && \
    install -m 755 /tmp/.local/bin/uv /usr/local/bin/uv && \
    install -m 755 /tmp/.local/bin/uvx /usr/local/bin/uvx

# ---- Layer 5: pip packages (no cache, pre-built wheels only) ----
# nano-pdf entry points land on PATH via system python (/usr/local/bin).
RUN pip install --no-cache-dir --break-system-packages \
      openai-whisper websockets nano-pdf

# ---- Layer 6: Go-built skills (copied from builder) ----
# 13 binaries built in the builder stage — Go toolchain itself stays out
# of the final image, saving ~600 MB.
COPY --from=builder /out/bin/ /usr/local/bin/

# OpenClaw's `obsidian` SKILL.md still requires bin "obsidian-cli", so
# alias to the renamed `notesmd-cli` to keep eligibility.
RUN ln -s /usr/local/bin/notesmd-cli /usr/local/bin/obsidian-cli

# ---- Layer 7: himalaya (pre-built binary via install.sh) ----
RUN curl -sSL https://raw.githubusercontent.com/pimalaya/himalaya/master/install.sh | sh

# ---- Layer 8: Node packages ----
# All globals install with --ignore-scripts (faster, no build deps).
# qmd was previously installed here; removed 2026-05-02. qmd's
# better-sqlite3 store on EFS deadlocked the gateway's V8 main thread
# (`rpc_wait_bit_killable` — Linux NFS client retries forever).
# We now use openclaw's `builtin` memory backend (flat MEMORY.md
# files), which doesn't open a sqlite connection on EFS at all.
ENV NPM_CONFIG_PREFIX=/usr/local
# @paperclipai/mcp-server is the Friday<->Paperclip MCP and is required on
# EVERY container. Bake it as a global bin (`paperclip-mcp-server` on PATH) so
# that once this image tag is rolled out, config.py can switch the seeded
# command from `npx -y @paperclipai/mcp-server` to the bare `paperclip-mcp-server`
# — instant spawn, off the per-run npx connect storm, and tools that load
# reliably (a bare command with no installed bin silently exposes zero tools).
# The config switch is deferred to a follow-up so it only lands after this baked
# tag is live. Pinned like the other bakes.
# mcporter exposes any configured MCP server's tools as deterministic CLI
# commands (`mcporter call <server>.<tool>`). Lobster workflow steps cannot
# invoke MCP tools — those are surfaced only to the agent LLM's tool loop — so
# this is the bridge that lets pipelines reach hosted-OAuth MCP servers (e.g.
# Attio) the same way they call any other CLI. Baked as a global bin to stay
# off the per-run npx connect storm; its auth comes from a pre-seeded vault.
RUN npm i -g --ignore-scripts \
      clawhub openai \
      @steipete/oracle @steipete/summarize \
      @anthropic-ai/claude-code @google/gemini-cli \
      @xdevplatform/xurl \
      mcporter@0.12.0 \
      @paperclipai/mcp-server@2026.529.0

# Durable lobster pipeline state. Lobster's native `state.get`/`state.set` verbs are a
# real, gateway-free file-KV, but they default to $HOME/.lobster/state — on the ephemeral
# container FS, wiped on every task restart. Point them at the per-user EFS mount instead,
# so a workflow's queues/counters/cursors survive restarts and are shared with the agent
# loop (same uid, same EFS). This is why no custom KV tool is needed: the engine already
# ships durable state; it just needs to be aimed at EFS. (`fast-io` is fictional — it's the
# Fast.io SaaS, never wired into mcp.servers — so workflows use `state.*`, not `fast-io`.)
ENV LOBSTER_STATE_DIR=/home/node/.openclaw/lobster-state

# ---- Layer 9: Playwright Chromium + Chromium system libs + /usr/bin symlink ----
# Upstream openclaw:2026.6.1 ships the playwright npm package (at
# /app/node_modules/playwright-core, v1.60.0) but NOT the Chromium binary
# and NOT the GUI libs Chromium needs to launch. The chromium install is
# gated behind upstream Dockerfile's `OPENCLAW_INSTALL_BROWSER` build arg,
# which is empty by default; the published image is built without it.
#
# This image now has two browser-using audiences, each with its own
# (incompatible) idea of where Chromium lives:
#
#   1. Third-party Playwright-based MCPs (Resy, etc.) call
#      `chromium.launch()`, which Playwright hardcodes to find its bundled
#      binary at `$HOME/.cache/ms-playwright/chromium-<rev>/...`. Playwright
#      ignores system Chromium by design.
#
#   2. OpenClaw's `browser` plugin spawns Chrome directly via CDP
#      (`extensions/browser/src/browser/chrome.ts:417`), using a resolver
#      that probes `/usr/bin/google-chrome`, `/usr/bin/chromium`, etc. It
#      does NOT check Playwright's cache.
#
# We satisfy both by installing playwright-core's pinned Chromium into
# Playwright's expected cache path (audience #1), then symlinking that
# binary into `/usr/bin/chromium` (audience #2). One binary, two access
# paths, no duplicated apt-installed Chromium.
#
# The `chromium-*` glob in the symlink target re-resolves at every image
# rebuild — when upstream bumps playwright-core, the revision dir name
# changes (e.g. chromium-1217 → chromium-1219) and the symlink picks up
# the new dir automatically. Only one revision dir exists per build, so
# the glob is unambiguous.
#
# --with-deps installs the GUI libs Chromium needs (libatk-1.0, libnss3,
# libgbm, libxkbcommon, libasound, etc.) at the system level. These cover
# any future MCP that brings a different Playwright version too — they
# only need to download their own Chromium binary, not re-install libs.
RUN mkdir -p /home/node/.cache/ms-playwright && \
    PLAYWRIGHT_BROWSERS_PATH=/home/node/.cache/ms-playwright \
      node /app/node_modules/playwright-core/cli.js install --with-deps chromium && \
    chown -R node:node /home/node/.cache/ms-playwright && \
    ln -sf /home/node/.cache/ms-playwright/chromium-*/chrome-linux64/chrome /usr/bin/chromium

# ---- Layer 10: externalized channel plugins (Slack, Discord) ----
# Upstream OpenClaw externalized these channel plugins out of the core image
# (@openclaw/slack and @openclaw/discord are pruned from the published ghcr
# image, which keeps only the still-bundled channels like telegram). Without
# the plugin present on a discovery root, the gateway hard-throws
# "Channel is unavailable: <id>" at send time for the channels we enable in
# openclaw.json. We bake them into the stock plugin root /app/dist/extensions/<id>
# so they are root-owned (the loader rejects any plugin dir not owned by uid 0)
# and auto-activate from the existing channels.<id>.enabled config. A runtime
# `openclaw plugins install` would also work — it installs into the per-user EFS
# plugin root as uid 1000, which the loader accepts there — but baking avoids the
# per-container download/install cost on the constrained box and keeps the plugin
# present uniformly across the whole fleet. Each plugin's runtime deps are co-located
# under its own node_modules so node resolution finds them. Versions are pinned
# to the host OpenClaw version; bump them in lockstep with the FROM tag /
# openclaw-version.json#upstream.
RUN set -eux; \
    for P in slack discord; do \
      mkdir -p "/opt/stage-$P"; \
      npm install --prefix "/opt/stage-$P" "@openclaw/$P@2026.7.1-beta.2"; \
      mkdir -p "/app/dist/extensions/$P"; \
      cp -R "/opt/stage-$P/node_modules/@openclaw/$P/." "/app/dist/extensions/$P/"; \
      mkdir -p "/app/dist/extensions/$P/node_modules"; \
      cp -R /opt/stage-$P/node_modules/. "/app/dist/extensions/$P/node_modules/"; \
      rm -rf "/app/dist/extensions/$P/node_modules/@openclaw/$P"; \
      rm -rf "/opt/stage-$P"; \
    done; \
    chown -R root:root /app/dist/extensions/slack /app/dist/extensions/discord

# ---- Layer 11: externalized Amazon Bedrock provider plugin ----
# Upstream externalized the Bedrock provider out of the core image at this
# version: the gateway logs "plugin not installed: amazon-bedrock" and every
# Bedrock-backed run fails with "No API provider registered for api:
# bedrock-converse-stream". A runtime `openclaw plugins install` would also work
# (into the per-user EFS plugin root as uid 1000), but we bake it root-owned into
# the stock plugin root to avoid the per-container install cost and keep it
# uniform across the fleet. The npm package
# is `@openclaw/amazon-bedrock-provider`; the plugin id (and the discovery dir
# the loader keys on) is `amazon-bedrock`. anthropic/openai providers stay
# bundled in the base image, so only Bedrock needs baking. Pin in lockstep with
# the FROM tag / openclaw-version.json#upstream.
RUN set -eux; \
    mkdir -p /opt/stage-amazon-bedrock; \
    npm install --prefix /opt/stage-amazon-bedrock "@openclaw/amazon-bedrock-provider@2026.7.1-beta.2"; \
    mkdir -p /app/dist/extensions/amazon-bedrock; \
    cp -R /opt/stage-amazon-bedrock/node_modules/@openclaw/amazon-bedrock-provider/. /app/dist/extensions/amazon-bedrock/; \
    mkdir -p /app/dist/extensions/amazon-bedrock/node_modules; \
    cp -R /opt/stage-amazon-bedrock/node_modules/. /app/dist/extensions/amazon-bedrock/node_modules/; \
    rm -rf /app/dist/extensions/amazon-bedrock/node_modules/@openclaw/amazon-bedrock-provider; \
    rm -rf /opt/stage-amazon-bedrock; \
    chown -R root:root /app/dist/extensions/amazon-bedrock

# ---- Layer 12: externalized lobster workflow plugin ----
# Upstream externalized the `lobster` workflow engine out of the core image in
# the same wave as the channel/provider plugins above: the gateway logs
# "plugin not installed: lobster" and every `.lobster` pipeline fails to run.
# Lobster is the execution engine behind the Atlas recruiter seed agent (whose
# entire methodology is .lobster pipelines) and any agent built on pipelines,
# and core/containers/config.py enables it on every container via
# workflow_plugin_entries(), so it must be present fleet-wide. The npm package
# and plugin id are both `lobster`; its discovery dir is /app/dist/extensions/lobster.
# (`llm-task`, the other workflow extension we enable, stays bundled in the base
# image, so only lobster needs baking.) A runtime `openclaw plugins install` would
# also work, but bake it root-owned for the same reasons as the layers above,
# pinned in lockstep with the FROM tag / openclaw-version.json#upstream.
RUN set -eux; \
    mkdir -p /opt/stage-lobster; \
    npm install --prefix /opt/stage-lobster "@openclaw/lobster@2026.7.1-beta.2"; \
    mkdir -p /app/dist/extensions/lobster; \
    cp -R /opt/stage-lobster/node_modules/@openclaw/lobster/. /app/dist/extensions/lobster/; \
    mkdir -p /app/dist/extensions/lobster/node_modules; \
    cp -R /opt/stage-lobster/node_modules/. /app/dist/extensions/lobster/node_modules/; \
    rm -rf /app/dist/extensions/lobster/node_modules/@openclaw/lobster; \
    rm -rf /opt/stage-lobster; \
    chown -R root:root /app/dist/extensions/lobster

# ---- Layer 12b: externalized web-search provider (parallel) ----
# OpenClaw 6.10 stopped auto-selecting a keyless web_search provider, so agents
# that call the native web_search tool fail with "disabled or no provider"
# unless one is explicitly configured. core/containers/config.py sets
# tools.web.search.provider="parallel-free" + plugins.entries.parallel.enabled
# on every container; parallel-free is Parallel's keyless hosted Search MCP
# (search.parallel.ai/mcp), reliable from Fargate (unlike the rate-limited
# DuckDuckGo fallback). The npm package is `@openclaw/parallel-plugin`; its
# plugin id + discovery dir are `parallel`. Bake it root-owned, pinned in
# lockstep with the FROM tag, same as the layers above.
RUN set -eux; \
    mkdir -p /opt/stage-parallel; \
    npm install --prefix /opt/stage-parallel "@openclaw/parallel-plugin@2026.7.1-beta.2"; \
    mkdir -p /app/dist/extensions/parallel; \
    cp -R /opt/stage-parallel/node_modules/@openclaw/parallel-plugin/. /app/dist/extensions/parallel/; \
    mkdir -p /app/dist/extensions/parallel/node_modules; \
    cp -R /opt/stage-parallel/node_modules/. /app/dist/extensions/parallel/node_modules/; \
    rm -rf /app/dist/extensions/parallel/node_modules/@openclaw/parallel-plugin; \
    rm -rf /opt/stage-parallel; \
    chown -R root:root /app/dist/extensions/parallel

# ---- Layer 13: externalized ACP runtime backend (acpx) ----
# acpx is the official ACP runtime plugin that lets a native OpenClaw agent
# (e.g. Jarvis) spawn external coding harnesses — Claude Code, Codex, Gemini —
# over the Agent Client Protocol via sessions_spawn({runtime:"acp"}). Like the
# slack/discord/bedrock plugins above, acpx is pruned from the published base
# image (`!dist/extensions/acpx/**` in the fork's root package.json `files`, so
# the release build — which passes no OPENCLAW_EXTENSIONS — strips
# dist/extensions/acpx). Without it on a discovery root the gateway reports
# "ACP runtime backend is not configured" and every /acp spawn or
# sessions_spawn({runtime:"acp"}) fails. Bake it root-owned into the stock
# plugin root so it auto-activates from plugins.entries.acpx.enabled (the
# runtime `openclaw plugins install` path is blocked — the per-user EFS mount
# forces uid 1000, which the loader rejects). The npm package `@openclaw/acpx`
# pulls its adapter deps (@agentclientprotocol/claude-agent-acp,
# @zed-industries/codex-acp) and the `acpx` process-manager binary transitively;
# the Claude Code / Gemini CLIs themselves are already global bins (Layer 8).
# Do NOT pass --ignore-scripts here: the `acpx` dep fetches a platform-specific
# binary via its postinstall hook, which CI's native per-arch build resolves
# correctly. Inert until openclaw.json enables acpx + acp. Pin in lockstep with
# the FROM tag / openclaw-version.json#upstream.
RUN set -eux; \
    mkdir -p /opt/stage-acpx; \
    npm install --prefix /opt/stage-acpx "@openclaw/acpx@2026.7.1-beta.2"; \
    mkdir -p /app/dist/extensions/acpx; \
    cp -R /opt/stage-acpx/node_modules/@openclaw/acpx/. /app/dist/extensions/acpx/; \
    mkdir -p /app/dist/extensions/acpx/node_modules; \
    cp -R /opt/stage-acpx/node_modules/. /app/dist/extensions/acpx/node_modules/; \
    rm -rf /app/dist/extensions/acpx/node_modules/@openclaw/acpx; \
    rm -rf /opt/stage-acpx; \
    chown -R root:root /app/dist/extensions/acpx

# ---- Layer 14: baked aws-exec-profile plugin ----
# Hand-written, dependency-free OpenClaw plugin (lives in-repo, NOT an npm
# package). Registers a `resolve_exec_env` hook that injects
# AWS_PROFILE=isol8-connector into the agent's exec'd commands ONLY when the
# owner's connector AWS profile has been materialized at AWS_CONFIG_FILE — the
# node/harness process (and Bedrock's in-process AWS SDK) keeps the host ECS
# task-role identity. COPY lands it root-owned in the stock plugin root (the
# loader rejects any plugin dir not owned by uid 0); it auto-activates from
# plugins.entries.aws-exec-profile.enabled (set by core/containers/config.py).
# Its only import is the host SDK (`openclaw/plugin-sdk/plugin-entry`), resolved
# by self-referencing the `openclaw` package from /app — same mechanism the
# baked extensions above use — so it ships no node_modules of its own.
COPY plugins/aws-exec-profile /app/dist/extensions/aws-exec-profile
RUN chown -R root:root /app/dist/extensions/aws-exec-profile

# ---- Layer 15: bundled Paperclip skill ----
# The `paperclip` skill is the agent's operating manual for the Paperclip
# control plane — checking assignments, updating issues, posting comments,
# approvals, and (critically) managing ROUTINES, the single governed home for
# recurring agent work now that the in-container `cron` tool is denied.
# OpenClaw auto-discovers bundled skills at /app/skills/<name>/SKILL.md (see
# the fork's src/skills/loading), so we COPY the vendored skill there. COPY
# lands it root-owned, matching /app and the externalized-plugin bakes above.
# Vendored from the Paperclip skills-catalog at the 2026.6 (June) line, kept in
# step with the deployed Paperclip backend in paperclip-version.json (NOT with
# the @paperclipai/mcp-server bin baked above, which is unwired and pinned
# separately). NOTE: this vendored copy carries a deliberate Isol8 write-path
# customization — every API write goes through skills/paperclip/scripts/paperclip-api.sh
# (jq-built, string-safe bodies) instead of hand-written jq/inline JSON, because
# a model-emitted malformed body (e.g. `{body:}`) once silently failed a write
# and stalled a prod run forever. When re-vendoring from upstream, re-apply that
# helper mandate (see the SKILL.md header comment) rather than letting it revert.
# The skill drives Paperclip's REST API directly; each agent
# authenticates with its OWN company-scoped key, loaded from a per-agent
# workspace file (~/.openclaw/workspaces/{agent_id}/paperclip-claimed-api-key.json,
# staged at provision — see core/services/{paperclip_api_key,friday_identity}).
# The agent's AGENTS.md seed exports PAPERCLIP_API_KEY / API_URL / COMPANY_ID /
# AGENT_ID from that file itself; they are NOT container-wide task env.
COPY skills/paperclip /app/skills/paperclip


# ---- Restore runtime context ----
USER node
WORKDIR /home/node
