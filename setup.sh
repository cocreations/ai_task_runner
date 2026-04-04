#!/usr/bin/env bash

# ─────────────────────────────────────────────────────────────
# Claude Task Runner — Setup Wizard
# Run this on your LOCAL machine to configure and deploy
# ─────────────────────────────────────────────────────────────

# Detect if sourced (`. ./setup.sh` or `source ./setup.sh`)
# This is dangerous because exit/errors would kill the user's shell
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    echo ""
    echo "  ERROR: Don't source this script. Run it directly:"
    echo "    ./setup.sh"
    echo ""
    return 1 2>/dev/null || true
fi

# Colours
R='\033[0;31m'
G='\033[0;32m'
Y='\033[1;33m'
B='\033[1;34m'
C='\033[0;36m'
M='\033[0;35m'
W='\033[1;37m'
DIM='\033[2m'
NC='\033[0m'

# Error handler — show a friendly message instead of crashing
trap 'echo ""; echo -e "  ${R}x${NC} Something went wrong (line $LINENO). Run ${W}./setup.sh${NC} again."; exit 1' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_FILE="$SCRIPT_DIR/.remote.yaml"
ENV_FILE="$SCRIPT_DIR/.env"
USERS_FILE="$SCRIPT_DIR/config/users.yaml"
PROJECTS_FILE="$SCRIPT_DIR/config/projects.yaml"

# ─── Helpers ─────────────────────────────────────────────────

print_banner() {
    echo ""
    echo -e "${B}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${B}║${NC}                                                      ${B}║${NC}"
    echo -e "${B}║${NC}   ${W}Claude Task Runner${NC}                                ${B}║${NC}"
    echo -e "${B}║${NC}   ${DIM}Self-hosted headless Claude Code execution${NC}        ${B}║${NC}"
    echo -e "${B}║${NC}                                                      ${B}║${NC}"
    echo -e "${B}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""
}

info()    { echo -e "  ${G}>${NC} $1"; }
warn()    { echo -e "  ${Y}!${NC} $1"; }
err()     { echo -e "  ${R}x${NC} $1"; }
ask()     { echo -en "  ${C}?${NC} $1"; }
section() { echo ""; echo -e "  ${M}── $1 ──${NC}"; echo ""; }

prompt_with_default() {
    local prompt="$1"
    local default="$2"
    local varname="$3"
    if [ -n "$default" ]; then
        ask "$prompt ${DIM}[$default]${NC}: "
    else
        ask "$prompt: "
    fi
    read -r value
    value="${value:-$default}"
    eval "$varname=\"\$value\""
}

generate_key() {
    python3 -c "import secrets; print('ctr_' + secrets.token_hex(32))" 2>/dev/null \
        || openssl rand -hex 32 | sed 's/^/ctr_/'
}

read_remote() {
    local key="$1"
    if [ -f "$REMOTE_FILE" ]; then
        grep "^${key}:" "$REMOTE_FILE" 2>/dev/null | sed "s/^${key}: *//" | tr -d '"' || echo ""
    else
        echo ""
    fi
}

show_hetzner_instructions() {
    echo ""
    echo -e "  ${W}Creating a server on Hetzner (\$4.50/mo)${NC}"
    echo ""
    echo -e "  ${C}1.${NC} Go to ${W}console.hetzner.cloud${NC} and create an account"
    echo -e "  ${C}2.${NC} Click ${W}Add Server${NC}"
    echo -e "  ${C}3.${NC} Location: pick the nearest to you"
    echo -e "  ${C}4.${NC} Image: ${W}Apps${NC} tab → ${W}Docker CE${NC}"
    echo -e "  ${C}5.${NC} Type: ${W}CX22${NC} (2 vCPU, 4GB RAM) — \$4.50/mo"
    echo -e "  ${C}6.${NC} SSH Keys: click ${W}Add SSH Key${NC}, paste your public key"
    echo -e "     ${DIM}Don't have one? Run: ssh-keygen -t ed25519${NC}"
    echo -e "     ${DIM}Then paste contents of: ~/.ssh/id_ed25519.pub${NC}"
    echo -e "  ${C}7.${NC} Click ${W}Create & Buy Now${NC}"
    echo -e "  ${C}8.${NC} Copy the IP address shown"
    echo ""
    echo -e "  ${W}DNS:${NC} Go to your DNS provider and add an A record"
    echo -e "  pointing your domain (e.g. tasks.yourdomain.com)"
    echo -e "  to the server IP."
    echo ""
}

