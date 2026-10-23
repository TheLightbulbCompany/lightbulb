# iroh-relay for Lightbulb Connect, linux/arm64 (relay-stack.ts). The version
# matches the engine's iroh pin (lightbulb-connect Cargo.toml: iroh = "=1.4.1").
#
# n0's own release binary (static musl), checked against its sha256 before use:
# no compile, no cross toolchain. The fetch stage runs on the build host, so an
# amd64 deploy runner needs no QEMU; that needs BuildKit (the default since
# Docker 23). The runtime stage only COPYs. Base images are pinned by digest.
FROM --platform=$BUILDPLATFORM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc AS fetch
ARG RELAY_URL=https://github.com/n0-computer/iroh/releases/download/v1.4.1/iroh-relay-v1.4.1-aarch64-unknown-linux-musl.tar.gz
ARG RELAY_SHA256=3fc0c2d67c1cd66250bf82f3e2f17a985b7081a3d272186bad9d63e3f2347b38
ADD ${RELAY_URL} /relay.tgz
RUN echo "${RELAY_SHA256}  /relay.tgz" | sha256sum -c - && tar -xzf /relay.tgz -C / ./iroh-relay

# :nonroot is uid 65532; the relay listens on 8080, so it needs no root.
FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=fetch /iroh-relay /usr/local/bin/iroh-relay
COPY config.toml /etc/iroh-relay/config.toml
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/iroh-relay", "--config-path", "/etc/iroh-relay/config.toml"]
