#!/bin/bash
# ============================================================
# Family Bot — Full Setup Wizard
# ============================================================
# End-to-end setup for a new family bot deployment:
#   1. Family config (family_config.json)
#   2. Environment variables (.env)
#   3. Data directory initialization
#   4. Server-side git repo setup
#   5. WhatsApp bridge pairing
#   6. Deploy watcher (systemd service)
#   7. Docker build & launch
#
# PREREQUISITE: Fork/clone this repo first. Each family bot
# should run from its own fork so that the bot's self-upgrade
# feature (git commit + push from inside the container) pushes
# to YOUR fork, not the upstream repo.
#
# Usage:
#   ./scripts/setup.sh           # Full interactive setup
#   ./scripts/setup.sh --config  # Only generate config files
#   ./scripts/setup.sh --deploy  # Only deploy (skip config)
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$PROJECT_DIR/data"
CONFIG_FILE="$DATA_DIR/family_config.json"
ENV_FILE="$PROJECT_DIR/.env"
DEPLOY_DIR="$PROJECT_DIR/deploy"

MODE="${1:-full}"  # full, --config, --deploy

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

header() { echo -e "\n${BLUE}${BOLD}=== $1 ===${NC}\n"; }
info() { echo -e "${CYAN}$1${NC}"; }
warn() { echo -e "${YELLOW}$1${NC}"; }
success() { echo -e "${GREEN}$1${NC}"; }
errormsg() { echo -e "${RED}$1${NC}"; }

prompt_required() {
    local varname="$1" prompt="$2" default="$3"
    local value=""
    while [ -z "$value" ]; do
        if [ -n "$default" ]; then
            read -rp "$(echo -e "${BOLD}$prompt${NC} [$default]: ")" value
            value="${value:-$default}"
        else
            read -rp "$(echo -e "${BOLD}$prompt${NC}: ")" value
        fi
        [ -z "$value" ] && errormsg "  This field is required."
    done
    eval "$varname=\"$value\""
}

prompt_optional() {
    local varname="$1" prompt="$2" default="$3"
    read -rp "$(echo -e "${BOLD}$prompt${NC} [$default]: ")" value
    eval "$varname=\"${value:-$default}\""
}

prompt_yn() {
    local prompt="$1" default="$2"
    local yn=""
    read -rp "$(echo -e "${BOLD}$prompt${NC} [${default}]: ")" yn
    yn="${yn:-$default}"
    [[ "$yn" =~ ^[Yy] ]]
}

# ============================================================
# Banner
# ============================================================
echo -e "${BLUE}${BOLD}"
echo "  ____            _ _         ____        _   "
echo " |  _ \\ __ _ _ __ (_) |_   _  | __ )  ___ | |_ "
echo " | |_) / _\` | '_ \\| | | | | | |  _ \\ / _ \\| __|"
echo " |  __/ (_| | | | | | | |_| | | |_) | (_) | |_ "
echo " |_|   \\__,_|_| |_|_|_|\\__, | |____/ \\___/ \\__|"
echo "                        |___/                    "
echo -e "${NC}"
echo -e "${BOLD}Full Setup Wizard${NC}"
echo ""

# ============================================================
# STEP 0: Prerequisites Check
# ============================================================
header "Step 0: Prerequisites"

# Check we're in a git repo
if [ ! -d "$PROJECT_DIR/.git" ]; then
    errormsg "This directory is not a git repository."
    errormsg "Please fork/clone the family-bot repo first."
    echo ""
    echo "  How to set up:"
    echo "  1. Fork the repo on GitHub: github.com/igoraxz/family-bot → Fork"
    echo "  2. Name your fork (e.g. 'smith-family-bot')"
    echo "  3. Clone YOUR fork, using the fork name as folder:"
    echo "     git clone git@github.com:YOU/smith-family-bot.git ~/smith-family-bot"
    echo "  4. Run this script from inside the clone:"
    echo "     cd ~/smith-family-bot && ./scripts/setup.sh"
    echo ""
    echo "  Why a fork? The bot can self-upgrade by committing and pushing."
    echo "  A fork ensures those changes go to YOUR repo, not upstream."
    echo "  Name the folder after your fork to avoid confusion."
    exit 1
fi

# Check git remote
REMOTE_URL=$(git -C "$PROJECT_DIR" remote get-url origin 2>/dev/null || echo "")
if [ -n "$REMOTE_URL" ]; then
    success "Git repo: $REMOTE_URL"
