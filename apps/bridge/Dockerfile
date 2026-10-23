# The bridge: linux/arm64, built by CDK (bridge-stack) from this directory.
FROM --platform=linux/arm64 rust:1.91-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src src
RUN cargo build --release --locked --bin lightbulb-bridge

FROM --platform=linux/arm64 debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/lightbulb-bridge /usr/local/bin/lightbulb-bridge
USER nobody
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/lightbulb-bridge"]