show_linode_instructions() {
    echo ""
    echo -e "  ${W}Creating a server on Linode (\$5/mo)${NC}"
    echo ""
    echo -e "  ${C}1.${NC} Go to ${W}cloud.linode.com${NC} and create an account"
    echo -e "  ${C}2.${NC} Click ${W}Create Linode${NC}"
    echo -e "  ${C}3.${NC} ${W}Marketplace${NC} tab → search ${W}Docker${NC} → select it"
    echo -e "  ${C}4.${NC} Region: pick the nearest to you"
    echo -e "  ${C}5.${NC} Plan: Shared CPU → ${W}Nanode 1 GB${NC} — \$5/mo"
    echo -e "  ${C}6.${NC} Label: ${DIM}claude-task-runner${NC}"
    echo -e "  ${C}7.${NC} Root Password: set one"
    echo -e "  ${C}8.${NC} SSH Keys: click ${W}Add an SSH Key${NC}, paste your public key"
    echo -e "     ${DIM}Don't have one? Run: ssh-keygen -t ed25519${NC}"
    echo -e "     ${DIM}Then paste contents of: ~/.ssh/id_ed25519.pub${NC}"
    echo -e "  ${C}9.${NC} Click ${W}Create Linode${NC}"
    echo -e "  ${C}10.${NC} Wait for status ${W}Running${NC}, copy the IP address"
    echo ""
    echo -e "  ${W}DNS:${NC} Go to your DNS provider and add an A record"
    echo -e "  pointing your domain (e.g. tasks.yourdomain.com)"
    echo -e "  to the server IP."
    echo ""
}

show_digitalocean_instructions() {
    echo ""
    echo -e "  ${W}Creating a server on DigitalOcean (\$4/mo)${NC}"
    echo ""
    echo -e "  ${C}1.${NC} Go to ${W}cloud.digitalocean.com${NC} and create an account"
    echo -e "  ${C}2.${NC} Click ${W}Create${NC} → ${W}Droplets${NC}"
    echo -e "  ${C}3.${NC} ${W}Marketplace${NC} tab → search ${W}Docker${NC} → select it"
    echo -e "  ${C}4.${NC} Region: pick the nearest to you"
    echo -e "  ${C}5.${NC} Size: Basic → Regular → ${W}\$4/mo${NC} (512 MB / 1 CPU)"
    echo -e "     ${DIM}Or \$6/mo for 1 GB if you want more headroom${NC}"
    echo -e "  ${C}6.${NC} Auth: ${W}SSH Key${NC} → New SSH Key, paste your public key"
    echo -e "     ${DIM}Don't have one? Run: ssh-keygen -t ed25519${NC}"
    echo -e "     ${DIM}Then paste contents of: ~/.ssh/id_ed25519.pub${NC}"
    echo -e "  ${C}7.${NC} Hostname: ${DIM}claude-task-runner${NC}"
    echo -e "  ${C}8.${NC} Click ${W}Create Droplet${NC}"
    echo -e "  ${C}9.${NC} Copy the IP address shown"
    echo ""
    echo -e "  ${W}DNS:${NC} Go to your DNS provider and add an A record"
    echo -e "  pointing your domain (e.g. tasks.yourdomain.com)"
    echo -e "  to the server IP."
    echo ""
}

# ─── Main ────────────────────────────────────────────────────

print_banner

# Check for python3 or openssl (needed for key generation)
if ! command -v python3 &>/dev/null && ! command -v openssl &>/dev/null; then
    err "Need python3 or openssl for key generation"
    exit 1
fi

# ─── Phase 1: Server ────────────────────────────────────────

section "Server"

PREV_HOST=$(read_remote "host")
PREV_USER=$(read_remote "ssh_user")
PREV_DOMAIN=$(read_remote "domain")

if [ -n "$PREV_HOST" ]; then
    info "Previous config found: ${W}${PREV_USER}@${PREV_HOST}${NC} (${PREV_DOMAIN})"
    ask "Use these settings? ${DIM}[Y/n]${NC}: "
    read -r reuse
    if [[ ! "$reuse" =~ ^[Nn] ]]; then
        SERVER_HOST="$PREV_HOST"
        SSH_USER="$PREV_USER"
        DOMAIN="$PREV_DOMAIN"
    fi
