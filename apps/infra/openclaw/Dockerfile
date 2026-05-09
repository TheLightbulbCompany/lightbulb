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

# ---- Layer 8c: pre-bake ALL openclaw plugin runtime deps ----
# At first start, openclaw's plugin loader npm-installs each plugin's deps
# into <pluginRoot>/node_modules/ unless a matching version already exists
# there. Detection logic (upstream src/plugins/bundled-runtime-deps.ts):
#   collectRuntimeDeps(packageJson) returns
#       { ...packageJson.dependencies, ...packageJson.optionalDependencies }
#   readInstalledDependencyVersion(<pluginRoot>, depName) reads
#       <pluginRoot>/node_modules/<dep>/package.json and semver-checks
#       against the spec. Match → skip install.
#
# Earlier versions of this layer scanned `package.json#openclaw.bundledRuntimeDeps`,
# but ZERO upstream plugins declare that field — confirmed via
# `find ~/Desktop/openclaw/extensions -name package.json -exec grep -l bundledRuntimeDeps {}`
# returning no matches. The prebake silently no-op'd, leaving deepgram /
# elevenlabs / microsoft TTS plugins (and any other plugin with deps) to
# install at first boot — observed as a 5.5 min `sidecars.plugin-services`
# wedge during cold start (2026-05-01).
#
# Match what openclaw actually checks: scan dependencies + optionalDependencies
# from each plugin's package.json, filter out workspace/file/link specs (npm
# rejects those outside a workspace), and install into the plugin's own
# node_modules so readInstalledDependencyVersion finds the dep first try.
RUN node -e ' \
    const fs=require("fs"),path=require("path"),{execSync}=require("child_process"); \
    const root="/app/dist/extensions"; \
    if (!fs.existsSync(root)) { console.log("[prebake] no extensions dir, skipping"); process.exit(0); } \
    const NON_REGISTRY_PREFIXES=["workspace:","file:","link:","portal:","catalog:"]; \
    for (const name of fs.readdirSync(root)) { \
      const dir=path.join(root,name); \
      const pkgPath=path.join(dir,"package.json"); \
      if (!fs.existsSync(pkgPath)) continue; \
      let pkg; try { pkg=JSON.parse(fs.readFileSync(pkgPath,"utf8")); } catch { continue; } \
      const all={...(pkg.dependencies||{}),...(pkg.optionalDependencies||{})}; \
      const specs=Object.entries(all) \
        .filter(([,v])=>typeof v==="string" && !NON_REGISTRY_PREFIXES.some(p=>v.startsWith(p))) \
        .map(([n,v])=>`${n}@${v}`); \
      if (specs.length===0) continue; \
      console.log(`[prebake] ${name}: ${specs.join(" ")}`); \
      execSync(`npm install --no-save --no-audit --no-fund --no-package-lock --omit=dev ${specs.map(s=>JSON.stringify(s)).join(" ")}`, \
        { cwd: dir, stdio: "inherit" }); \
    }' && \
    chown -R node:node /app/dist/extensions

# ---- Layer 8d: pre-download Playwright Chromium browser ----
# After Layer 8c installs playwright-core, the browser plugin still pays
# ~6 minutes on cold start downloading + extracting Chromium (~150 MB)
# the first time it spins up — observed empirically as the gap between
# `[gateway] starting channels and sidecars...` (T+29s) and
# `[browser/server] Browser control listening` (T+446s).
#
# `playwright-core` is the runtime; the `playwright` package wraps it
# and provides the `playwright install` CLI. We install playwright
# alongside playwright-core into the browser plugin's node_modules,
# then run the CLI with PLAYWRIGHT_BROWSERS_PATH pointing at the
# default cache location so the binary is ready when the plugin starts.
ENV PLAYWRIGHT_BROWSERS_PATH=/home/node/.cache/ms-playwright
RUN cd /app/dist/extensions/browser && \
    npm install --no-save --no-audit --no-fund --no-package-lock --omit=dev playwright@1.59.1 && \
    HOME=/home/node ./node_modules/.bin/playwright install --with-deps chromium && \
    chown -R node:node /app/dist/extensions/browser /home/node/.cache

# ---- Restore runtime context ----
USER node
WORKDIR /home/node
