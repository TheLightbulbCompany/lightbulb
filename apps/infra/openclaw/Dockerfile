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
FROM ghcr.io/thelightbulbcompany/agent:2026.8.1-isol8.2
# PLUGIN-PIN POLICY (read before bumping the @openclaw/* pins below): the baked
# plugins pin to the latest PUBLISHED plugin dist of the FROM tag's release
# line on npm. The FROM tag is on the 2026.8.1 line, which upstream cut as a
# STABLE release on 2026-08-30 — so all eight baked plugins pin to the stable
# 2026.8.1 dist (previously the 2026.8.1-beta.3 prerelease) to stay in lockstep
# with the 8.1 host; promoting this base to dev and then prod is the separate
# dev.tag/prod.tag flip in openclaw-version.json. Keep these
# pins in lockstep with the FROM tag on every base bump: the 7.1 line moved the
# channel inbound-dispatch contract (turn-adoption ack), so a stale channel
# plugin (slack/discord) on a newer host is not a safe skew. The dev canary boots
# the built image to confirm every baked plugin converges on the host.

USER root

# ---- Layer 1: runtime apt packages ----
# Dropped from upstream extended image: build-essential, make (build-only).
# Keeps git/curl/wget/gnupg — runtime tools (skills shell out, clawhub uses git).
#
# The VNC/display packages back the container browser live view (the workspace
# Browser panel). Two generations coexist here so one image tag serves both
# boot commands during the transition — the same decoupling that let the
# original xvfb layer ship ahead of its DISPLAY flip:
#
#   current: tigervnc (Xvnc = X server + VNC server in ONE resident process,
#            with native client-driven SetDesktopSize resize — the thing
#            x11vnc cannot do, and what lets the dock panel resize the actual
#            display) + matchbox (kiosk window manager: every window
#            borderless-fullscreen, re-fit on display resize — without it
#            Chrome floats at its default size on a black root window)
#   legacy:  xvfb + x11vnc (+ socat spawn-per-connection), kept until no
#            pinned tag's boot command references them, then removable
#
# Upstream's sandbox recipe (scripts/docker/sandbox/Dockerfile.browser) uses
# the xvfb+x11vnc generation; we do NOT use their sandbox feature — it spawns
# a sibling container via `docker run`, and Fargate has no Docker daemon. We
# run the processes in this one container, which also drops their CDP relay
# (it exists only to cross a container boundary ours does not have).
#
# Debian ships TigerVNC's server binary as Xtigervnc; the boot command guards
# on and launches /usr/bin/Xvnc, so pin that name to a real path via the
# update-alternatives symlink the package installs (asserted by the ln -sf
# below in case the alternatives link ever changes shape).
#
# All of this is inert until DISPLAY is set on the task definition; see the
# display block in container-stack.ts startupCommand.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      ffmpeg jq ripgrep tmux poppler-utils \
      socat python3-pip sqlite3 python3 \
      curl wget ca-certificates gnupg git unzip \
      xvfb x11vnc novnc websockify \
      tigervnc-standalone-server matchbox-window-manager \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/Xtigervnc /usr/bin/Xvnc \
    # ln -sf happily creates a DANGLING link if the package ever renames its
    # binary; test -x follows the link, turning that into a failed image build
    # here instead of a fleet-wide boot FATAL at the next deploy.
    && test -x /usr/bin/Xvnc

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
#
# openai-whisper was REMOVED (2026-08-17). It pulled the PyPI linux-x86_64
# torch wheel, which drags the whole NVIDIA CUDA runtime -- torch, triton,
# cudnn, cublas, nccl, cusparse, cusolver, cufft, nvrtc, ~3.4 GB of wheels
# compressing to a 2.87 GB layer (37% of the whole image) -- onto a CPU-only
# Fargate box. Nothing in the fleet could reach it: write_openclaw_config
# emits tools.media.audio.enabled=false for EVERY owner, so the
# media-understanding path that would invoke local STT is off fleet-wide.
# The only other consumer is OpenClaw's OPTIONAL bundled `openai-whisper`
# skill (requires.bins ["whisper"], brew install path), which an agent would
# have to invoke by hand and which is useless on 1 vCPU without a GPU.
# Removing it does NOT speed up the image pull -- SOCI lazy-loads layers
# >=10 MiB, so those bytes were never in the pull window. What it buys is
# ephemeral-storage headroom (Fargate's 20 GiB ceiling holds the compressed
# AND uncompressed image; this file targets 2-3 GB and had reached 7.79 GB)
# and less background decompression competing with the boot chain on one
# vCPU -- the same CPU contention that inflates `doctor --fix` from ~43s
# standalone to ~4 min during boot. It also drops doctor's local-STT probe.
# If audio understanding is ever enabled, do NOT restore this line as-is:
# install torch from the CPU wheel index
# (--index-url https://download.pytorch.org/whl/cpu) and keep CUDA out.
RUN pip install --no-cache-dir --break-system-packages \
      websockets nano-pdf

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
# Globals install with --ignore-scripts (faster, no build deps) EXCEPT the three
# below that fetch a native binary in postinstall (claude-code, xurl, opencode).
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
      @google/gemini-cli \
      mcporter@0.12.0 \
      @paperclipai/mcp-server@2026.529.0

