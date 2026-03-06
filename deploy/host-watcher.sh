#!/bin/bash
# Host-side deploy watcher — runs as regular user, watches for bot-triggered deploys.
# Requires: user in 'docker' group (sudo usermod -aG docker $USER)
#
# The bot writes trigger files to /host-repo/deploy/ (mounted from host).
# This watcher runs on the HOST and polls for trigger files.
#
# SETUP (run once on host — or use scripts/setup.sh):
#   chmod +x ~/your-family-bot/deploy/host-watcher.sh
#   sudo cp ~/your-family-bot/deploy/family-bot-watcher.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now family-bot-watcher
#
# Or run manually: ./deploy/host-watcher.sh

COMPOSE_DIR="${COMPOSE_DIR:-$HOME/family-bot}"
DEPLOY_DIR="$COMPOSE_DIR/deploy"

# Use user's SSH config for git operations (host aliases for deploy keys)
export GIT_SSH_COMMAND="ssh -F $HOME/.ssh/config"

TRIGGER="$DEPLOY_DIR/deploy_trigger.json"
RESULT="$DEPLOY_DIR/deploy_result.json"
LOG="$DEPLOY_DIR/deploy_watcher.log"
HEALTHY_COMMIT_FILE="$DEPLOY_DIR/last_healthy_commit"
HEALTH_URL="http://localhost:${BOT_PORT:-8000}/health"
HEALTH_TIMEOUT=120
HEALTH_INTERVAL=5

get_rollback_target() {
    # Prefer last known healthy commit over pre-pull commit
    if [ -f "$HEALTHY_COMMIT_FILE" ]; then
        cat "$HEALTHY_COMMIT_FILE"
    else
        echo "$1"  # fallback to PREV_COMMIT passed as arg
    fi
}

save_healthy_commit() {
    local commit="$1"
    echo "$commit" > "$HEALTHY_COMMIT_FILE"
    echo "$(date) — Saved healthy commit: $commit" | tee -a "$LOG"
}

wait_for_health() {
    local elapsed=0
    echo "$(date) — Waiting for health check ($HEALTH_URL, timeout ${HEALTH_TIMEOUT}s)..." | tee -a "$LOG"
    while [ $elapsed -lt $HEALTH_TIMEOUT ]; do
        if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
            echo "$(date) — Health check passed after ${elapsed}s" | tee -a "$LOG"
            return 0
        fi
        sleep $HEALTH_INTERVAL
        elapsed=$((elapsed + HEALTH_INTERVAL))
    done
    echo "$(date) — Health check FAILED after ${HEALTH_TIMEOUT}s!" | tee -a "$LOG"
    return 1
}

# Initialize healthy commit file on first run (before any deploys)
if [ ! -f "$HEALTHY_COMMIT_FILE" ]; then
    INIT_COMMIT=$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo "")
    if [ -n "$INIT_COMMIT" ]; then
        echo "$INIT_COMMIT" > "$HEALTHY_COMMIT_FILE"
        echo "$(date) — Initialized healthy commit: $INIT_COMMIT" | tee -a "$LOG"
    fi
fi

echo "$(date) — Deploy watcher started (user: $(whoami))" | tee -a "$LOG"
echo "  Compose dir: $COMPOSE_DIR" | tee -a "$LOG"
echo "  Watching: $TRIGGER" | tee -a "$LOG"

