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
#   gogcli@v0.35.0 → openclaw/gogcli (flipped 2026-08: v0.35.0's go.mod
#     declares the new path, so the steipete/* install path started failing
#     with "module declares its path as: github.com/openclaw/gogcli")
#   songsee/goplaces/spogo @latest → still steipete/* (released before
#     rename; their go.mod hasn't been bumped to the new path)
# When a future release of those three ships with `module github.com/openclaw/*`
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
    go install github.com/openclaw/gogcli/cmd/gog@latest && \
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
FROM ghcr.io/isol8ai/agent:2026.7.2-isol8.4
# PLUGIN-PIN POLICY (read before bumping the @openclaw/* pins below): the baked
# plugins pin to the latest PUBLISHED plugin dist of the FROM tag's release
# line on npm. The FROM tag is on the 2026.7.2 line, which is BETA-only upstream
# — its latest published dist is 2026.7.2-beta.3 (npm `beta` dist-tag; no stable
# 2026.7.2 dist exists yet), so all eight baked plugins pin to 2026.7.2-beta.3 to
# stay in lockstep with the 7.2 host (this base rolls to DEV only). Keep these
# pins in lockstep with the FROM tag on every base bump: the 7.1 line moved the
# channel inbound-dispatch contract (turn-adoption ack), so a stale channel
# plugin (slack/discord) on a newer host is not a safe skew. The dev canary boots
# the built image to confirm every baked plugin converges on the host.

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
      mcporter@0.12.0 \
      @paperclipai/mcp-server@2026.529.0

# xurl downloads its native Go binary in npm postinstall. Keep it outside the
# --ignore-scripts global install above; otherwise the JS wrapper is present but
# every `xurl` invocation fails with "xurl binary not found".
RUN npm i -g @xdevplatform/xurl@1.2.3 && xurl --help >/dev/null

# OpenCode: the ACP coding harness for chatgpt_oauth owners (registered via
# the acpx agents map in openclaw.json — see backend build_acp_config). Its
# platform binary arrives via optionalDependencies + postinstall, so like xurl
# it stays outside the --ignore-scripts install above. Version-pinned; the
# smoke test fails the build if the binary didn't materialize for this arch.
RUN npm i -g opencode-ai@1.18.15 && opencode --version >/dev/null

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
      npm install --prefix "/opt/stage-$P" "@openclaw/$P@2026.7.2-beta.3"; \
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
    npm install --prefix /opt/stage-amazon-bedrock "@openclaw/amazon-bedrock-provider@2026.7.2-beta.3"; \
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
    npm install --prefix /opt/stage-lobster "@openclaw/lobster@2026.7.2-beta.3"; \
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
    npm install --prefix /opt/stage-parallel "@openclaw/parallel-plugin@2026.7.2-beta.3"; \
    mkdir -p /app/dist/extensions/parallel; \
    cp -R /opt/stage-parallel/node_modules/@openclaw/parallel-plugin/. /app/dist/extensions/parallel/; \
    mkdir -p /app/dist/extensions/parallel/node_modules; \
    cp -R /opt/stage-parallel/node_modules/. /app/dist/extensions/parallel/node_modules/; \
    rm -rf /app/dist/extensions/parallel/node_modules/@openclaw/parallel-plugin; \
    rm -rf /opt/stage-parallel; \
    chown -R root:root /app/dist/extensions/parallel

