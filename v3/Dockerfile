FROM cloudflare/cloudflared@sha256:0aa26e284f05e6c77ae375b8c9c11d9eb6a448fb7bcd8d40f31cb6176189eb38 AS preview_tunnel
FROM rust:1.90-bookworm@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f AS builder
WORKDIR /build
COPY v3/Cargo.toml v3/Cargo.lock ./
COPY v3/build.rs ./build.rs
COPY v3/src ./src
# COMPACTION CONFIG: build-time tests read this fixture; runtime uses the single
# host config directory mounted by deploy.sh, not an embedded configuration.
COPY v3/config ./config
# GATE-CONFIG: only the two selected non-secret live JSON files enter the image.
COPY v3/data/config/gates ./data/config/gates
COPY v3/plugins ./plugins
# Runtime tests load the installed ability catalog and chat configuration.
COPY v3/plugins/abilities ./plugins/abilities
COPY v3/ui ./ui
ENV CARGO_BUILD_JOBS=2 CARGO_INCREMENTAL=0
RUN cargo test --locked -- --test-threads=1
RUN cargo build --release --locked

FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates curl tini git ripgrep build-essential pkg-config chromium chromium-driver python3-selenium \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1100 v3 && useradd -u 1100 -g 1100 -M -s /usr/sbin/nologin v3app \
    && useradd -u 1101 -g 1100 -M -s /usr/sbin/nologin v3tools
COPY --from=preview_tunnel /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY --from=builder /build/target/release/webagent-v3 /usr/local/bin/webagent-v3
COPY --from=builder /usr/local/rustup/toolchains/1.90.0-x86_64-unknown-linux-gnu /opt/rust
COPY v3/worker.py v3/sandbox.py v3/launch.py v3/promote.py /opt/v3/
COPY v3/plugins/abilities /opt/v3/plugins/abilities
COPY v3/plugins/gates /opt/v3/plugins/gates
COPY v3 /opt/v3/source-template
COPY v3/ui /opt/v3/ui
COPY v3/ui-baseline.json /opt/v3/ui-baseline.json
COPY ui/shared/css/app1.css ui/shared/css/app2.css ui/shared/css/app3.css ui/shared/css/design-system.css ui/shared/css/index.css /opt/v3/shared-ui/shared/css/
COPY ui/main-panel/agents/agents.html ui/main-panel/agents/agents.css /opt/v3/shared-ui/main-panel/agents/
COPY ui/vendor/fonts /opt/v3/shared-ui/vendor/fonts
COPY ui/vendor/lucide /opt/v3/shared-ui/vendor/lucide
COPY ui/favicon.svg /opt/v3/shared-ui/
ENV PATH=/opt/rust/bin:$PATH
ENV V3_LISTEN=0.0.0.0:8080 V3_STATE_ROOT=/state V3_WORKSPACE_ROOT=/workspaces V3_SECURE_COOKIE=true
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 CMD curl --fail http://127.0.0.1:8080/health/ready || exit 1
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["/usr/bin/python3","-I","/opt/v3/launch.py"]
