FROM golang:1.23 AS builder
ARG component
WORKDIR /src
COPY . .
RUN GOARCH=amd64 CGO_ENABLED=0 go build -ldflags="-s -w" -o /ko-app ./cmd/${component}

FROM ghcr.io/wolfi-dev/static:alpine
ARG component
COPY --from=builder /ko-app /usr/local/bin/${component}
ENTRYPOINT ["/usr/local/bin/${component}"]