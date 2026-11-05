# The production Hermes-on-AgentCore image. Pinned base, no diagnostic hatch, no :latest.
# Dockerfile.spike stays for local iteration; this is the built-and-pushed artifact
# (.github/workflows/build-hermes-image.yml). Runtime contents mirror the spike exactly —
# only the ordering differs, to do all root-owned setup (skill binaries, managed overlay)
# before the single USER switch to the unprivileged runtime uid AgentCore expects.
#
# M4-R31: nousresearch/hermes-agent publishes no numbered tags upstream (only date-stamped
# `vYYYY.M.D` tags + `latest`/`main`), so a `:0.21.3` tag pin (the brief's placeholder) does
# not exist and would 404. Pin by DIGEST instead, to the exact image already proven end to
# end in dev: digest sha256:a5b2eac62d3f97686e027eedd837d38bc5cc7d941f3107bebefdcef38126d103,
# reporting version 0.21.3 (`importlib.metadata.version('hermes-agent')`), pulled as `latest`
# on 2026-09-16T20:56Z. Re-derive the digest with:
#   docker image inspect nousresearch/hermes-agent:latest --format '{{join .RepoDigests ","}}'
# Mirrored in hermes-version.json#base (the promotion record; this line is what actually builds).

# ---- gog (Google CLI) ------------------------------------------------------------------------
# The one connector binary with no distro package and no npm package. `go install` verifies the
# module against sum.golang.org, so the BYTES are pinned even though the tag is not.
#
# --platform=$BUILDPLATFORM + GOARCH=arm64 cross-compiles on the amd64 builder instead of running
# a whole Go toolchain under the QEMU emulation this image's arm64 build uses. golang:1.27.1
# because gogcli's go.mod declares `toolchain go1.27.1`. GOBIN is NOT honoured for a cross build --
# `go install` refuses GOBIN when GOOS/GOARCH differ from the builder and drops the binary under
# $GOPATH/bin/<GOOS>_<GOARCH>/ instead, so we copy it out explicitly (also covers a native-arm64
# builder, where it lands straight in $GOPATH/bin/).
FROM --platform=$BUILDPLATFORM golang:1.27.1-trixie AS gocli
ENV CGO_ENABLED=0 GOOS=linux GOARCH=arm64
RUN go install github.com/openclaw/gogcli/cmd/gog@v0.39.1 \
 && mkdir -p /out/bin \
 && cp "$(ls /go/bin/linux_arm64/gog 2>/dev/null || ls /go/bin/gog)" /out/bin/gog \
 && test -x /out/bin/gog

FROM nousresearch/hermes-agent@sha256:a5b2eac62d3f97686e027eedd837d38bc5cc7d941f3107bebefdcef38126d103

# Import smoke: the base's own SDK set (anthropic included) must still load Hermes.
RUN python3 -c "from run_agent import AIAgent; print('hermes imports ok')"

# Linux skill binaries the bundled skills shell out to.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl jq ripgrep unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ---- Lightbulb patch series over the pinned upstream source ------------------------------
# hermes-patches/ is the `git format-patch --full-index` export of the private fork
# TheLightbulbCompany/hermes-agent-lightbulb (its LIGHTBULB.md has the series table and the
# upstream sync policy). SERIES names the pin this base image was built from
# (hermes-version.json#base.revision), the fork commit, then the patches in order. Every patch
# is inert until the bridge installs a checkpoint callback, injects an input or sets a refresh
# delegate. Applied ONE AT A TIME in SERIES order: a later patch can edit a file an earlier one
# creates, so a single `git apply --check *.patch` (each checked against the untouched tree)
# fails. --check first, so a series that no longer fits the base fails the build HERE, not at
# runtime; the smoke then proves every patch is present and inert. Separate commands, not
# `--check ... && apply`: under `set -e` a failure on the left of && does not stop the loop.
COPY hermes-patches/ /tmp/hermes-patches/
COPY tests/image_smoke/patched_hermes_smoke.py /tmp/patched_hermes_smoke.py
RUN set -eu; cd /opt/hermes; \
    for p in $(grep -E '^[0-9]{4}-.*\.patch$' /tmp/hermes-patches/SERIES); do \
      git apply --check "/tmp/hermes-patches/$p"; \
      git apply "/tmp/hermes-patches/$p"; \
    done; \
    HERMES_HOME=/tmp/patched-smoke python3 /tmp/patched_hermes_smoke.py; \
    rm -rf /tmp/hermes-patches /tmp/patched_hermes_smoke.py /tmp/patched-smoke

