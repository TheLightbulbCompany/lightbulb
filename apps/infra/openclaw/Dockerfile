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
FROM ghcr.io/openclaw/openclaw:2026.5.7

USER root

# ---- Layer 1: runtime apt packages ----
# Dropped from upstream extended image: build-essential, make (build-only).
# Keeps git/curl/wget/gnupg — runtime tools (skills shell out, clawhub uses git).
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      ffmpeg jq ripgrep tmux poppler-utils \
      socat python3-pip sqlite3 python3 \
      curl wget ca-certificates gnupg git \
    && rm -rf /var/lib/apt/lists/*

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

# ---- Layer 4: uv (Python package manager) ----
RUN curl -sSL https://astral.sh/uv/install.sh | HOME=/tmp sh && \
    install -m 755 /tmp/.local/bin/uv /usr/local/bin/uv

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
RUN npm i -g --ignore-scripts \
      mcporter clawhub openai \
      @steipete/oracle @steipete/summarize \
      @anthropic-ai/claude-code @google/gemini-cli \
      @xdevplatform/xurl

# ---- Layer 9: Playwright Chromium + Chromium system libs + /usr/bin symlink ----
# Upstream openclaw:2026.5.7 ships the playwright npm package (at
# /app/node_modules/playwright-core, v1.59.1) but NOT the Chromium binary
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

# ---- Restore runtime context ----
USER node
WORKDIR /home/node