# Claude Code: the ACP coding harness for bedrock_claude owners (see backend
# _build_bedrock_acp_harness_env). Its platform binary arrives via a postinstall
# hook, so like xurl and opencode below it stays outside the --ignore-scripts
# install above — with scripts skipped the `claude` wrapper is present but every
# invocation dies with "claude native binary not installed", so the harness could
# never launch. The smoke test fails the build if the binary didn't materialize.
# 2026.8.1 BASE / npm allowScripts: the base image now runs
# `npm install --global npm@latest`, and the newer npm BLOCKS install scripts by
# default unless the package is named in --allow-scripts:
#
#   npm warn install-scripts 1 package had install scripts blocked because they
#   are not covered by allowScripts: <pkg> (postinstall: ...)
#
# The three layers below deliberately skip the --ignore-scripts global install
# above precisely because they need their postinstall to fetch a platform-native
# binary. Without --allow-scripts the JS wrapper installs and every invocation
# then dies ("claude native binary not installed", "xurl binary not found").
# Each layer keeps its own smoke test, which is what fails the build if the
# binary did not materialise -- this cannot regress silently.
RUN npm i -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code \
    && claude --version >/dev/null

# xurl downloads its native Go binary in npm postinstall. Keep it outside the
# --ignore-scripts global install above; otherwise the JS wrapper is present but
# every `xurl` invocation fails with "xurl binary not found".
RUN npm i -g --allow-scripts=@xdevplatform/xurl @xdevplatform/xurl@1.2.3 && xurl --help >/dev/null

# OpenCode: the ACP coding harness for chatgpt_oauth owners (registered via
# the acpx agents map in openclaw.json — see backend build_acp_config). Its
# platform binary arrives via optionalDependencies + postinstall, so like xurl
# it stays outside the --ignore-scripts install above. Version-pinned; the
# smoke test fails the build if the binary didn't materialize for this arch.
RUN npm i -g --allow-scripts=opencode-ai opencode-ai@1.18.15 && opencode --version >/dev/null

# Durable lobster pipeline state. Lobster's native `state.get`/`state.set` verbs are a
# real, gateway-free file-KV, but they default to $HOME/.lobster/state — on the ephemeral
# container FS, wiped on every task restart. Point them at the per-user EFS mount instead,
# so a workflow's queues/counters/cursors survive restarts and are shared with the agent
# loop (same uid, same EFS). This is why no custom KV tool is needed: the engine already
# ships durable state; it just needs to be aimed at EFS. (`fast-io` is fictional — it's the
# Fast.io SaaS, never wired into mcp.servers — so workflows use `state.*`, not `fast-io`.)
ENV LOBSTER_STATE_DIR=/home/node/.openclaw/lobster-state