else
    warn "No git remote 'origin' configured."
    warn "The bot's self-upgrade feature needs a remote to push to."
    echo ""
    if prompt_yn "Add a git remote now?" "y"; then
        prompt_required GIT_REMOTE "  Git remote URL (SSH recommended)" ""
        git -C "$PROJECT_DIR" remote add origin "$GIT_REMOTE" 2>/dev/null || \
            git -C "$PROJECT_DIR" remote set-url origin "$GIT_REMOTE"
        success "  Set origin to $GIT_REMOTE"
    fi
fi

echo ""
info "IMPORTANT: Each family bot should run from its own fork."
info "The bot self-upgrades by committing + pushing to this repo."
info "Using a fork ensures those changes go to YOUR repo, not upstream."
info "Name your clone folder after the fork (e.g. ~/smith-family-bot)"
info "to distinguish it from the upstream family-bot repo."

# Check Docker
if command -v docker &>/dev/null; then
    success "Docker: $(docker --version | head -1)"
else
    errormsg "Docker not found. Install Docker first: https://docs.docker.com/get-docker/"
    exit 1
fi

if command -v docker compose &>/dev/null || docker compose version &>/dev/null 2>&1; then
    success "Docker Compose: available"
else
    errormsg "Docker Compose not found. Install Docker Compose v2."
    exit 1
fi

# Check python3
if command -v python3 &>/dev/null; then
    success "Python 3: $(python3 --version)"
else
    warn "Python 3 not found locally. Config generation may fail."
    warn "Python 3 is only needed for this setup script; the bot runs in Docker."
fi

if [ "$MODE" = "--deploy" ]; then
    # Skip config, jump to deploy
    if [ ! -f "$CONFIG_FILE" ]; then
        errormsg "No family_config.json found. Run without --deploy first."
        exit 1
    fi
    if [ ! -f "$ENV_FILE" ]; then
        errormsg "No .env found. Run without --deploy first."
        exit 1
    fi
    # Jump to deploy section
    SKIP_CONFIG=true
    SKIP_ENV=true
fi

# ============================================================
# STEP 1: Family Config
# ============================================================
if [ "${SKIP_CONFIG:-}" != "true" ]; then

    if [ -f "$CONFIG_FILE" ]; then
        warn "family_config.json already exists at $CONFIG_FILE"
        if ! prompt_yn "Overwrite it?" "n"; then
            info "Keeping existing config."
            SKIP_CONFIG=true
        fi
    fi
fi

