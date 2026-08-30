# Extended Paperclip image — a thin ECR re-tag of the LightbulbAI/paperclip fork image.
#
# WHY THIS EXISTS:
#   We run a fork (LightbulbAI/paperclip) that carries our patches in REAL SOURCE —
#   the openclaw-gateway protocol-v4 fix (previously a runtime sed-overlay here)
#   plus the routine circuit breaker. The fork's own CI builds and publishes the
#   image to GHCR. This overlay no longer edits anything at build time; it simply
#   re-tags the fork image into our ECR repo so per-tenant ECS tasks can pull it
#   with their execution-role credentials (ECS has no GHCR credentials). Keeping a
#   one-line passthrough (rather than pointing the stack straight at GHCR) means
#   the deploy registry stays ECR and the existing build/promotion wiring is
#   unchanged.
#
# The base is pinned BY DIGEST in `paperclip-version.json#upstream` (now the fork
# image, e.g. ghcr.io/lightbulbai/paperclip@sha256:...). That field is the SINGLE
# SOURCE: the build workflow passes it as the UPSTREAM_IMAGE build-arg, so this
# FROM cannot drift from the digest the workflow tags and the CDK deploys against.
# There is deliberately NO default — a `docker build` that forgets the arg fails
# closed instead of silently using a stale pin. Manual builds must pass it:
#   docker build --build-arg UPSTREAM_IMAGE="$(node -p "require('./paperclip-version.json').upstream")" apps/infra/paperclip-extended/
#
# ENTRYPOINT/CMD/WORKDIR/USER/ENV are inherited from the fork image unchanged.
ARG UPSTREAM_IMAGE
FROM ${UPSTREAM_IMAGE}