# ---- Layer 12c: externalized web-fetch fallback provider (firecrawl) ----
# OpenClaw's keyless Firecrawl fetch provider is the Readability-failure
# fallback that tools.web.fetch selects: core/containers/config.py sets
# tools.web.fetch.provider="firecrawl" + plugins.entries.firecrawl.enabled on
# every container. Like the parallel provider above, firecrawl is externalized
# out of the base image — but it also ships ONLY on the npm `beta` dist-tag (no
# stable release-line dist), which is the spec OpenClaw's doctor resolves for
# this plugin id. With firecrawl configured but absent from every discovery
# root, OpenClaw 7.2's doctor-on-boot npm-installs
# `@openclaw/firecrawl-plugin@beta` into the per-user EFS plugin root on EVERY
# cold start — measured +2-4 min of dead boot time (dev log gap: `Installed
# missing configured plugin "firecrawl" from @openclaw/firecrawl-plugin@beta`).
# Baking it root-owned into the stock plugin root makes the doctor find the
# plugin id already present and skip the install fleet-wide (the loader rejects
# any plugin dir not owned by uid 0; it auto-activates from
# plugins.entries.firecrawl.enabled). The npm package is
# `@openclaw/firecrawl-plugin`; its plugin id + discovery dir are `firecrawl`.
# `@beta` resolves to 2026.7.2-beta.3 today — the same dist every other baked
# plugin pins to on this 7.2 base — so it stays in lockstep with the FROM tag;
# re-resolve `npm view @openclaw/firecrawl-plugin@beta version` on each base
# bump. Bake it root-owned, same as the layers above.
RUN set -eux; \
    mkdir -p /opt/stage-firecrawl; \
    npm install --prefix /opt/stage-firecrawl "@openclaw/firecrawl-plugin@2026.7.2-beta.3"; \
    mkdir -p /app/dist/extensions/firecrawl; \
    cp -R /opt/stage-firecrawl/node_modules/@openclaw/firecrawl-plugin/. /app/dist/extensions/firecrawl/; \
    mkdir -p /app/dist/extensions/firecrawl/node_modules; \
    cp -R /opt/stage-firecrawl/node_modules/. /app/dist/extensions/firecrawl/node_modules/; \
    rm -rf /app/dist/extensions/firecrawl/node_modules/@openclaw/firecrawl-plugin; \
    rm -rf /opt/stage-firecrawl; \
    chown -R root:root /app/dist/extensions/firecrawl

# ---- Layer 12d: first-party Isol8 marketplace tool plugin ----
# Read-only agent tools (marketplace_search, recommend_agents) so an agent can
# name real marketplace listings instead of waving at a URL. Enabled fleet-wide
# via plugins.entries["isol8-marketplace"] in core/containers/config.py.
#
# Second first-party plugin in-repo, same shape as plugins/aws-exec-profile
# (Layer 14): the committed dist/ IS the baked artifact, so this is a plain
# COPY — no npm install and no build step in the image. Rebuilding dist/ is a
# local `npm run build`, and the plugin's own .gitignore overrides the repo-root
# dist/ ignore so it stays tracked.
#
# It ships NO node_modules of its own. Its two imports resolve from the host by
# self-referencing the `openclaw` package from /app — `openclaw/plugin-sdk/
# tool-plugin` exactly as aws-exec-profile does, and `typebox` from
# /app/node_modules (openclaw depends on typebox 1.3.3, which the plugin pins
# in lockstep). The `openclaw` devDependency exists only to typecheck the build
# locally and is ~460MB; it must never reach the image, which this COPY-only
# layer guarantees structurally.
#
# Root-owned like every other baked plugin — the loader rejects any plugin dir
# not owned by uid 0. Enabled fleet-wide via
# plugins.entries["isol8-marketplace"] in core/containers/config.py.
COPY plugins/isol8-marketplace /app/dist/extensions/isol8-marketplace
RUN chown -R root:root /app/dist/extensions/isol8-marketplace

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
    npm install --prefix /opt/stage-acpx "@openclaw/acpx@2026.7.2-beta.3"; \
    mkdir -p /app/dist/extensions/acpx; \
    cp -R /opt/stage-acpx/node_modules/@openclaw/acpx/. /app/dist/extensions/acpx/; \
    mkdir -p /app/dist/extensions/acpx/node_modules; \
    cp -R /opt/stage-acpx/node_modules/. /app/dist/extensions/acpx/node_modules/; \
    rm -rf /app/dist/extensions/acpx/node_modules/@openclaw/acpx; \
    rm -rf /opt/stage-acpx; \
    chown -R root:root /app/dist/extensions/acpx