# ---- Layer 9: (REMOVED) Playwright Chrome-for-Testing binary ----
# History, because this WILL come up again: PR #596 baked Playwright's
# Chrome-for-Testing here when the image had NO browser at all (upstream
# gates its install behind OPENCLAW_INSTALL_BROWSER, empty by default) --
# one install then served BOTH the browser tool and Playwright-based MCPs,
# and pre-baking closed a ~6-minute first-use download (#443). Real Chrome
# (layer 9b) now serves the browser tool, whose Playwright usage is
# connectOverCDP ONLY -- a library call against the already-running Chrome
# that never needs the CfT binary (playwright-core the LIBRARY still ships
# in /app; only the ~170MB browser download is gone).
#
# What this costs, knowingly accepted (2026-08-17): an owner who connects a
# Playwright-based MCP (none ship by default; marketplace hires can carry
# them, e.g. Resy-style) gets Playwright's own runtime download at first
# use, re-fetched per task restart since ~/.cache is ephemeral. If such
# MCPs become common, re-add the bake rather than debugging slow first-runs.
#
# The GUI libs the old --with-deps installed now arrive as the Chrome deb's
# dependencies; the FONTS it installed do NOT (the deb only Recommends
# them and layer 9b installs with --no-install-recommends), so they are
# installed explicitly in 9b below -- without them Chrome renders tofu.