# ---- QuickBooks MCP server -------------------------------------------------
# Intuit publishes this server only as source. Keep the same pinned commit and
# dev-dependency build fix used by the retired OpenClaw image; the runtime image
# carries only the compiled server after pruning build tools.
#
# quickbooks-mcp/ is our overlay: the pinned server (and upstream HEAD as of
# 2026-09-15) has create/read/update/delete/search/pdf for invoices but no SEND,
# so an agent could draft an invoice and never email it (Anna's Frozen Treats,
# 2026-10-06). send_invoice wraps node-quickbooks's sendInvoicePdf
# (POST /v3/company/{realmId}/invoice/{id}/send, accounting scope). The two
# files are copied into the checkout and registered in index.ts before the
# build; the grep pins exactly one import + one RegisterTool, so a drifted
# index.ts fails the build instead of silently dropping the tool. Upstream PR
# intuit/quickbooks-online-mcp-server#151 (open since 2026-09-30, unreviewed)
# adds the same send_invoice with the same params and guards; once a pin
# carries it, the count check fails the build and this overlay gets deleted.
#
# brokered-token.patch (spec 13 T8, lane V15) teaches the client a BROKERED store: when
# QUICKBOOKS_ACCESS_TOKEN is in the store (the bridge writes the backend-vended token there each
# turn, no refresh token, no client secret) the server seeds from it, re-reads the store on every
# call and adopts a rewritten token, and never calls Intuit, never starts the OAuth flow, never
# writes the store. Inert for today's stores (no such key). `git apply --check` is its own
# command (same reason as the Hermes series above); the smoke after the build proves the
# behaviour with every HTTP primitive replaced by a throw.
ENV QUICKBOOKS_MCP_COMMIT=c351dc011d9cb14b211857457085f7994d8b1e15
COPY quickbooks-mcp/ /opt/quickbooks-mcp-overlay/
COPY tests/image_smoke/quickbooks_brokered_smoke.mjs /tmp/quickbooks_brokered_smoke.mjs
RUN set -eux; \
    mkdir -p /opt/quickbooks-mcp; \
    cd /opt/quickbooks-mcp; \
    git init -q .; \
    git remote add origin https://github.com/intuit/quickbooks-online-mcp-server.git; \
    git fetch -q --depth 1 origin "$QUICKBOOKS_MCP_COMMIT"; \
    git checkout -q FETCH_HEAD; \
    rm -rf .git; \
    cp /opt/quickbooks-mcp-overlay/send-invoice.tool.ts src/tools/; \
    cp /opt/quickbooks-mcp-overlay/send-quickbooks-invoice.handler.ts src/handlers/; \
    git apply --check /opt/quickbooks-mcp-overlay/brokered-token.patch; \
    git apply /opt/quickbooks-mcp-overlay/brokered-token.patch; \
    grep -q 'brokeredAccessToken: brokered_access_token' src/clients/quickbooks-client.ts; \
    rm -rf /opt/quickbooks-mcp-overlay; \
    perl -pi -e 's|^(import \{ GetInvoicePdfTool \} from "./tools/get-invoice-pdf.tool.js";)$|$1\nimport { SendInvoiceTool } from "./tools/send-invoice.tool.js";|' src/index.ts; \
    perl -pi -e 's|^(\s*)(RegisterTool\(server, GetInvoicePdfTool\);)$|$1$2\n$1RegisterTool(server, SendInvoiceTool);|' src/index.ts; \
    test "$(grep -c SendInvoiceTool src/index.ts)" = 2; \
    perl -pi -e 's|^(\s*)("create_": CRUD_CATEGORY\.WRITE,)$|$1$2\n$1"send_": CRUD_CATEGORY.WRITE,|' src/helpers/register-tool.ts; \
    grep -q '"send_": CRUD_CATEGORY.WRITE,' src/helpers/register-tool.ts; \
    npm ci --include=dev --ignore-scripts; \
    npm run build; \
    npm prune --omit=dev; \
    test -f /opt/quickbooks-mcp/dist/index.js; \
    node /tmp/quickbooks_brokered_smoke.mjs /opt/quickbooks-mcp/dist; \
    rm -f /tmp/quickbooks_brokered_smoke.mjs; \
    grep -q '"send_invoice"' /opt/quickbooks-mcp/dist/tools/send-invoice.tool.js; \
    grep -q 'RegisterTool(server, SendInvoiceTool)' /opt/quickbooks-mcp/dist/index.js; \
    printf '%s\n' '#!/bin/sh' 'set -eu' 'export QUICKBOOKS_TOKEN_STORE_PATH="${HOME}/quickbooks/.env"' 'exec node /opt/quickbooks-mcp/dist/index.js' > /usr/local/bin/lightbulb-quickbooks-mcp; \
    chmod 0755 /usr/local/bin/lightbulb-quickbooks-mcp; \
    sh -n /usr/local/bin/lightbulb-quickbooks-mcp; \
    chown -R root:root /opt/quickbooks-mcp /usr/local/bin/lightbulb-quickbooks-mcp