# ---- Layer 13b: tokenjuice exec-output compaction plugin ----
# Official @openclaw plugin that registers agentToolResultMiddleware compacting
# noisy `exec`/`bash` tool results AFTER the command runs, before the result
# re-enters the agent session. Compaction is deterministic rule-driven
# reducers (no LLM aux calls); exact file-content reads stay raw and unsafe
# mixed command sequences pass through untouched, so commands, exit codes,
# and semantics are never altered. Every compacted token is tokens we don't
# pay for on the managed-Bedrock/trial paths (bedrock-gate meters input
# tokens) and context-window pressure the size-based pruning patch doesn't
# have to absorb. Enabled fleet-wide via plugins.entries.tokenjuice
# (core/containers/config.py workflow_plugin_entries()); its configSchema is
# strict-empty, so the entry is enable-only. Not bundled in the published
# base image (the release build passes no OPENCLAW_EXTENSIONS), so bake it
# root-owned like the layers above. Pin in lockstep with the FROM tag /
# openclaw-version.json#upstream.
RUN set -eux; \
    mkdir -p /opt/stage-tokenjuice; \
    npm install --prefix /opt/stage-tokenjuice "@openclaw/tokenjuice@2026.7.2-beta.3"; \
    mkdir -p /app/dist/extensions/tokenjuice; \
    cp -R /opt/stage-tokenjuice/node_modules/@openclaw/tokenjuice/. /app/dist/extensions/tokenjuice/; \
    mkdir -p /app/dist/extensions/tokenjuice/node_modules; \
    cp -R /opt/stage-tokenjuice/node_modules/. /app/dist/extensions/tokenjuice/node_modules/; \
    rm -rf /app/dist/extensions/tokenjuice/node_modules/@openclaw/tokenjuice; \
    rm -rf /opt/stage-tokenjuice; \
    chown -R root:root /app/dist/extensions/tokenjuice

# ---- Layer 13c: Gen Sage ADR safety plugin + fleet ~/.sage config ----
# Third-party (Gen Digital, github.com/gendigitalinc/sage) Agent Detection &
# Response layer: intercepts exec/read/write/edit/web_fetch/apply_patch tool
# calls BEFORE execution and allow/ask/denies against ~300 local YAML threat
# rules (reverse shells, credential exfil, persistence, typosquats) plus
# cloud URL/file/package reputation. Channel-exposed agents (Telegram/Slack/
# Discord senders) are an untrusted-input surface; Sage is the only guard
# between an injected "run this curl|bash" and the container's exec: full.
# UNLIKE the @openclaw layers above this is versioned independently of the
# host — bump deliberately, not on base bumps (compat: pluginApi >=2026.3.28).
# Plugin id (and discovery dir) is `sage-openclaw`; configSchema is
# strict-empty so the plugins.entries entry is enable-only, and ALL behavior
# config lives in ~/.sage/config.json. /home/node/.sage is NOT under the
# per-user EFS mount (only ~/.openclaw is), so the baked config below is the
# fleet-wide policy. It is the product of a rule-by-rule audit of the v0.11.0
# threat YAMLs against what our agents legitimately do (2026-07-17); the
# levers are preset-only thresholds + per-rule disabled_threats:
#   - sensitivity=relaxed: instant-deny only >=0.95-confidence rules — 13
#     Linux-relevant, all attack-shaped (curl|bash, rm -rf /, mkfs,
#     base64|sh, /etc/shadow, authorized_keys tampering, PEM-in-content,
#     unicode smuggling). 0.70-0.95 verdicts return requireApproval with
#     timeoutBehavior:"deny": they ride the host plugin-approval broker
#     (exec-approval delivery rails; the agent's own LLM canNOT approve —
#     older conversation-relay docs are stale) and FAIL CLOSED after the
#     120s host default when nobody answers, the normal case for unattended
#     Paperclip routine/heartbeat runs. That ask band is reverse shells /
#     exfil chains / obfuscation — patterns legit agent work essentially
#     never emits.
#   - NEVER set balanced/paranoid fleet-wide: their 0.50-0.70 band includes
#     "export of credential env var" + "reading credential file" (conf 0.60)
#     — our seeded Paperclip flow exports PAPERCLIP_API_KEY from
#     paperclip-claimed-api-key.json on EVERY write, so balanced would
#     stall+deny the entire Teams surface fleet-wide.
#   - disabled_threats CLT-CRED-001/006 (raw API-key-string patterns,
#     match_on command+content, conf 0.80/0.92): they fire on a BYOK owner's
#     own sk-/AKIA/ghp_/sk_live keys being written to .env or curl'd —
#     routine, user-visible work. The exfil-SHAPED rules (dotfile-piped-to-
#     curl, env-dump-and-send) stay active, so injection exfil is still
#     covered without punishing legitimate key handling.
#   - community_iq=false (never ship tenant detection metadata to Gen's
#     cloud); pi_check left default-off (runtime ML model download — wrong
#     for this box). URL/file/package cloud reputation stays ON (known-
#     malicious URL/package blocking; sends URLs/package names — not file
#     contents — to Gen's reputation API).
# Runtime writes audit logs to ~/.sage/audit.jsonl, hence chown to node
# (uid 1000); logs are ephemeral with the task, like the rest of overlay-fs.
RUN set -eux; \
    mkdir -p /opt/stage-sage; \
    npm install --prefix /opt/stage-sage "@gendigital/sage-openclaw@0.11.0"; \
    mkdir -p /app/dist/extensions/sage-openclaw; \
    cp -R /opt/stage-sage/node_modules/@gendigital/sage-openclaw/. /app/dist/extensions/sage-openclaw/; \
    mkdir -p /app/dist/extensions/sage-openclaw/node_modules; \
    cp -R /opt/stage-sage/node_modules/. /app/dist/extensions/sage-openclaw/node_modules/; \
    rm -rf /app/dist/extensions/sage-openclaw/node_modules/@gendigital/sage-openclaw; \
    rm -rf /opt/stage-sage; \
    chown -R root:root /app/dist/extensions/sage-openclaw
