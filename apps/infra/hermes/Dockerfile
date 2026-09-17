# The production Hermes-on-AgentCore image. Pinned base, no diagnostic hatch, no :latest.
# Dockerfile.spike stays for local iteration; this is the built-and-pushed artifact
# (.github/workflows/build-hermes-image.yml). Runtime contents mirror the spike exactly —
# only the ordering differs, to do all root-owned setup (skill binaries, managed overlay)
# before the single USER switch to the unprivileged runtime uid AgentCore expects.
FROM nousresearch/hermes-agent:0.21.3

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