fi

if [ -z "${SERVER_HOST:-}" ]; then
    ask "Do you already have a server set up? ${DIM}[y/N]${NC}: "
    read -r has_server

    if [[ ! "$has_server" =~ ^[Yy] ]]; then
        echo ""
        echo -e "  No worries! You'll need a small VPS with Docker installed."
        echo -e "  Here are three good, cheap options:"
        echo ""
        echo -e "    ${W}1.${NC} Hetzner     — ${G}\$4.50/mo${NC}  ${DIM}(best value)${NC}"
        echo -e "    ${W}2.${NC} Linode      — ${G}\$5/mo${NC}"
        echo -e "    ${W}3.${NC} DigitalOcean — ${G}\$4/mo${NC}"
        echo ""
        ask "See instructions for one of these? ${DIM}[1/2/3/skip]${NC}: "
        read -r provider_choice

        case "$provider_choice" in
            1) show_hetzner_instructions ;;
            2) show_linode_instructions ;;
            3) show_digitalocean_instructions ;;
            *) echo "" ;;
        esac

        echo -e "  ${DIM}Once your server is running and you have the IP address,"
        echo -e "  come back and run this wizard again.${NC}"
        echo ""
        ask "Ready to continue with a server IP? ${DIM}[y/N]${NC}: "
        read -r ready
        if [[ ! "$ready" =~ ^[Yy] ]]; then
            info "No problem — run ${W}./setup.sh${NC} again when you're ready."
            exit 0
        fi
        echo ""
    fi

    prompt_with_default "Server IP or hostname" "" SERVER_HOST
    prompt_with_default "SSH user" "root" SSH_USER
    prompt_with_default "Domain name (pointed at server)" "" DOMAIN
fi

if [ -z "$SERVER_HOST" ] || [ -z "$DOMAIN" ]; then
    err "Server host and domain are required"
    exit 1
fi

# Test SSH
echo ""
info "Testing SSH connection to ${W}${SSH_USER}@${SERVER_HOST}${NC}..."
if ssh -o ConnectTimeout=10 -o BatchMode=yes "${SSH_USER}@${SERVER_HOST}" "echo ok" &>/dev/null; then
    info "SSH connection ${G}OK${NC}"
else
    err "Cannot connect via SSH. Check that:"
    echo -e "    - Your SSH key is set up"
    echo -e "    - The server is running"
    echo -e "    - ssh ${SSH_USER}@${SERVER_HOST} works manually"
    exit 1
fi

# Check Docker is installed on server
info "Checking Docker on server..."
if ssh "${SSH_USER}@${SERVER_HOST}" "docker compose version" &>/dev/null; then
    info "Docker Compose ${G}OK${NC}"
else
    err "Docker Compose not found on server."
    echo -e "    SSH in and install it:"
    echo -e "    ${C}ssh ${SSH_USER}@${SERVER_HOST}${NC}"
    echo -e "    ${DIM}curl -fsSL https://get.docker.com | sh${NC}"
    exit 1
fi

# ─── Phase 2: Claude Authentication ─────────────────────────

section "Claude Authentication"

echo -e "  How does Claude Code authenticate on the server?"
echo ""
echo -e "    ${W}1.${NC} Claude Pro / Max / Enterprise subscription ${DIM}(recommended)${NC}"
echo -e "    ${W}2.${NC} Anthropic API key"
echo ""
ask "Choose ${DIM}[1/2]${NC}: "
read -r auth_choice

AUTH_METHOD="subscription"
ANTHROPIC_KEY=""

if [[ "$auth_choice" == "2" ]]; then
    AUTH_METHOD="api_key"
    echo ""
    echo -e "  ${DIM}Get a key at: https://console.anthropic.com/settings/keys${NC}"
    echo ""

    PREV_KEY=""
    if [ -f "$ENV_FILE" ]; then
        PREV_KEY=$(grep "^ANTHROPIC_API_KEY=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")
    fi

    if [ -n "$PREV_KEY" ]; then
        MASKED="${PREV_KEY:0:10}...${PREV_KEY: -4}"
        info "Current key: ${DIM}${MASKED}${NC}"
        ask "Keep this key? ${DIM}[Y/n]${NC}: "
        read -r keep_key
        if [[ "$keep_key" =~ ^[Nn] ]]; then
            PREV_KEY=""
        fi
    fi

    if [ -z "$PREV_KEY" ]; then
        ask "Anthropic API key: "
        read -rs ANTHROPIC_KEY
        echo ""
    else
        ANTHROPIC_KEY="$PREV_KEY"
    fi

    if [ -z "$ANTHROPIC_KEY" ]; then
        err "API key is required"
        exit 1
    fi
    info "API key set"
