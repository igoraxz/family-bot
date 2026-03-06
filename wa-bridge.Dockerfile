# Dockerfile for whatsapp-bridge
# Based on github.com/lharries/whatsapp-mcp (MIT license)
# with updated whatsmeow for WhatsApp API compatibility
FROM golang:1.25-bookworm AS builder

WORKDIR /build

# Cache dependencies
COPY whatsapp-bridge/go.mod whatsapp-bridge/go.sum ./
RUN go mod download

# Build with CGO enabled (required for go-sqlite3)
COPY whatsapp-bridge/main.go .
RUN CGO_ENABLED=1 GOOS=linux GOARCH=amd64 \
    go build -o whatsapp-bridge .

# --- Runtime ---
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /build/whatsapp-bridge .

# Data directory for SQLite DBs (session store + messages)
RUN mkdir -p /app/store
VOLUME /app/store

EXPOSE 8080

CMD ["./whatsapp-bridge", "-port", "8080", "-datadir", "/app/store"]