if [ "${SKIP_CONFIG:-}" != "true" ]; then
    header "Step 1: Family Information"

    prompt_required FAMILY_NAME "Family surname (e.g. Smith)" ""
    prompt_optional BOT_NAME "Bot name" "$FAMILY_NAME Family Bot"
    prompt_optional TIMEZONE "Timezone" "Europe/London"
    prompt_optional LOCATION "Home address (optional, helps with directions)" ""

    # --- Parents ---
    header "Step 1a: Parents / Guardians"
    info "Add parents/guardians. These are the primary bot users."
    echo ""

    PARENTS_JSON="[]"
    PARENT_NUM=0
    while true; do
        PARENT_NUM=$((PARENT_NUM + 1))
        if [ $PARENT_NUM -gt 1 ]; then
            prompt_yn "Add another parent?" "y" || break
        fi

        echo -e "\n${BOLD}Parent $PARENT_NUM:${NC}"
        prompt_required P_NAME "  Full name" ""
        prompt_required P_ROLE "  Role (father/mother/guardian)" ""
        prompt_required P_EMAIL "  Email" ""

        prompt_optional P_TG_ID "  Telegram user ID (number, or blank)" ""
        prompt_optional P_TG_USERNAME "  Telegram username (without @, or blank)" ""
        prompt_optional P_WA_PHONE "  WhatsApp phone (intl, e.g. 447700900001, or blank)" ""

        IS_ADMIN="false"
        prompt_yn "  Is this person an admin (can self-upgrade bot)?" "y" && IS_ADMIN="true"

        P_JSON="{"
        P_JSON+="\"name\":\"$P_NAME\","
        P_JSON+="\"role\":\"$P_ROLE\","
        P_JSON+="\"email\":\"$P_EMAIL\","
        [ -n "$P_TG_ID" ] && P_JSON+="\"telegram_id\":$P_TG_ID,"
        [ -n "$P_TG_USERNAME" ] && P_JSON+="\"telegram_username\":\"$P_TG_USERNAME\","
        [ -n "$P_WA_PHONE" ] && P_JSON+="\"whatsapp_phone\":\"$P_WA_PHONE\","
        P_JSON+="\"is_admin\":$IS_ADMIN"
        P_JSON+="}"

        if [ "$PARENTS_JSON" = "[]" ]; then
            PARENTS_JSON="[$P_JSON]"
        else
            PARENTS_JSON="${PARENTS_JSON%]}, $P_JSON]"
        fi
    done

    # --- Children ---
    header "Step 1b: Children"
    CHILDREN_JSON="[]"
    if prompt_yn "Add children?" "y"; then
        CHILD_NUM=0
        while true; do
            CHILD_NUM=$((CHILD_NUM + 1))
            [ $CHILD_NUM -gt 1 ] && { prompt_yn "Add another child?" "y" || break; }

            echo -e "\n${BOLD}Child $CHILD_NUM:${NC}"
            prompt_required C_NAME "  First name" ""
            prompt_optional C_SCHOOL "  School name (or blank)" ""
            prompt_optional C_CLASS "  Class/form (or blank)" ""
            prompt_optional C_TUTOR "  Form tutor (or blank)" ""

            C_JSON="{\"name\":\"$C_NAME\""
            [ -n "$C_SCHOOL" ] && C_JSON+=",\"school\":\"$C_SCHOOL\""
            [ -n "$C_CLASS" ] && C_JSON+=",\"class\":\"$C_CLASS\""
            [ -n "$C_TUTOR" ] && C_JSON+=",\"form_tutor\":\"$C_TUTOR\""
            C_JSON+="}"

            if [ "$CHILDREN_JSON" = "[]" ]; then
                CHILDREN_JSON="[$C_JSON]"
            else
                CHILDREN_JSON="${CHILDREN_JSON%]}, $C_JSON]"
            fi
        done
    fi

    # --- Other members ---
    header "Step 1c: Other Household Members"
    OTHER_JSON="[]"
    if prompt_yn "Add other household members (nanny, au pair, etc.)?" "n"; then
        OTHER_NUM=0
        while true; do
            OTHER_NUM=$((OTHER_NUM + 1))
            [ $OTHER_NUM -gt 1 ] && { prompt_yn "Add another member?" "n" || break; }
            prompt_required O_NAME "  Name" ""
            prompt_required O_ROLE "  Role (nanny/au pair/etc.)" ""
            O_JSON="{\"name\":\"$O_NAME\",\"role\":\"$O_ROLE\"}"
            if [ "$OTHER_JSON" = "[]" ]; then
                OTHER_JSON="[$O_JSON]"
            else
                OTHER_JSON="${OTHER_JSON%]}, $O_JSON]"
            fi
        done
    fi

    # --- Goals ---
    header "Step 1d: Family Goals"
    info "What should the bot help with? Enter goals one per line."
    info "Press Enter on an empty line when done."
    echo ""
    GOALS_JSON="[]"
    GOAL_NUM=0
    while true; do
        GOAL_NUM=$((GOAL_NUM + 1))
        read -rp "$(echo -e "${BOLD}Goal $GOAL_NUM (or Enter to finish)${NC}: ")" GOAL
        [ -z "$GOAL" ] && break
        GOAL_ESC=$(echo "$GOAL" | sed 's/"/\\"/g')
        if [ "$GOALS_JSON" = "[]" ]; then
            GOALS_JSON="[\"$GOAL_ESC\"]"
        else
            GOALS_JSON="${GOALS_JSON%]}, \"$GOAL_ESC\"]"
        fi
    done
    if [ "$GOALS_JSON" = "[]" ]; then
        GOALS_JSON='["Plan family time effectively","Support children'\''s education","Stay on top of school events and deadlines"]'
    fi

    # --- Phone agent ---
    header "Step 1e: Phone Agent (AI voice calls)"
    prompt_optional PHONE_SURNAME "Family surname for phone agent" "$FAMILY_NAME"
    prompt_optional PHONE_GENDER "Default voice gender (male/female)" "male"

    # --- Email config ---
    header "Step 1f: Email"
    FIRST_EMAIL=$(echo "$PARENTS_JSON" | grep -o '"email":"[^"]*"' | head -1 | cut -d'"' -f4)
    FIRST_NAME=$(echo "$PARENTS_JSON" | grep -o '"name":"[^"]*"' | head -1 | cut -d'"' -f4 | cut -d' ' -f1)
    prompt_optional PRIMARY_EMAIL "Primary email for sending" "$FIRST_EMAIL"
    prompt_optional PRIMARY_EMAIL_USER "Sender display name" "$FIRST_NAME"

    # --- Write family_config.json ---
    header "Writing family_config.json"
    mkdir -p "$DATA_DIR"

    python3 -c "