# ---- network engine (the `lightbulb member` sidecar, bridge/network.py) ----------------------
# The engine (TheLightbulbCompany/cli): its static musl release asset, pinned by tag and checksum.
# TODO: the engine release containing network workers (cli PR #105) must replace this pin before
# LIGHTBULB_NETWORK_WORKERS can be turned on.
# v0.7.0 or later: the backend's memory sync (memory.list/import/remember/forget) are engine ops.
# The checksum is v0.9.13's lightbulb-linux-arm64.tar.gz.sha256 asset; bump the tag and the checksum together.
RUN curl -fsSLo /tmp/lb.tgz https://github.com/TheLightbulbCompany/cli/releases/download/v0.9.13/lightbulb-linux-arm64.tar.gz \
 && echo "a895ef798f5e97ca4aeae3778e070a31ac419431476bd2523a5e274c7917d959  /tmp/lb.tgz" | sha256sum -c - \
 && tar -xzf /tmp/lb.tgz -C /usr/local/bin lightbulb && rm /tmp/lb.tgz && lightbulb version

# ---- CLI connector binaries ------------------------------------------------------------------
# The binaries behind the five CLI-materializer connectors whose credentials the backend projects
# into every profile (core/services/credentials/hermes_cli.py). Instagram's "CLI" is curl + jq,
# already installed above. This image is built linux/arm64 only (AgentCore runtimes are arm64),
# so every recipe names the arm64 artefact. Each layer keeps its own smoke test, so a binary
# that did not materialise fails the build instead of a turn.
COPY --from=gocli /out/bin/gog /usr/local/bin/

# AWS CLI v2 -- the official bundled installer (apt and pip ship only v1); aarch64 asset.
RUN curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64-2.17.0.zip" -o /tmp/awscliv2.zip \
 && unzip -q /tmp/awscliv2.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/aws /tmp/awscliv2.zip \
 && aws --version