while true; do
    if [ -f "$TRIGGER" ]; then
        echo "$(date) — Deploy trigger detected!" | tee -a "$LOG"
        cat "$TRIGGER" | tee -a "$LOG"

        ACTION=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('action','rebuild'))" < "$TRIGGER" 2>/dev/null || echo "rebuild")
        REASON=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('reason','unknown'))" < "$TRIGGER" 2>/dev/null || echo "unknown")

        rm -f "$TRIGGER"
        echo "{\"status\":\"in_progress\",\"started\":\"$(date -Iseconds)\"}" > "$RESULT"

        cd "$COMPOSE_DIR"

        # Save current commit for rollback
        PREV_COMMIT=$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo "")

        # Pull latest code first (repo root = COMPOSE_DIR)
        git -C "$COMPOSE_DIR" pull origin main >> "$LOG" 2>&1 || true

        NEW_COMMIT=$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo "")

        case "$ACTION" in
            rebuild)
                echo "$(date) — Rebuilding bot-core ($PREV_COMMIT → $NEW_COMMIT)..." | tee -a "$LOG"
                if docker compose build --no-cache bot-core >> "$LOG" 2>&1; then
                    echo "$(date) — Build OK. Restarting..." | tee -a "$LOG"
                    docker compose up -d bot-core >> "$LOG" 2>&1

                    if wait_for_health; then
                        save_healthy_commit "$NEW_COMMIT"
                        echo "{\"status\":\"success\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"commit\":\"$NEW_COMMIT\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                        echo "$(date) — Deploy complete!" | tee -a "$LOG"
                    else
                        ROLLBACK_TARGET=$(get_rollback_target "$PREV_COMMIT")
                        if [ -n "$ROLLBACK_TARGET" ] && [ "$ROLLBACK_TARGET" != "$NEW_COMMIT" ]; then
                            echo "$(date) — AUTO-ROLLBACK: reverting to $ROLLBACK_TARGET (last healthy)" | tee -a "$LOG"
                            git -C "$COMPOSE_DIR" reset --hard "$ROLLBACK_TARGET" >> "$LOG" 2>&1
                            if docker compose build --no-cache bot-core >> "$LOG" 2>&1; then
                                docker compose up -d bot-core >> "$LOG" 2>&1
                                echo "$(date) — Rollback build complete, waiting for health..." | tee -a "$LOG"
                                if wait_for_health; then
                                    save_healthy_commit "$ROLLBACK_TARGET"
                                    echo "{\"status\":\"rolled_back\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"failed_commit\":\"$NEW_COMMIT\",\"restored_commit\":\"$ROLLBACK_TARGET\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                                    echo "$(date) — Rollback successful! Restored $ROLLBACK_TARGET" | tee -a "$LOG"
                                else
                                    echo "{\"status\":\"rollback_unhealthy\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"failed_commit\":\"$NEW_COMMIT\",\"restored_commit\":\"$ROLLBACK_TARGET\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                                    echo "$(date) — WARNING: Rollback deployed but health check still failing!" | tee -a "$LOG"
                                fi
                            else
                                echo "{\"status\":\"rollback_build_failed\",\"action\":\"$ACTION\",\"failed_commit\":\"$NEW_COMMIT\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                                echo "$(date) — CRITICAL: Rollback build also failed!" | tee -a "$LOG"
                            fi
                        else
                            echo "{\"status\":\"unhealthy\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"commit\":\"$NEW_COMMIT\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                            echo "$(date) — Deploy unhealthy, no healthy commit to rollback to" | tee -a "$LOG"
                        fi
                    fi
                else
                    ROLLBACK_TARGET=$(get_rollback_target "$PREV_COMMIT")
                    echo "$(date) — BUILD FAILED! Reverting to $ROLLBACK_TARGET" | tee -a "$LOG"
                    if [ -n "$ROLLBACK_TARGET" ] && [ "$ROLLBACK_TARGET" != "$NEW_COMMIT" ]; then
                        git -C "$COMPOSE_DIR" reset --hard "$ROLLBACK_TARGET" >> "$LOG" 2>&1
                    fi
                    echo "{\"status\":\"build_failed\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"failed_commit\":\"$NEW_COMMIT\",\"restored_commit\":\"$ROLLBACK_TARGET\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                fi
                ;;
            restart)
                echo "$(date) — Restarting bot-core (no rebuild)..." | tee -a "$LOG"
                docker compose restart bot-core >> "$LOG" 2>&1
                echo "{\"status\":\"success\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                ;;
            rebuild-all)
                echo "$(date) — Rebuilding ALL services..." | tee -a "$LOG"
                if docker compose build --no-cache >> "$LOG" 2>&1; then
                    docker compose up -d >> "$LOG" 2>&1
                    echo "{\"status\":\"success\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                else
                    echo "{\"status\":\"build_failed\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                fi
                ;;
            *)
                echo "{\"status\":\"error\",\"message\":\"Unknown action: $ACTION\"}" > "$RESULT"
                ;;
        esac
    fi
    sleep 5
done