else
    info "Using Claude subscription"
    echo -e "  ${DIM}After deployment, you'll need to run a one-time login"
    echo -e "  command on the server to authenticate. We'll show you how.${NC}"
fi

# ─── Phase 3: Users ─────────────────────────────────────────

section "Users"

echo -e "  ${DIM}Each user gets an API key to submit tasks via claude.ai."
echo -e "  You can re-run this wizard later to add more users.${NC}"
echo ""

declare -a USER_NAMES=()
declare -a USER_DISPLAY_NAMES=()
declare -a USER_KEYS=()

# Load existing users if present
if [ -f "$USERS_FILE" ] && ! grep -q "CHANGE_ME" "$USERS_FILE"; then
    info "Existing users found in config/users.yaml"
    ask "Keep existing users and add more? ${DIM}[Y/n]${NC}: "
    read -r keep_users
    if [[ ! "$keep_users" =~ ^[Nn] ]]; then
        current_name=""
        while IFS= read -r line; do
            if echo "$line" | grep -qE '^\s{2}\w+:$'; then
                current_name=$(echo "$line" | sed 's/://;s/ //g')
            elif echo "$line" | grep -q "display_name:" && [ -n "$current_name" ]; then
                display=$(echo "$line" | sed 's/.*display_name: *"//;s/".*//')
                USER_NAMES+=("$current_name")
                USER_DISPLAY_NAMES+=("$display")
            elif echo "$line" | grep -q "api_key:" && [ -n "$current_name" ]; then
                key=$(echo "$line" | sed 's/.*api_key: *"//;s/".*//')
                USER_KEYS+=("$key")
                current_name=""
            fi
        done < "$USERS_FILE"

        for i in "${!USER_NAMES[@]}"; do
            MASKED="${USER_KEYS[$i]:0:8}...${USER_KEYS[$i]: -4}"
            info "  ${W}${USER_NAMES[$i]}${NC} (${USER_DISPLAY_NAMES[$i]}) — ${DIM}${MASKED}${NC}"
        done
        echo ""
    fi
fi

while true; do
    ask "Add a user? ${DIM}[y/N]${NC}: "
    read -r add_user
    if [[ ! "$add_user" =~ ^[Yy] ]]; then
        break
    fi

    prompt_with_default "  Username (short, no spaces)" "" new_name
    prompt_with_default "  Display name" "$new_name" new_display
    new_key=$(generate_key)

    USER_NAMES+=("$new_name")
    USER_DISPLAY_NAMES+=("$new_display")
    USER_KEYS+=("$new_key")

    info "  Added ${W}${new_name}${NC} — key: ${DIM}${new_key}${NC}"
    echo ""
done

