FROM python:3.12-slim

# Install system dependencies + Node.js 22 (required by Claude Agent SDK + Playwright MCP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gosu git openssh-client \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI + compose plugin (for self-upgrade deploy via mounted socket)
RUN ARCH=$(uname -m) && \
    curl -fsSL "https://download.docker.com/linux/static/stable/${ARCH}/docker-27.5.1.tgz" \
    | tar xz --strip-components=1 -C /usr/local/bin docker/docker && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    COMPOSE_ARCH=$([ "$ARCH" = "x86_64" ] && echo "x86_64" || echo "aarch64") && \
    curl -fsSL "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-${COMPOSE_ARCH}" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose && \
    chmod +x /usr/local/bin/docker /usr/local/lib/docker/cli-plugins/docker-compose

# Create non-root user (Claude CLI refuses --dangerously-skip-permissions as root)
RUN useradd -m -s /bin/bash botuser && \
    groupadd -f docker && usermod -aG docker botuser

# Pre-install Playwright MCP npm package as botuser (avoids root npm cache issues)
# Pin version — review periodically for security patches (last: 2026-03-06)
USER botuser
RUN npx -y @playwright/mcp@0.0.68 --help > /dev/null 2>&1 || true
USER root

# Install Chromium using the SAME Playwright version that @playwright/mcp bundles.
# MCP 0.0.68 bundles playwright 1.59.x — a different version causes re-download at runtime.
# Use the exact Playwright binary from the MCP package's npx cache.
RUN PLAYWRIGHT_NPX_DIR=$(find /home/botuser/.npm/_npx -name "playwright" -path "*/node_modules/playwright" -type d 2>/dev/null | head -1) && \
    echo "Using Playwright from: $PLAYWRIGHT_NPX_DIR (version: $(node -e "console.log(require('$PLAYWRIGHT_NPX_DIR/package.json').version)"))" && \
    PLAYWRIGHT_BROWSERS_PATH=/home/botuser/.cache/ms-playwright \
    node "$PLAYWRIGHT_NPX_DIR/cli.js" install --with-deps chromium && \
    chmod -R 755 /home/botuser/.cache/ms-playwright

# Set timezone (overridable via docker-compose environment)
ARG TZ=Europe/London
ENV TZ=${TZ}
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# Install Python dependencies for bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install external MCP server dependencies (code mounted as volumes at runtime)
# Pinned versions — review periodically for security patches (last: 2026-03-06)
RUN pip install --no-cache-dir mcp==1.26.0 requests==2.32.5 python-dotenv==1.2.2 workspace-mcp==1.14.2

# Copy application code
COPY . .

# Create directories and set ownership
RUN mkdir -p data/tmp data/prompts data/browser-profile data/google-workspace-creds logs \
    /home/botuser/.claude && \
    chown -R botuser:botuser /app /home/botuser

# Entrypoint fixes volume permissions then drops to botuser
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