import json

config = {
    'family_name': '''$FAMILY_NAME''',
    'bot_name': '''$BOT_NAME''',
    'timezone': '''$TIMEZONE''',
    'location': '''$LOCATION''',
    'members': {
        'parents': json.loads('''$PARENTS_JSON'''),
        'children': json.loads('''$CHILDREN_JSON'''),
        'other': json.loads('''$OTHER_JSON''')
    },
    'goals': json.loads('''$GOALS_JSON'''),
    'phone_agent': {
        'family_surname': '''$PHONE_SURNAME''',
        'default_gender': '''$PHONE_GENDER'''
    },
    'email': {
        'primary_address': '''$PRIMARY_EMAIL''',
        'primary_user_name': '''$PRIMARY_EMAIL_USER'''
    }
}

with open('$CONFIG_FILE', 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
print('OK')
" && success "  Saved: $CONFIG_FILE" || { errormsg "  Failed to write config!"; exit 1; }
fi

# ============================================================
# STEP 2: Environment Variables (.env)
# ============================================================
if [ "${SKIP_ENV:-}" != "true" ]; then
    header "Step 2: Environment Variables (.env)"

    if [ -f "$ENV_FILE" ]; then
        warn ".env already exists at $ENV_FILE"
        if ! prompt_yn "Overwrite it?" "n"; then
            info "Keeping existing .env."
            SKIP_ENV=true
        fi
    fi
fi

if [ "${SKIP_ENV:-}" != "true" ]; then
    info "We'll ask for key values. Press Enter to leave blank (configure later)."
    echo ""

    # Auth
    echo -e "${BOLD}Authentication (pick one):${NC}"
    prompt_optional AUTH_TOKEN "  CLAUDE_CODE_OAUTH_TOKEN (recommended)" ""
    prompt_optional API_KEY "  ANTHROPIC_API_KEY (alternative)" ""

    # Telegram
    echo -e "\n${BOLD}Telegram:${NC}"
    prompt_optional TG_TOKEN "  TG_BOT_TOKEN (from @BotFather)" ""
    prompt_optional TG_CHAT "  TG_CHAT_ID (group chat, negative number)" ""
    prompt_optional TG_WEBHOOK "  TG_WEBHOOK_URL (e.g. https://bot.example.com/webhook/telegram)" ""
    prompt_optional TG_BOT_UID "  TG_BOT_USER_ID (bot's own user ID)" ""

    # WhatsApp
    echo -e "\n${BOLD}WhatsApp (optional — leave blank to disable):${NC}"
    prompt_optional WA_PHONE "  WA_BOT_PHONE (bot's phone number)" ""
    prompt_optional WA_GROUP "  WA_FAMILY_GROUP_JID (e.g. 120363XXXXX@g.us)" ""

    # Google
    echo -e "\n${BOLD}Google Workspace (optional):${NC}"
    prompt_optional G_CLIENT_ID "  GOOGLE_OAUTH_CLIENT_ID" ""
    prompt_optional G_CLIENT_SECRET "  GOOGLE_OAUTH_CLIENT_SECRET" ""

    # Vapi
    echo -e "\n${BOLD}Phone calls — Vapi (optional):${NC}"
    prompt_optional V_API_KEY "  VAPI_API_KEY" ""
    prompt_optional V_PHONE_ID "  VAPI_PHONE_NUMBER_ID" ""
    prompt_optional V_WEBHOOK "  WEBHOOK_BASE_URL (public URL)" ""

    # Gemini
    echo -e "\n${BOLD}Image generation (optional):${NC}"
    prompt_optional GEM_KEY "  GEMINI_API_KEY" ""

    # Docker
    echo -e "\n${BOLD}Docker:${NC}"
    COMPOSE_DEFAULT="${FAMILY_NAME:-family}"
    COMPOSE_DEFAULT=$(echo "$COMPOSE_DEFAULT" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')
    prompt_optional D_PROJECT "  COMPOSE_PROJECT_NAME" "${COMPOSE_DEFAULT}-bot"
    prompt_optional D_PORT "  BOT_PORT" "8000"
    prompt_optional D_TZ "  TZ (timezone)" "${TIMEZONE:-Europe/London}"

    info ""
    info "Note: Scheduled tasks (morning reminders, email checks) are managed"
    info "conversationally via the bot — no env vars needed."
    info ""
    info "Note: TG_ALLOWED_USERS, WA_ALLOWED_PHONES, and ADMIN_USERS"
    info "are auto-populated from family_config.json at runtime."

    # --- Write .env ---
    header "Writing .env"
    cat > "$ENV_FILE" << ENVEOF
# ============================================================
# Family Bot — Environment Configuration
# Generated by setup wizard on $(date +%Y-%m-%d)
# ============================================================

# === AUTHENTICATION ===
CLAUDE_CODE_OAUTH_TOKEN=$AUTH_TOKEN
# ANTHROPIC_API_KEY=$API_KEY

# === TELEGRAM ===
TG_BOT_TOKEN=$TG_TOKEN
TG_CHAT_ID=$TG_CHAT
TG_WEBHOOK_URL=$TG_WEBHOOK
TG_BOT_USER_ID=$TG_BOT_UID

# Auth lists — auto-populated from family_config.json if empty
# TG_ALLOWED_USERS=
# WA_ALLOWED_PHONES=
# ADMIN_USERS=

# === WHATSAPP ===
WA_BOT_PHONE=$WA_PHONE
WA_FAMILY_GROUP_JID=$WA_GROUP

# === GOOGLE WORKSPACE ===
GOOGLE_OAUTH_CLIENT_ID=$G_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET=$G_CLIENT_SECRET

# === VAPI PHONE ===
VAPI_API_KEY=$V_API_KEY
VAPI_PHONE_NUMBER_ID=$V_PHONE_ID
WEBHOOK_BASE_URL=$V_WEBHOOK

# === IMAGE GENERATION ===
GEMINI_API_KEY=$GEM_KEY

# === DOCKER ===
COMPOSE_PROJECT_NAME=$D_PROJECT
BOT_PORT=$D_PORT
TZ=$D_TZ
MEDIA_RETENTION_DAYS=30
ENVEOF

    success "  Saved: $ENV_FILE"
fi

# ============================================================
# STEP 3: Initialize Data Files
# ============================================================
header "Step 3: Initializing Data Files"

for dir in "$DATA_DIR/prompts" "$DATA_DIR/google-workspace-creds" "$DATA_DIR/media_cache" "$DATA_DIR/tmp"; do
    mkdir -p "$dir"
done
success "  Created data directories"

[ -f "$DATA_DIR/family_knowledge.md" ] || { echo "# Family Knowledge Base" > "$DATA_DIR/family_knowledge.md"; info "  Created family_knowledge.md"; }
[ -f "$DATA_DIR/family_facts.json" ] || { echo "{}" > "$DATA_DIR/family_facts.json"; info "  Created family_facts.json"; }
[ -f "$DATA_DIR/family_goals.json" ] || { echo "{}" > "$DATA_DIR/family_goals.json"; info "  Created family_goals.json"; }

if [ "$MODE" = "--config" ]; then
    header "Config Setup Complete!"
    echo "  Run ./scripts/setup.sh --deploy to continue with deployment."
    exit 0
fi

# ============================================================
# STEP 4: Server-Side Git Setup
# ============================================================
header "Step 4: Server-Side Git Setup"

info "The bot needs a git repo on the server for self-upgrade."
info "The bot commits changes inside its container and pushes to origin."
echo ""
info "Architecture:"
info "  YOUR FORK (e.g. github.com/you/smith-family-bot)"
info "       |"
info "  SERVER (~/smith-family-bot — named after fork) <-- Docker runs here"
info "       |"
info "  CONTAINER (/host-repo mount) <-- bot edits code here"
echo ""
info "Tip: Name the server folder after your fork to avoid confusion"
info "with the upstream 'family-bot' repo."
echo ""

if prompt_yn "Set up server-side git (SSH deploy key, safe.directory)?" "y"; then

    # Check if we have a git remote
    REMOTE_URL=$(git -C "$PROJECT_DIR" remote get-url origin 2>/dev/null || echo "")
    if [ -z "$REMOTE_URL" ]; then
        prompt_required REMOTE_URL "  Git remote URL (SSH format, e.g. git@github.com:you/yourname-family-bot.git)" ""
        git -C "$PROJECT_DIR" remote add origin "$REMOTE_URL" 2>/dev/null || \
            git -C "$PROJECT_DIR" remote set-url origin "$REMOTE_URL"
    fi

    info ""
    info "For the bot to push changes, you need an SSH deploy key."
    info "This is a key pair where the public key is added to your"
    info "git host as a deploy key with WRITE access."
    echo ""

    DEPLOY_KEY_PATH="$HOME/.ssh/family_bot_deploy"
    if [ ! -f "$DEPLOY_KEY_PATH" ]; then
        if prompt_yn "Generate a new deploy key now?" "y"; then
            ssh-keygen -t ed25519 -f "$DEPLOY_KEY_PATH" -N "" -C "family-bot-deploy-$(hostname -s)"
            success "  Generated: $DEPLOY_KEY_PATH"
            echo ""
            echo -e "${BOLD}Add this PUBLIC KEY to your git host as a deploy key (with write access):${NC}"
            echo ""
            cat "${DEPLOY_KEY_PATH}.pub"
            echo ""
            info "GitHub: Settings → Deploy Keys → Add Key → paste the above"
            info "GitLab: Settings → Repository → Deploy Keys → paste the above"
            echo ""
            read -rp "$(echo -e "${BOLD}Press Enter when done...${NC}")"
        fi
    else
        success "  Deploy key exists: $DEPLOY_KEY_PATH"
    fi

    # Configure git to use the deploy key
    git -C "$PROJECT_DIR" config core.sshCommand "ssh -i $DEPLOY_KEY_PATH -o IdentitiesOnly=yes"
    success "  Configured git to use deploy key"

    # Mark repo as safe for bot user
    git -C "$PROJECT_DIR" config --global --add safe.directory "$PROJECT_DIR"
    success "  Marked repo as safe directory"

    # Generate docker-compose.override.yml for deploy key mount
    OVERRIDE_FILE="$PROJECT_DIR/docker-compose.override.yml"
    if [ ! -f "$OVERRIDE_FILE" ]; then
        cat > "$OVERRIDE_FILE" << OVERRIDEEOF
# Auto-generated by setup wizard — mounts deploy key for self-upgrade
services:
  bot-core:
    volumes:
      - ${DEPLOY_KEY_PATH}:/root/.ssh/id_ed25519:ro
      - ${DEPLOY_KEY_PATH}.pub:/root/.ssh/id_ed25519.pub:ro
OVERRIDEEOF
        success "  Created docker-compose.override.yml (deploy key mount)"
    else
        warn "  docker-compose.override.yml already exists — not overwriting."
        info "  Ensure it mounts the deploy key:"
        echo "    - ${DEPLOY_KEY_PATH}:/root/.ssh/id_ed25519:ro"
        echo "    - ${DEPLOY_KEY_PATH}.pub:/root/.ssh/id_ed25519.pub:ro"
    fi
else
    info "Skipping git setup. Self-upgrade will need manual configuration."
fi

# ============================================================
# STEP 5: WhatsApp Bridge
# ============================================================
header "Step 5: WhatsApp Bridge"

# Read WA config from .env
WA_BOT_PHONE_VAL=""
if [ -f "$ENV_FILE" ]; then
    WA_BOT_PHONE_VAL=$(grep '^WA_BOT_PHONE=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
fi

if [ -n "$WA_BOT_PHONE_VAL" ]; then
    info "WhatsApp is configured (phone: $WA_BOT_PHONE_VAL)."
    info "The WhatsApp bridge (whatsmeow) needs a one-time QR code pairing."
    echo ""
    info "After 'docker compose up -d', pair the bridge:"
    echo ""
    echo "  1. Check bridge logs for QR code:"
    D_PROJECT_VAL=$(grep '^COMPOSE_PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "family-bot")
    echo "     docker logs ${D_PROJECT_VAL:-family-bot}-wa-bridge"
    echo ""
    echo "  2. Scan the QR code with WhatsApp on your phone:"
    echo "     WhatsApp → Settings → Linked Devices → Link a Device"
    echo ""
    echo "  3. The bridge stores its session in a Docker volume."
    echo "     It survives container restarts — pair once only."
    echo ""
    info "The bot will start polling WhatsApp once the bridge is paired."
else
    info "WhatsApp not configured (WA_BOT_PHONE is empty)."
    info "To enable later: set WA_BOT_PHONE and WA_FAMILY_GROUP_JID in .env"
fi

# ============================================================
# STEP 6: Deploy Watcher (systemd)
# ============================================================
header "Step 6: Deploy Watcher Service"

info "The deploy watcher is a host-side systemd service that watches"
info "for deploy triggers from the bot. When the bot calls deploy_bot(),"
info "it writes a trigger file. The watcher picks it up, pulls code,"
info "rebuilds, and restarts the container."
echo ""

if prompt_yn "Install the deploy watcher service now?" "y"; then
    # Get the current user for the service
    CURRENT_USER=$(whoami)
    BOT_DIR="$PROJECT_DIR"

    # Generate the systemd service file
    SERVICE_FILE="/tmp/family-bot-watcher.service"
    cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=Family Bot Deploy Watcher ($(basename "$BOT_DIR"))
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=$CURRENT_USER
Group=docker
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/deploy/host-watcher.sh
Restart=always
RestartSec=10
Environment=COMPOSE_DIR=$BOT_DIR
Environment=HOME=$HOME

[Install]
WantedBy=multi-user.target
SVCEOF

    # Update host-watcher.sh to use correct path
    WATCHER_SCRIPT="$DEPLOY_DIR/host-watcher.sh"
    if [ -f "$WATCHER_SCRIPT" ]; then
        # Update the default COMPOSE_DIR in the watcher script
        sed -i.bak "s|COMPOSE_DIR=.*|COMPOSE_DIR=\"\${COMPOSE_DIR:-$BOT_DIR}\"|" "$WATCHER_SCRIPT" 2>/dev/null || \
            sed -i '' "s|COMPOSE_DIR=.*|COMPOSE_DIR=\"\${COMPOSE_DIR:-$BOT_DIR}\"|" "$WATCHER_SCRIPT" 2>/dev/null
        rm -f "${WATCHER_SCRIPT}.bak"
        chmod +x "$WATCHER_SCRIPT"
        success "  Updated host-watcher.sh with correct path"
    fi

    # Determine service name (unique per family to support multiple bots)
    D_PROJECT_VAL=$(grep '^COMPOSE_PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "family-bot")
    SERVICE_NAME="${D_PROJECT_VAL:-family-bot}-watcher"

    info ""
    info "To install the systemd service, run these commands:"
    echo ""
    echo "  sudo cp $SERVICE_FILE /etc/systemd/system/${SERVICE_NAME}.service"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl enable --now ${SERVICE_NAME}"
    echo ""
    info "Check status: sudo systemctl status ${SERVICE_NAME}"

    if prompt_yn "Run these commands now (requires sudo)?" "y"; then
        sudo cp "$SERVICE_FILE" "/etc/systemd/system/${SERVICE_NAME}.service" && \
        sudo systemctl daemon-reload && \
        sudo systemctl enable --now "${SERVICE_NAME}" && \
        success "  Deploy watcher installed and started!" || \
        warn "  Failed to install service. Run the commands manually."
    fi

    # Also update the git pull path in watcher
    if [ -f "$WATCHER_SCRIPT" ]; then
        # The watcher does: git -C "$COMPOSE_DIR/.." pull
        # For the new repo structure, COMPOSE_DIR is the repo root, so pull from COMPOSE_DIR directly
        sed -i.bak 's|git -C "$COMPOSE_DIR/\.\." pull|git -C "$COMPOSE_DIR" pull|' "$WATCHER_SCRIPT" 2>/dev/null || \
            sed -i '' 's|git -C "$COMPOSE_DIR/\.\." pull|git -C "$COMPOSE_DIR" pull|' "$WATCHER_SCRIPT" 2>/dev/null
        rm -f "${WATCHER_SCRIPT}.bak"
        success "  Fixed git pull path in watcher (repo root, not parent)"
    fi
else
    info "Skipping watcher install."
    info "Install later: sudo cp deploy/family-bot-watcher.service /etc/systemd/system/"
fi

# ============================================================
# STEP 7: Docker Build & Launch
# ============================================================
header "Step 7: Build & Launch"

echo -e "${BOLD}Ready to build and start the bot?${NC}"
echo ""
echo "  This will:"
echo "  - Build Docker images (bot-core + wa-bridge)"
echo "  - Start both containers"
echo "  - The first build takes 3-5 minutes (downloads, installs)"
echo ""

if prompt_yn "Build and start now?" "y"; then
    cd "$PROJECT_DIR"

    info "Building Docker images..."
    docker compose build 2>&1 | tail -5
    success "  Build complete!"

    info "Starting containers..."
    docker compose up -d 2>&1
    success "  Containers started!"

    # Wait for health
    D_PORT_VAL=$(grep '^BOT_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "8000")
    D_PORT_VAL="${D_PORT_VAL:-8000}"

    echo ""
    info "Waiting for health check..."
    for i in $(seq 1 30); do
        if curl -sf "http://localhost:${D_PORT_VAL}/health" &>/dev/null; then
            success "  Bot is healthy!"
            echo ""
            curl -s "http://localhost:${D_PORT_VAL}/health" | python3 -m json.tool 2>/dev/null || \
                curl -s "http://localhost:${D_PORT_VAL}/health"
            break
        fi
        sleep 2
    done

    if ! curl -sf "http://localhost:${D_PORT_VAL}/health" &>/dev/null; then
        warn "  Bot not responding yet. Check logs:"
        D_PROJECT_VAL=$(grep '^COMPOSE_PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "family-bot")
        echo "    docker logs ${D_PROJECT_VAL:-family-bot} --tail 30"
    fi
else
    info "Skipping Docker launch."
    echo ""
    echo "  To build and start later:"
    echo "    cd $PROJECT_DIR"
    echo "    docker compose build"
    echo "    docker compose up -d"
fi

# ============================================================
# STEP 8: Telegram Webhook
# ============================================================
header "Step 8: Telegram Webhook"

TG_TOKEN_VAL=$(grep '^TG_BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
TG_WEBHOOK_VAL=$(grep '^TG_WEBHOOK_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)

if [ -n "$TG_TOKEN_VAL" ] && [ -n "$TG_WEBHOOK_VAL" ]; then
    info "Set the Telegram webhook so messages reach your bot:"
    echo ""
    echo "  curl -X POST \"https://api.telegram.org/bot${TG_TOKEN_VAL}/setWebhook\" \\"
    echo "    -H 'Content-Type: application/json' \\"
    echo "    -d '{\"url\": \"${TG_WEBHOOK_VAL}\"}'"
    echo ""
    if prompt_yn "Set webhook now?" "y"; then
        RESULT=$(curl -sf -X POST "https://api.telegram.org/bot${TG_TOKEN_VAL}/setWebhook" \
            -H 'Content-Type: application/json' \
            -d "{\"url\": \"${TG_WEBHOOK_VAL}\"}" 2>&1)
        if echo "$RESULT" | grep -q '"ok":true'; then
            success "  Webhook set!"
        else
            warn "  Webhook setup failed: $RESULT"
            warn "  Make sure your domain points to this server."
        fi
    fi
else
    info "Telegram not fully configured. Set TG_BOT_TOKEN and TG_WEBHOOK_URL in .env"
    info "Then set webhook: curl https://api.telegram.org/bot<TOKEN>/setWebhook -d url=<URL>"
fi

# ============================================================
# Summary
# ============================================================
header "Setup Complete!"

echo -e "${GREEN}${BOLD}Your family bot is configured!${NC}"
echo ""
echo "  Files:"
[ -f "$CONFIG_FILE" ] && echo "    data/family_config.json  — Family members, goals, config"
[ -f "$ENV_FILE" ] && echo "    .env                     — API keys, Docker settings"
echo "    data/family_knowledge.md — Knowledge base (grows over time)"
echo "    data/family_facts.json   — Learned facts (grows over time)"
echo ""

echo "  Services:"
D_PROJECT_VAL=$(grep '^COMPOSE_PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "family-bot")
D_PORT_VAL=$(grep '^BOT_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "8000")
echo "    Bot:      docker logs ${D_PROJECT_VAL:-family-bot}"
echo "    WA Bridge: docker logs ${D_PROJECT_VAL:-family-bot}-wa-bridge"
echo "    Health:   curl http://localhost:${D_PORT_VAL:-8000}/health"
echo ""

echo -e "  ${BOLD}Multi-family note:${NC}"
echo "    Each family runs from its own fork with its own .env and"
echo "    family_config.json. Multiple bots can run on the same server"
echo "    using different COMPOSE_PROJECT_NAME and BOT_PORT values."
echo ""

echo -e "  ${BOLD}Useful commands:${NC}"
echo "    docker compose logs -f bot-core    # Follow bot logs"
echo "    docker compose restart bot-core    # Restart bot"
echo "    docker compose down                # Stop everything"
echo "    docker compose up -d               # Start everything"
echo ""
success "Happy botting!"