COPY sage/config.json /home/node/.sage/config.json
RUN chown -R 1000:1000 /home/node/.sage

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

# ---- Layer 16: bundled Isol8 platform skill ----
# The `isol8` skill is every agent's authoritative reference for the
# platform itself — UI surfaces (incl. the Integrations tab), marketplace
# mechanics, plans/providers, the Paperclip org model, and hard boundaries.
# Baked (not seeded) so the WHOLE fleet — Friday, Atlas, scaffolded
# specialists, marketplace hires — gets identical, current platform
# knowledge on every image promotion, with no per-agent seeding and no
# reprovision. Dynamic per-owner state deliberately lives OUTSIDE the image
# in ~/.openclaw/isol8-facts.json (written by the backend — see
# core/services/owner_facts.py); the skill points agents at it.
# Authored in-repo (not vendored). Keep in lockstep with the anchors listed
# in the SKILL.md maintenance header.
COPY skills/isol8 /app/skills/isol8


# ---- Layer 17: resolve the `openclaw` peer dependency for baked plugins ----
# @openclaw/* plugins declare `openclaw` (the fork core package; its package.json
# is /app/package.json, which exports plugin-sdk/*) as a PEER dependency, so the
# `npm install @openclaw/<p>` steps above never install it. The fork's own
# bundled plugins under /app/extensions are pnpm workspace members and get
# `openclaw` linked automatically; our npm-baked plugins under
# /app/dist/extensions do NOT — so any baked-plugin code importing `openclaw` /
# `openclaw/plugin-sdk/*` throws ERR_MODULE_NOT_FOUND. A plugin's main entry
# doesn't import it eagerly (runtime plugin load succeeds), but its doctor
# contribution does — e.g. the discord plugin's security-doctor imports
# `openclaw/plugin-sdk/channel-policy` — which crashed the boot-time
# `openclaw doctor --fix` (startupCommand Step C3) before it could migrate legacy
# per-workspace state, leaving every agent failing the un-migrated-state gate.
# Symlink the `openclaw` package root (/app) into each baked plugin's node_modules
# so the peer resolves to /app/package.json's exports. Root-owned to match the
# plugin dirs (the loader rejects non-root plugin trees).
RUN set -eux; \
    for d in /app/dist/extensions/*/; do \
      [ -d "$d" ] || continue; \
      mkdir -p "${d}node_modules"; \
      ln -sfn /app "${d}node_modules/openclaw"; \
      chown -h root:root "${d}node_modules/openclaw"; \
    done


# ---- Layer 18: pre-populated V8 compile cache ----
# The fork enables Node's compile cache on every CLI start
# (src/entry.compile-cache.ts) but defaults its base to os.tmpdir() —
# container-ephemeral, so every cold start recompiles the whole import graph
# (doctor pays it first, then the gateway process pays it again). The cache
# subdirectory key is <base>/openclaw/<pkg version>/<package.json mtimeMs-size>
# — all three components frozen at image build — so a cache populated HERE
# lands at the exact path both boot-time processes resolve at runtime. The
# warm run is a throwaway-state doctor pass (imports the state-migration,
# config-snapshot, and plugin-plan graphs); runtime processes add whatever it
# missed. Must run AFTER the last /app mutation (Layer 17) so compiled
# modules match what ships.
ENV NODE_COMPILE_CACHE=/var/tmp/openclaw-compile-cache
# The warm pass runs the real `doctor` so the cache covers what the boot
# chain actually pays: the state-migration, config-snapshot and plugin-plan
# graphs, not just the CLI entry. It briefly holds a ~760MB heap. That was
# survivable on a dedicated runner but killed buildkit when two builds
# collided on the shared self-hosted VM (2026-08-12 03:55); CI now runs on
# hosted runners with an isolated VM per job, and the concurrency group below
# keeps two OpenClaw builds from overlapping, so the peak has room. Bounded
# by a 120s timeout and `|| true` — a cold cache is a slow boot, never a
# failed build. Node validates entries per file, so a partial cache is a
# partial win, never a wrong load.
RUN set -eux; \
    mkdir -p /var/tmp/openclaw-compile-cache; \
    OPENCLAW_STATE_DIR=/tmp/compile-warm HOME=/tmp/compile-warm-home \
      timeout 120 node /app/openclaw.mjs doctor --non-interactive < /dev/null > /dev/null 2>&1 || true; \
    rm -rf /tmp/compile-warm /tmp/compile-warm-home; \
    chown -R node:node /var/tmp/openclaw-compile-cache

# ---- Litestream + local-SQLite boot chain ----
# Every environment runs this chain — the per-env storage gate was removed once
# dev proved the layout, so any tag pinned in openclaw-version.json MUST be
# built from a commit containing apps/infra/openclaw/local-sqlite/. Version pinned
# with the checksum from the release's published checksums.txt; v0.5.15 is
# the stabilization release of the LTX line (silent-replication fix #1037
# landed in v0.5.9, ECS container-credential fix in v0.5.11).
ARG LITESTREAM_VERSION=0.5.15
ARG LITESTREAM_SHA256=839a68e69d111409262775bed78ba9f73f0835cfd23d8c128da21eedaedb50ba
RUN set -eux; \
    curl -fsSL -o /tmp/litestream.tgz \
      "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-x86_64.tar.gz"; \
    echo "${LITESTREAM_SHA256}  /tmp/litestream.tgz" | sha256sum -c -; \
    tar -xzf /tmp/litestream.tgz -C /usr/local/bin litestream; \
    rm /tmp/litestream.tgz; \
    litestream version
COPY local-sqlite/isol8-local-sqlite-boot.sh /usr/local/bin/isol8-local-sqlite-boot.sh
COPY local-sqlite/isol8-local-sqlite-app.sh /usr/local/bin/isol8-local-sqlite-app.sh
COPY local-sqlite/isol8-litestream.yml /etc/isol8-litestream.yml
RUN chmod 0755 /usr/local/bin/isol8-local-sqlite-boot.sh /usr/local/bin/isol8-local-sqlite-app.sh

# ---- Restore runtime context ----
USER node
WORKDIR /home/node