if [ ${#USER_NAMES[@]} -eq 0 ]; then
    err "At least one user is required"
    exit 1
fi

# ─── Phase 4: Projects ──────────────────────────────────────

section "Projects"

echo -e "  ${DIM}Projects define what Claude Code can work on."
echo -e "  Each project maps to a directory on the server.${NC}"
echo ""

declare -a PROJ_NAMES=()
declare -a PROJ_DIRS=()
declare -a PROJ_DESCS=()
declare -a PROJ_USERS=()

# Load existing projects
if [ -f "$PROJECTS_FILE" ]; then
    if grep -q "directory:" "$PROJECTS_FILE" && ! grep -q "/home/deploy/apps/moment" "$PROJECTS_FILE"; then
        info "Existing projects found in config/projects.yaml"
        ask "Keep existing projects? ${DIM}[Y/n]${NC}: "
        read -r keep_proj
        if [[ "$keep_proj" =~ ^[Nn] ]]; then
            : # Will overwrite
        else
            info "Keeping existing projects. Add more below."
        fi
    fi
fi

while true; do
    ask "Add a project? ${DIM}[y/N]${NC}: "
    read -r add_proj
    if [[ ! "$add_proj" =~ ^[Yy] ]]; then
        break
    fi

    prompt_with_default "  Project name (short, no spaces)" "" proj_name
    prompt_with_default "  Directory on server" "/home/${SSH_USER}/apps/${proj_name}" proj_dir
    prompt_with_default "  Description" "" proj_desc

    echo -e "  ${DIM}Which users can submit tasks to this project?${NC}"
    proj_users=""
    for i in "${!USER_NAMES[@]}"; do
        ask "  Allow ${W}${USER_NAMES[$i]}${NC}? ${DIM}[Y/n]${NC}: "
        read -r allow
        if [[ ! "$allow" =~ ^[Nn] ]]; then
            if [ -n "$proj_users" ]; then
                proj_users="${proj_users},${USER_NAMES[$i]}"
            else
                proj_users="${USER_NAMES[$i]}"
            fi
        fi
    done

    PROJ_NAMES+=("$proj_name")
    PROJ_DIRS+=("$proj_dir")
    PROJ_DESCS+=("$proj_desc")
    PROJ_USERS+=("$proj_users")

    info "  Added project ${W}${proj_name}${NC} → ${proj_dir}"
    echo ""
done

if [ ${#PROJ_NAMES[@]} -eq 0 ]; then
    warn "No projects configured. You can add them later by re-running this wizard."
fi

# ─── Phase 5: Generate Config Files ─────────────────────────

section "Generating Config"

mkdir -p "$SCRIPT_DIR/config"

# .env
{
    echo "DOMAIN=${DOMAIN}"
    echo "SERVER_URL=https://${DOMAIN}"
    echo "MAX_CONCURRENT=2"
    echo "DEFAULT_TIMEOUT_MINUTES=60"
    if [ -n "$ANTHROPIC_KEY" ]; then
        echo "ANTHROPIC_API_KEY=${ANTHROPIC_KEY}"
    fi
} > "$ENV_FILE"
info "Written .env"

# config/users.yaml
cat > "$USERS_FILE" <<EOF
users:
EOF
for i in "${!USER_NAMES[@]}"; do
    cat >> "$USERS_FILE" <<EOF
  ${USER_NAMES[$i]}:
    display_name: "${USER_DISPLAY_NAMES[$i]}"
    api_key: "${USER_KEYS[$i]}"
EOF
done
info "Written config/users.yaml"

# config/projects.yaml
cat > "$PROJECTS_FILE" <<EOF
projects:
EOF
for i in "${!PROJ_NAMES[@]}"; do
    cat >> "$PROJECTS_FILE" <<EOF
  ${PROJ_NAMES[$i]}:
    directory: ${PROJ_DIRS[$i]}
    description: "${PROJ_DESCS[$i]}"
    allowed_users:
EOF
    IFS=',' read -ra users_arr <<< "${PROJ_USERS[$i]}"
    for u in "${users_arr[@]}"; do
        echo "      - ${u}" >> "$PROJECTS_FILE"
    done
done
info "Written config/projects.yaml"

# Caddyfile
cat > "$SCRIPT_DIR/Caddyfile" <<EOF
${DOMAIN} {
    reverse_proxy task-runner:8080
}
EOF
info "Written Caddyfile"

# .remote.yaml
cat > "$REMOTE_FILE" <<EOF
host: ${SERVER_HOST}
ssh_user: ${SSH_USER}
domain: ${DOMAIN}
auth_method: ${AUTH_METHOD}
EOF
info "Written .remote.yaml"

# ─── Phase 6: Deploy ────────────────────────────────────────

section "Deploying to ${SERVER_HOST}"

REMOTE_DIR="/home/${SSH_USER}/claude-task-runner"
if [ "$SSH_USER" = "root" ]; then
    REMOTE_DIR="/root/claude-task-runner"
fi

info "Creating directory on server..."
ssh "${SSH_USER}@${SERVER_HOST}" "mkdir -p ${REMOTE_DIR}/config ${REMOTE_DIR}/tasks ${REMOTE_DIR}/logs"

info "Copying files..."
scp -q \
    "$SCRIPT_DIR/server.py" \
    "$SCRIPT_DIR/runner.py" \
    "$SCRIPT_DIR/entrypoint.sh" \
    "$SCRIPT_DIR/requirements.txt" \
    "$SCRIPT_DIR/Dockerfile" \
    "$SCRIPT_DIR/docker-compose.yml" \
    "$SCRIPT_DIR/Caddyfile" \
    "$SCRIPT_DIR/.env" \
    "${SSH_USER}@${SERVER_HOST}:${REMOTE_DIR}/"

scp -q \
    "$SCRIPT_DIR/config/users.yaml" \
    "$SCRIPT_DIR/config/projects.yaml" \
    "${SSH_USER}@${SERVER_HOST}:${REMOTE_DIR}/config/"

info "Setting permissions on secrets..."
ssh "${SSH_USER}@${SERVER_HOST}" "chmod 600 ${REMOTE_DIR}/.env ${REMOTE_DIR}/config/users.yaml"

info "Building and starting containers..."
ssh "${SSH_USER}@${SERVER_HOST}" "cd ${REMOTE_DIR} && docker compose up -d --build" 2>&1 | while read -r line; do
    echo -e "    ${DIM}${line}${NC}"
done

# ─── Phase 7: Claude Login (subscription only) ──────────────

if [ "$AUTH_METHOD" = "subscription" ]; then
    section "Claude Login"

    echo -e "  ${W}One last step!${NC} You need to authenticate Claude Code"
    echo -e "  on the server using your Pro/Max/Enterprise subscription."
    echo ""
    echo -e "  Run this command:"
    echo ""
    echo -e "    ${C}ssh ${SSH_USER}@${SERVER_HOST} 'cd ${REMOTE_DIR} && docker compose exec task-runner claude setup-token'${NC}"
    echo ""
    echo -e "  ${DIM}This generates a long-lived auth token inside the container."
    echo -e "  You only need to do this once — the token persists across restarts.${NC}"
    echo ""
    ask "Want to do this now? ${DIM}[Y/n]${NC}: "
    read -r do_login

    if [[ ! "$do_login" =~ ^[Nn] ]]; then
        echo ""
        info "Running claude setup-token on server..."
        echo -e "  ${DIM}Follow the prompts below:${NC}"
        echo ""
        ssh -t "${SSH_USER}@${SERVER_HOST}" "cd ${REMOTE_DIR} && docker compose exec task-runner claude setup-token"
        echo ""
        info "Authentication complete"
    else
        echo ""
        warn "Remember to run the command above before submitting tasks."
    fi
fi

# ─── Phase 8: Summary ───────────────────────────────────────

section "Setup Complete"

echo -e "  ${G}Your Claude Task Runner is live!${NC}"
echo ""
echo -e "  ${W}Server:${NC}  https://${DOMAIN}"
echo -e "  ${W}Auth:${NC}    $([ "$AUTH_METHOD" = "subscription" ] && echo "Claude subscription" || echo "API key")"
echo ""
echo -e "  ${W}Users:${NC}"
for i in "${!USER_NAMES[@]}"; do
    echo -e "    ${USER_NAMES[$i]}  →  ${DIM}${USER_KEYS[$i]}${NC}"
done
echo ""
if [ ${#PROJ_NAMES[@]} -gt 0 ]; then
    echo -e "  ${W}Projects:${NC}"
    for i in "${!PROJ_NAMES[@]}"; do
        echo -e "    ${PROJ_NAMES[$i]}  →  ${PROJ_DIRS[$i]}"
    done
    echo ""
fi

echo -e "  ${M}── Connect to claude.ai ──${NC}"
echo ""
echo -e "  1. Go to ${C}claude.ai${NC} → Settings → Connectors"
echo -e "  2. Add MCP Server"
echo -e "  3. URL: ${W}https://${DOMAIN}/${NC}"
echo -e "  4. Auth: Bearer token → paste your API key from above"
echo ""

echo -e "  ${M}── Useful commands ──${NC}"
echo ""
echo -e "  ${DIM}# Check status${NC}"
echo -e "  ssh ${SSH_USER}@${SERVER_HOST} 'cd ${REMOTE_DIR} && docker compose logs -f'"
echo ""
echo -e "  ${DIM}# Re-run this wizard to update config${NC}"
echo -e "  ./setup.sh"
echo ""

echo -e "  ${Y}!${NC} ${W}Security reminders:${NC}"
echo -e "  ${DIM}- This system executes AI-generated code on your server"
echo -e "  - API keys grant ability to run arbitrary prompts — treat as passwords"
echo -e "  - Review logs regularly${NC}"
echo ""
