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

# The bundled Anthropic SDK (0.87) predates AnthropicBedrock's bearer mode (api_key /
# AWS_BEARER_TOKEN_BEDROCK + ANTHROPIC_BEDROCK_BASE_URL), which is how our bedrock-gate is
# reached. The assertion is the gate: a silent downgrade breaks every turn.
RUN uv pip install --python "$(command -v python3)" --upgrade "anthropic>=1.0" \
 && python3 -c "import inspect, anthropic; from anthropic import AnthropicBedrock; assert 'api_key' in inspect.signature(AnthropicBedrock.__init__).parameters, 'no bearer mode'; print('anthropic', anthropic.__version__)" \
 && python3 -c "from run_agent import AIAgent; print('hermes imports ok')"

# Linux skill binaries the bundled skills shell out to.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl jq ripgrep unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

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

# agent-browser drives AgentCore Browser through its OWN merged provider (PR #397); the runtime
# env sets AGENT_BROWSER_PROVIDER=agentcore. Global install so the native arm64 binary is on PATH
# with no npx/Node at runtime; the release asset arrives in the npm POSTINSTALL, so this must stay
# outside any --ignore-scripts install (same pattern as the xurl line above). The second command is
# the one that matters: the provider is feature-gated upstream, and a binary without it fails every
# browser call at runtime instead of failing the build -- an unknown-provider error whose
# "Supported:" list names agentcore is the build-time proof it's compiled in (measured live in the
# dev spike, 2026-09-19: "Unknown provider 'nosuchprovider'. Supported: browserbase, browserless,
# browser-use, kernel, agentcore, or a configured plugin with browser.provider"). The CLI exits
# non-zero on that error, so the check is wrapped with `|| true` and the grep is what actually gates
# the build; AGENT_BROWSER_SOCKET_DIR/HOME give the daemon a writable socket dir to fail inside.
# Review M1 (PR #2792): the failed `open` still forks a DETACHED background daemon that creates a
# live control-plane socket/pid/config under AGENT_BROWSER_SOCKET_DIR -- Docker only commits the
# filesystem, not the process, so the daemon dies with the build container but those files do not.
# `agent-browser close` (same env, so it targets the same socket dir) asks the daemon to shut down
# cleanly first; the `rm -rf` is the real backstop either way, and the `test !` proves the layer
# ships no leftover root-owned debris -- the same standard the .playwright guard above holds itself
# to.
RUN npm i -g --allow-scripts=agent-browser agent-browser@0.38.1 && agent-browser --version \
 && ( AGENT_BROWSER_PROVIDER=nosuchprovider AGENT_BROWSER_SOCKET_DIR=/tmp/ab-smoke HOME=/tmp \
      agent-browser --json open about:blank 2>&1 || true ) | grep -q agentcore \
 && ( AGENT_BROWSER_SOCKET_DIR=/tmp/ab-smoke HOME=/tmp agent-browser close >/dev/null 2>&1 || true ) \
 && rm -rf /tmp/ab-smoke \
 && test ! -d /tmp/ab-smoke

# Hermes advertises browser_* only when _chromium_installed() is true (browser_tool_install.py:227-242):
# AGENT_BROWSER_EXECUTABLE_PATH is an existing file (a bare os.path.isfile), OR chrome/chromium is on
# PATH, OR a Playwright build is cached. The upstream base BUNDLES one -- a 269 MB
# chromium_headless_shell-1243 under PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright -- so without this
# rm the gate passes on its own and a workspace with NO browser profile (hence no AGENT_BROWSER_PROVIDER)
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

# The sentinel. A provider connection never launches a local engine, so it is never exec'd -- and if the
# provider path ever fails, exec'ing a zero-byte file errors loudly with this path in the message.
RUN mkdir -p /opt/lightbulb && : > /opt/lightbulb/no-local-browser && test -f /opt/lightbulb/no-local-browser

# The gate, both ways, in TWO processes: _chromium_installed() memoizes per process, so a single-process
# before/after proves nothing -- it would just return the first call's cached answer. This fails the
# build if the bundle above ever comes back (e.g. a base image bump) or the sentinel above goes missing.
RUN python3 -c "import sys; sys.path.insert(0,'/opt/hermes'); \
from tools.browser_tool_install import _chromium_installed; assert not _chromium_installed()" \
 && AGENT_BROWSER_EXECUTABLE_PATH=/opt/lightbulb/no-local-browser python3 -c "import sys, os; \
sys.path.insert(0,'/opt/hermes'); from tools.browser_tool_install import _chromium_installed; \
assert _chromium_installed()"

# The released CLI rewrites /json/version's WebSocket host to its HTTP discovery
# host. Resolve our loopback metadata in a child launcher so traffic goes to AWS,
# not wss://127.0.0.1:8080. The upstream binary remains unchanged.
COPY --chmod=755 browser_cli.py /opt/lightbulb/bin/agent-browser
RUN test -x /usr/local/bin/agent-browser && /opt/lightbulb/bin/agent-browser --version
ENV PATH="/opt/lightbulb/bin:${PATH}"

COPY bridge /opt/bridge/bridge
COPY plugins/ /opt/hermes/plugins/
# Bundled agent-facing skills. NOT checked in: `skills/` is GENERATED into this build
# context by ../build-skills.sh (run by build-hermes-image.yml before `docker build`) from
# skills-src/ + skills-overlay/, with tool names swapped for Hermes/our plugin's. If this
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
