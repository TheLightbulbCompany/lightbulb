# =============================================================================
# Isol8 Backend Dockerfile
# =============================================================================
# Build: docker build -t isol8-backend .
# Run:   docker run -p 8000:8000 --env-file .env isol8-backend
# =============================================================================

# -----------------------------------------------------------------------------
# Stage: gog-builder — compiles the gog CLI binary
# -----------------------------------------------------------------------------
# The gog_cli materializer runs `gog` as a server-side subprocess to seed an
# owner's Google keyring during provisioning. That keyring file is later read by
# the gog baked into the per-user OpenClaw image, so the backend's gog MUST match
# the OpenClaw image's gog: the keyring PATH is version-dependent. v0.11.0 stored
# the keyring under os.UserConfigDir()/gogcli (XDG_CONFIG_HOME on Linux), but
# newer gog follows the XDG spec and stores it under XDG_DATA_HOME/gogcli — so a
# version mismatch makes the backend write the keyring where the container never
# reads it (and there is no smoke test catching that). Pinned to v0.31.0 to match
# the OpenClaw image's gog@latest; the headless auth contract (credentials set /
# tokens import / remove) is verified on v0.31.0. v0.31.0's go.mod requires
# Go >= 1.26.2.
#
# Multi-stage so the ~600 MB Go toolchain never lands in the runtime image —
# only the static `gog` binary is copied across.
FROM golang:1.26-bookworm AS gog-builder
# Official golang image sets GOPATH=/go (no GOBIN), so `go install` drops the
# binary at /go/bin/gog.
RUN go install github.com/steipete/gogcli/cmd/gog@v0.31.0

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install build tools and curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# gog CLI (built in the gog-builder stage). The gog_cli materializer shells out
# to this binary at runtime to seed an owner's Google keyring. See the
# gog-builder stage for the version-pin rationale.
COPY --from=gog-builder /go/bin/gog /usr/local/bin/gog

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Install Python dependencies
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN chown appuser:appuser /app
USER appuser
RUN uv sync --frozen --no-dev --no-editable

# Copy application code
COPY --chown=appuser:appuser . .
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["sh", "-c", "uv run python init_db.py 2>&1 || echo 'WARNING: init_db failed'; exec uv run uvicorn main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 300 --timeout-graceful-shutdown 25"]