# ---- Layer 9b: real Google Chrome for the browser tool (audience #2) ----
# Chrome for Testing self-labels with a "only for automated testing —
# Download Chrome" banner on every fresh profile, which the Browser live-view
# panel puts in front of the OWNER on every session (the profile lives on
# /tmp and is wiped each task restart, so the banner never goes away). Real
# Chrome has no such banner.
#
# This ADDS Chrome for audience #2 (OpenClaw's browser tool, launched via
# executablePath in the generated openclaw.json); it must never REPLACE the
# Playwright install above — audience #1 (Playwright-based MCPs) hardcodes
# the ms-playwright cache path and ignores system Chrome by design. Both
# browsers coexist; the GUI libs are shared (--with-deps above installed
# them system-wide).
#
# Inert until the config generator's executablePath flips to
# /usr/bin/google-chrome (config.py): the browser tool's resolver would
# prefer google-chrome over chromium on its own, but the explicit pin wins,
# so shipping this image changes nothing until the config PR lands — same
# image-first decoupling as every other layer here.
#
# dl.google.com serves only the CURRENT stable deb (no version pinning
# available); the image tag pins what a rebuild picked up, same posture as
# the apt-installed packages above. amd64-only, matching the AWS CLI layer.
# NO /usr/bin/chromium compat symlink, by explicit owner decision
# (2026-08-17): configs that still pin the old path are fixed for real via
# the admin fleet config patch BEFORE any container rolls onto this image.
# ORDERING IS LOAD-BEARING: rolling a container here with an unpatched
# config leaves executablePath pointing at a binary this image does not
# have, and the browser tool dies at launch.
#
# MESA IS LOAD-BEARING, not an optimisation. Chrome 137 removed the automatic
# SwiftShader fallback for WebGL (blink-dev "Intent to Remove: SwiftShader
# Fallback"); prod runs Chrome 151. Fargate has no GPU and this image shipped
# no GL driver at all, so `canvas.getContext("webgl")` returned NULL — a state
# essentially no real desktop is in, and one that silently breaks any site
# rendering through WebGL. Arkose/FunCaptcha (LinkedIn's sign-in challenge)
# draws its puzzle in 3D, so it cannot render at all without a context.
#
# libgl1-mesa-dri supplies the llvmpipe software rasteriser and libglx-mesa0
# the GLX entry points ANGLE resolves against; paired with
# `--use-gl=angle --use-angle=gl` in the generated openclaw.json, the unmasked
# WEBGL_debug_renderer_info string becomes an ordinary Mesa/llvmpipe value —
# what any real driverless Linux VM reports — rather than a spoofed one.
# llvmpipe is also faster than SwiftShader (LLVM JIT, multithreaded).
#
# If chrome://gpu ever shows GL initialisation failing, the zero-package
# fallback is `--enable-unsafe-swiftshader` (ANGLE ships SwiftShader inside
# Chrome), at the cost of a rarer renderer string. Do not simply drop the
# packages and flags: that returns the fleet to no-WebGL-at-all.
RUN curl -fsSL -o /tmp/google-chrome.deb \
      "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" && \
    apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
      /tmp/google-chrome.deb \
      libgl1-mesa-dri libglx-mesa0 \
      fonts-liberation fonts-noto-cjk fonts-noto-color-emoji && \
    rm -f /tmp/google-chrome.deb && rm -rf /var/lib/apt/lists/* && \
    test -x /usr/bin/google-chrome

# ---- Layer 9c: Cloudflare WARP (browser egress) ----
# Routes ONLY the browser tool's Chrome off the AWS NAT IP and onto a
# Cloudflare egress IP, to cut the datacenter-IP CAPTCHA walls. Fargate grants
# no NET_ADMIN and has no /dev/net/tun, so a real tunnel is impossible; the
# official package's "Local proxy" mode needs neither — warp-svc opens a
# SOCKS5/HTTP listener on loopback and speaks MASQUE/QUIC (UDP 443) out to
# 162.159.198.x. Verified against this exact package: warp-svc starts with no
# dbus, no CAP_NET_ADMIN and no tun device.
#
# A loopback proxy affects only processes explicitly pointed at it, which is
# exactly the property we want: the gateway WebSocket, Paperclip, the
# bedrock-gate, Litestream/S3, EFS and every MCP server are untouched. The one
# pointer is Chrome's --proxy-pac-url, emitted by the backend config generator.
#
# INERT until the enrollment secret is set: isol8-warp-boot.sh skips entirely
# (exit 0) when CLOUDFLARE_WARP_ENROLLMENT is absent or is still the CDK
# placeholder, so this layer ships and rolls ahead of any Zero Trust org
# existing — same image-first decoupling as layers 9b and 10 above.
#
# Codename is read from the base rather than hardcoded (unlike the 1Password /
# GitHub repos above, which serve one distro-agnostic "stable" suite):
# Cloudflare publishes per-suite, and this base's Debian release is whatever
# the upstream OpenClaw fork's node image is on. Cloudflare currently publishes
# both bookworm and trixie; a base that moves to a suite they do not publish
# fails the build here, loudly, instead of shipping a container that silently
# never connects.
RUN . /etc/os-release && \
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
      | gpg --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ ${VERSION_CODENAME} main" \
      > /etc/apt/sources.list.d/cloudflare-client.list && \
    apt-get update -qq && apt-get install -y -qq --no-install-recommends cloudflare-warp && \
    rm -rf /var/lib/apt/lists/* && \
    warp-cli --version
COPY warp/isol8-warp-boot.sh /usr/local/bin/isol8-warp-boot.sh
RUN chmod 0755 /usr/local/bin/isol8-warp-boot.sh

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
      npm install --prefix "/opt/stage-$P" "@openclaw/$P@2026.8.1"; \
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
    npm install --prefix /opt/stage-amazon-bedrock "@openclaw/amazon-bedrock-provider@2026.8.1"; \
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
    npm install --prefix /opt/stage-lobster "@openclaw/lobster@2026.8.1"; \
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
    npm install --prefix /opt/stage-parallel "@openclaw/parallel-plugin@2026.8.1"; \
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
# We now pin the stable 2026.8.1 dist explicitly — the same dist every other
# baked plugin pins to on this 8.1 base — so it stays in lockstep with the FROM
# tag rather than tracking whatever `@beta` resolves to on the day of the build;
# re-resolve `npm view @openclaw/firecrawl-plugin@beta version` on each base
# bump. Bake it root-owned, same as the layers above.
RUN set -eux; \
    mkdir -p /opt/stage-firecrawl; \
    npm install --prefix /opt/stage-firecrawl "@openclaw/firecrawl-plugin@2026.8.1"; \
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
# via plugins.entries["lightbulb-marketplace"] in core/containers/config.py.
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
# plugins.entries["lightbulb-marketplace"] in core/containers/config.py.
COPY plugins/lightbulb-marketplace /app/dist/extensions/lightbulb-marketplace
RUN chown -R root:root /app/dist/extensions/lightbulb-marketplace

# ---- Layer 13: externalized ACP runtime backend (acpx) ----
# acpx is the official ACP runtime plugin that lets a native OpenClaw agent
# (e.g. Forge) spawn external coding harnesses — Claude Code, Codex, Gemini —
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
    npm install --prefix /opt/stage-acpx "@openclaw/acpx@2026.8.1"; \
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
    npm install --prefix /opt/stage-tokenjuice "@openclaw/tokenjuice@2026.8.1"; \
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

# ---- Layer 14b: baked paperclip-exec-creds plugin ----
# Same shape and same rationale as the layer above, for a different secret.
# Registers a `resolve_exec_env` hook that injects this AGENT's PAPERCLIP_*
# credentials into its exec'd commands only, read per-agent from
# workspace-<agentId>/paperclip-claimed-api-key.json (the file
# core/services/friday_identity.py seeds).
#
# WHY: the seeded AGENTS.md used to instruct the agent to
# `export PAPERCLIP_API_KEY=$(jq -r .token …)` before every Paperclip call,
# because `export` does not survive between exec calls. The model therefore
# re-materialized the token every turn and printed it into its visible
# reasoning — a long-lived key in the owner's chat transcript, which persists
# to SQLite and replicates to S3. Injecting here means the model never holds
# the token at all, so it cannot echo what it does not have.
COPY plugins/paperclip-exec-creds /app/dist/extensions/paperclip-exec-creds
RUN chown -R root:root /app/dist/extensions/paperclip-exec-creds

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

# ---- Layer 16: bundled Lightbulb platform skill ----
# The `lightbulb` skill is every agent's authoritative reference for the
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
COPY skills/lightbulb /app/skills/lightbulb

# Chrome extensions, baked as Linux "external extensions" (see chrome-extensions/README.md).
# Branded Chrome 137+ ignores --load-extension; the install path Linux Chrome still
# honors is /usr/share/google-chrome/extensions/<ID>.json pointing at a local .crx —
# Chrome installs it into the (ephemeral, /tmp) profile on every launch. No flag, no
# policy, no server. popups-to-tabs folds every new window (window.open OAuth popups)
# into the main window as a tab, so the kiosk WM never hides one window behind another
# in the live view. The JSON filename IS the extension id (derived from the packing key).
COPY chrome-extensions/popups-to-tabs.crx /opt/isol8/chrome-extensions/popups-to-tabs.crx
COPY chrome-extensions/popups-to-tabs.external.json /usr/share/google-chrome/extensions/fdojippeikcjnafolmkpdgfcjlbnalpo.json
RUN chmod 0644 /opt/isol8/chrome-extensions/popups-to-tabs.crx \
      /usr/share/google-chrome/extensions/fdojippeikcjnafolmkpdgfcjlbnalpo.json


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

# ---- /home/node ownership ----
# The gateway runs as uid 1000: container-stack.ts boots as root (it has to —
# the ECS ephemeral volumes materialise root:root) and then hands the gateway
# over with `setpriv --reuid=1000`. So nothing in that user's HOME may ship
# root-owned, or the gateway cannot write it.
#
# Build steps run as root, but with HOME=/root — which is why npm's cache
# lands in /root/.npm and never here. What DOES land root-owned is anything a
# COPY or a base-image step creates under /home/node (the `COPY sage/config.json`
# above is exactly that, hence its own chown, and the base's Playwright branch
# does the same to .cache whenever OPENCLAW_INSTALL_BROWSER is set). Fix those
# in the layer rather than repairing them on every container boot: the boot has
# no idea what the image baked, so the repair there was a blind sweep.
#
# maxdepth 1: the entries are dotfiles and directory inodes, so this is metadata
# only — never a recursive copy-up of a large tree into a new layer.
# .openclaw is excluded on purpose. In the image it is an empty node-owned
# mountpoint, but at runtime it IS the per-user EFS mount, whose ownership comes
# from the access point's uid squash. The image must never have an opinion there.
RUN find /home/node -maxdepth 1 -uid 0 ! -name .openclaw -exec chown -h node:node {} +

# ---- Restore runtime context ----
USER node
WORKDIR /home/node
