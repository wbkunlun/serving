FROM golang:1.25 AS builder
ARG component
WORKDIR /src
COPY . .
RUN GOARCH=amd64 CGO_ENABLED=0 go build -ldflags="-s -w" -o /ko-app ./cmd/${component}

# Default target: Debian-based with ops tools (for activator, autoscaler, controller, webhook)
FROM debian:bookworm
ARG component

# Create non-root user and group (6001:6000 app:apps)
RUN groupadd -r apps -g 6000 && \
    useradd -r -g apps -u 6001 -m -s /sbin/nologin -c "App user" app && \
    mkdir -p /logs && \
    chown -R app:apps /logs

# Install common ops tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    dnsutils \
    iproute2 \
    procps \
    netcat-openbsd \
    tcpdump \
    strace \
    lsof \
    less \
    vim-tiny && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /ko-app /usr/local/bin/${component}

USER app:apps
ENTRYPOINT /usr/local/bin/${component}

# Lean target: wolfi-based minimal image (for queue sidecar)
FROM ghcr.io/wolfi-dev/static:alpine AS wolfi
ARG component
COPY --from=builder /ko-app /usr/local/bin/${component}
ENTRYPOINT /usr/local/bin/${component}