# GitHub CLI -- the vendor apt repo (a release-tarball pipe breaks on the release-assets redirect).
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=arm64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update -qq && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/* \
 && gh --version && gog --version

# xurl (X) -- the npm package. Its native binary arrives in the npm POSTINSTALL, so this must stay outside any --ignore-scripts install; the
# smoke test is what fails the build if the binary did not materialise for this arch. `node` and
# `npm` are both in the Hermes base image already.
RUN npm i -g --allow-scripts=@xdevplatform/xurl @xdevplatform/xurl@1.2.3 && xurl --help >/dev/null

# Microsoft 365 CLI -- device-code/browser login persists its own MSAL and token files
# in the profile home, so the agent can use the authenticated CLI without a backend-held
# refresh grant. Pin the public release; the upstream docs recommend Node 20+.
RUN npm i -g @pnp/cli-microsoft365@11.11.0 && m365 version

# agent-browser is Hermes's browser tool. It connects to the workspace's shared AgentCore Browser
# session over CDP (the backend starts the session and signs the address; the bridge serves it on
# loopback, see browser_cli.py below), so its own AgentCore provider is not used. Global install so
# the native arm64 binary is on PATH with no npx/Node at runtime; the release asset arrives in the
# npm POSTINSTALL, so this must stay outside any --ignore-scripts install.
RUN npm i -g --allow-scripts=agent-browser agent-browser@0.38.1 && agent-browser --version

# OpenCLI: the front desk's site adapters (design 2026-10-16). Node >= 20.18.1 is its floor; the
# `opencli --version` smoke is what fails the build when the base's Node cannot run it.
# Adapters go to ~/.opencli; the shell's HOME is <profile>/home (TERMINAL_HOME_MODE=profile), so
# they live in the profile and are saved with it.
RUN npm i -g @jackwener/opencli@2.0.0 && opencli --version

# pnpm for `lightbulb app` (the starter and its checks run pnpm). Without it in the image, each
# agent installed its own into the profile home, and on Demo Lab that copy rotted into
# "Cannot find module '../dist/pnpm.cjs'" mid-build (e2e, 30 Oct 2026). Pinned like the rest.
RUN npm i -g pnpm@10.34.9 && pnpm --version

# Hermes advertises browser_* only when _chromium_installed() is true (browser_tool_install.py:227-242):
# AGENT_BROWSER_EXECUTABLE_PATH is an existing file (a bare os.path.isfile), OR chrome/chromium is on
# PATH, OR a Playwright build is cached. The upstream base BUNDLES one -- a 269 MB
# chromium_headless_shell-1243 under PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright -- so without this
# rm the gate passes on its own and a workspace with NO browser profile (hence no loopback CDP address)
# would browse with a LOCAL headless Chromium in the microVM: no live view, microVM CPU and storage, and
# a silent departure from "browser means AgentCore Browser". Ruling R13: delete it, so the zero-byte
# sentinel below is the ONLY way the gate passes and "no profile => no browser tools" holds in the image.
# Verified 2026-09-19: nothing else in the image needs the bundle. The `playwright` PYTHON package
# itself is not even installed in the base image (checked live), so every other mention of it --
# hermes_cli/doctor_live.py's live-browser probe (opt-in `hermes doctor --live`, never called by our
# bridge), the google_meet plugin's Playwright-driven meet bot, the har-derived-api-client optional
# skill -- already fails soft on a missing import today, independent of this directory. Only
# tools/browser_tool_install.py's own directory scan (and agent-browser's own LOCAL-mode discovery,
# which R13 exists to make unreachable) ever reads it.
RUN test -d /opt/hermes/.playwright && rm -rf /opt/hermes/.playwright \
 && test ! -d /opt/hermes/.playwright

# The sentinel. A CDP connection never launches a local engine, so it is never exec'd -- and if that
# path ever fails, exec'ing a zero-byte file errors loudly with this path in the message.
RUN mkdir -p /opt/lightbulb && : > /opt/lightbulb/no-local-browser && test -f /opt/lightbulb/no-local-browser

# The gate, both ways, in TWO processes: _chromium_installed() memoizes per process, so a single-process
# before/after proves nothing -- it would just return the first call's cached answer. This fails the
# build if the bundle above ever comes back (e.g. a base image bump) or the sentinel above goes missing.
RUN python3 -c "import sys; sys.path.insert(0,'/opt/hermes'); \
from tools.browser_tool_install import _chromium_installed; assert not _chromium_installed()" \
 && AGENT_BROWSER_EXECUTABLE_PATH=/opt/lightbulb/no-local-browser python3 -c "import sys, os; \
sys.path.insert(0,'/opt/hermes'); from tools.browser_tool_install import _chromium_installed; \
assert _chromium_installed()"

# computer_use on the shared browser's screen: Hermes's own tool, with our driver in place of the
# desktop cua-driver (the bridge points HERMES_CUA_DRIVER_CMD here when the workspace has a browser).
COPY --chmod=755 cua_driver.py /opt/lightbulb/bin/lightbulb-cua
RUN /opt/lightbulb/bin/lightbulb-cua manifest | jq -e '.binary_version == "0.20.0"' >/dev/null

# computer_use lazily "installs" mcp/httpx2/starlette at exact pins before its first call. The base
# already ships all three, but httpx2 2.12.0 (what mcp and anthropic resolve to) against the lazy
# pin 2.7.0, so every call would try a pip downgrade as uid 1000 and fail. Pin the lazy list to the
# installed httpx2 instead, and prove nothing is left to install.
RUN v=$(python3 -c "import importlib.metadata as m; print(m.version('httpx2'))") \
 && sed -i "s/\"httpx2==2\.7\.0\"/\"httpx2==$v\"/g" /opt/hermes/tools/lazy_deps.py \
 && python3 -c "import sys; sys.path.insert(0,'/opt/hermes'); from tools.lazy_deps import feature_missing; \
assert feature_missing('tool.computer_use') == (), feature_missing('tool.computer_use')"

COPY bridge /opt/bridge/bridge
COPY plugins/ /opt/hermes/plugins/
# COPY merges into the base's plugins/ tree (it does not replace it), so upstream's
# plugins/memory/__init__.py discovers plugins/memory/agentcore/ as a BUNDLED provider
# (module plugins.memory.agentcore). Prove it loads; HERMES_HOME points at scratch because
# the loader creates a home, and the rm keeps that out of the layer.
RUN HERMES_HOME=/tmp/mp-smoke python3 -c "import sys; sys.path.insert(0,'/opt/hermes'); \
from plugins.memory import load_memory_provider; p = load_memory_provider('agentcore', register_skills=False); \
assert p is not None and p.name == 'agentcore' and [t['name'] for t in p.get_tool_schemas()] == ['remember', 'workspace_memory_search'], p" \
 && rm -rf /tmp/mp-smoke && test ! -d /tmp/mp-smoke
# Bundled agent-facing skills. NOT checked in: `skills/` is GENERATED into this build
# context by ../build-skills.sh (run by build-hermes-image.yml before `docker build`) from
# skills-src/, with tool names swapped for Hermes/our plugin's. If this
# COPY fails, that script has not been run. Path matches config.yaml's
# skills.external_dirs (Task 7).
COPY skills/ /opt/hermes-skills/

# Managed overlay (Task 11) — non-negotiables an agent's own profile config cannot undo.
COPY managed/config.yaml /etc/hermes/config.yaml
RUN chown root:root /etc/hermes/config.yaml && chmod 0444 /etc/hermes/config.yaml \
 && mkdir -p /opt/data && chown -R 1000:1000 /opt/data /opt/bridge /opt/hermes-skills

# AgentCore runs the container as uid 1000; Hermes state lives under HERMES_HOME. One
# switch, after every root-owned write above — nothing here needs root again.
USER 1000
WORKDIR /opt/bridge
ENV HERMES_HOME=/opt/data HERMES_HEADLESS=1 PYTHONUNBUFFERED=1 AGENTCORE_PORT=8080
EXPOSE 8080
ENTRYPOINT []
CMD ["python3", "-m", "bridge.server"]
