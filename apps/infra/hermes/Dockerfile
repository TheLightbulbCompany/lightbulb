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

# ---- Go-built connector CLIs -----------------------------------------------------------------
# gog (Google) and xurl (X) are the two CLI-connector binaries with no distro package. The
# OpenClaw extended image installs gog with this same `go install` line and xurl from npm, whose
# postinstall just downloads this same Go binary -- building both here keeps node/npm out of the
# picture and pins the BYTES, since `go install` verifies every module against sum.golang.org.
#
# --platform=$BUILDPLATFORM + GOARCH=arm64 cross-compiles on the amd64 builder instead of running
# a whole Go toolchain under the QEMU emulation this image's arm64 build uses. golang:1.27.1
# because gogcli's go.mod declares `toolchain go1.27.1` (xurl needs only 1.24). GOBIN is honoured
# for a cross build too, so both binaries land in one directory.
FROM --platform=$BUILDPLATFORM golang:1.27.1-trixie AS gocli
ENV CGO_ENABLED=0 GOOS=linux GOARCH=arm64 GOBIN=/out/bin
RUN go install github.com/openclaw/gogcli/cmd/gog@v0.39.1 \
 && go install github.com/xdevplatform/xurl@v1.2.3 \
 && test -x /out/bin/gog && test -x /out/bin/xurl

FROM nousresearch/hermes-agent@sha256:a5b2eac62d3f97686e027eedd837d38bc5cc7d941f3107bebefdcef38126d103

# The bundled Anthropic SDK (0.87) predates AnthropicBedrock's bearer mode (api_key /
# AWS_BEARER_TOKEN_BEDROCK + ANTHROPIC_BEDROCK_BASE_URL), which is how our bedrock-gate is
# reached. The assertion is the gate: a silent downgrade breaks every turn.
RUN uv pip install --python "$(command -v python3)" --upgrade "anthropic>=1.0" \
 && python3 -c "import inspect, anthropic; from anthropic import AnthropicBedrock; assert 'api_key' in inspect.signature(AnthropicBedrock.__init__).parameters, 'no bearer mode'; print('anthropic', anthropic.__version__)" \
 && python3 -c "from run_agent import AIAgent; print('hermes imports ok')"

# Linux skill binaries the bundled skills shell out to (the OpenClaw extended image's set).
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl jq ripgrep unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ---- CLI connector binaries ------------------------------------------------------------------
# The binaries behind the five CLI-materializer connectors whose credentials the backend projects
# into every profile (core/services/credentials/hermes_cli.py). Instagram's "CLI" is curl + jq,
# already installed above. Each recipe mirrors apps/infra/openclaw/Dockerfile's, with the arm64
# artefact substituted: this image is built linux/arm64 only (AgentCore runtimes are arm64),
# where the OpenClaw extended image is amd64. Each layer keeps its own smoke test, so a binary
# that did not materialise fails the build instead of a turn.
COPY --from=gocli /out/bin/gog /out/bin/xurl /usr/local/bin/

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
 && gh --version && gog --version && xurl --help >/dev/null

COPY bridge /opt/bridge/bridge
COPY plugins/ /opt/hermes/plugins/
# Task 24: bundled agent-facing skills, tool names swapped for Hermes/our plugin's.
# Path matches config.yaml's skills.external_dirs (Task 7).
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
