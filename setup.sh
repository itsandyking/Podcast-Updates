#!/usr/bin/env bash
# setup.sh — configure Podcast-Updates on Pi (Linux) or Mac (Darwin)
# Safe to re-run: skips steps already done, never duplicates cron/launchd entries.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$REPO_DIR/.venv"
ENV_FILE="$REPO_DIR/config/.env"

OS="$(uname -s)"   # Darwin or Linux
ARCH="$(uname -m)" # arm64 / aarch64 / x86_64

# ── colours ──────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    CYAN='\033[0;36m'; RED='\033[0;31m'; RESET='\033[0m'
else
    BOLD=''; GREEN=''; YELLOW=''; CYAN=''; RED=''; RESET=''
fi

info()    { echo -e "${CYAN}▸ $*${RESET}"; }
success() { echo -e "${GREEN}✓ $*${RESET}"; }
warn()    { echo -e "${YELLOW}! $*${RESET}"; }
error()   { echo -e "${RED}✗ $*${RESET}" >&2; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }

# ── helpers ───────────────────────────────────────────────────────────────────

# Read a value from config/.env (returns empty string if not set)
env_get() {
    local key="$1"
    if [ -f "$ENV_FILE" ]; then
        grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2- | tr -d '"' || true
    fi
}

# Set or update a key in config/.env
env_set() {
    local key="$1" val="$2"
    mkdir -p "$(dirname "$ENV_FILE")"
    if [ -f "$ENV_FILE" ] && grep -qE "^${key}=" "$ENV_FILE"; then
        # Replace in-place (compatible with both GNU and BSD sed)
        sed -i.bak "s|^${key}=.*|${key}=${val}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

# Prompt with an existing-value default; skips prompt if already set and --yes flag given
prompt() {
    local key="$1" prompt_text="$2" default secret="${4:-}"
    default="$(env_get "$key")"

    if [ -n "$default" ] && [ "${AUTO_YES:-}" = "1" ]; then
        return  # already configured, non-interactive mode
    fi

    local display_default=""
    if [ -n "$default" ]; then
        if [ -n "$secret" ]; then
            display_default=" [****]"
        else
            display_default=" [$default]"
        fi
    fi

    local value
    if [ -n "$secret" ]; then
        read -rsp "  ${prompt_text}${display_default}: " value; echo
    else
        read -rp  "  ${prompt_text}${display_default}: " value
    fi

    value="${value:-$default}"
    if [ -n "$value" ]; then
        env_set "$key" "$value"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
header "=== Podcast-Updates setup ==="
echo "Repo:   $REPO_DIR"
echo "OS:     $OS ($ARCH)"

# ── 1. Python ─────────────────────────────────────────────────────────────────
header "1. Python"

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver="$("$candidate" -c 'import sys; print(sys.version_info[:2])')"
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PYTHON="$(command -v "$candidate")"
            success "Using $PYTHON ($ver)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    error "Python 3.11+ not found. Install it and re-run setup."
    exit 1
fi

# ── 2. Virtual environment + dependencies ─────────────────────────────────────
header "2. Virtual environment"

if [ ! -d "$VENV" ]; then
    info "Creating venv at $VENV"
    "$PYTHON" -m venv "$VENV"
    success "Venv created"
else
    success "Venv already exists"
fi

PIP="$VENV/bin/pip"
info "Installing/updating dependencies"

if [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
    # Apple Silicon: use mlx-whisper (Neural Engine + GPU)
    "$PIP" install -q --upgrade pip
    "$PIP" install -q -e "$REPO_DIR[mlx]"
    success "Installed with mlx-whisper (Apple Silicon)"
else
    # Pi or Intel Mac: use faster-whisper (CPU)
    "$PIP" install -q --upgrade pip
    "$PIP" install -q -e "$REPO_DIR[whisper]"
    success "Installed with faster-whisper"
fi

# ── 3. Shows config: set mlx-whisper engine on Mac ───────────────────────────
if [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
    header "3. Transcription engine"
    info "Checking shows.yaml files for mlx-whisper..."
    for cfg in shows.yaml shows_tech.yaml shows_finance.yaml shows_parenting.yaml; do
        cfg_path="$REPO_DIR/config/$cfg"
        [ -f "$cfg_path" ] || continue
        if grep -q "engine: faster-whisper" "$cfg_path"; then
            sed -i.bak "s/engine: faster-whisper/engine: mlx-whisper/" "$cfg_path"
            rm -f "${cfg_path}.bak"
            success "  $cfg → engine: mlx-whisper"
        elif grep -q "engine: mlx-whisper" "$cfg_path"; then
            success "  $cfg already uses mlx-whisper"
        fi
    done
fi

# ── 4. Email credentials ───────────────────────────────────────────────────────
header "4. Email credentials (Gmail SMTP)"

if [ ! -f "$ENV_FILE" ]; then
    cp "$REPO_DIR/config/.env.example" "$ENV_FILE"
fi

# Check if all values are already configured
all_set=1
for key in SMTP_USER SMTP_PASSWORD EMAIL_TO; do
    [ -z "$(env_get "$key")" ] && all_set=0 && break
done

if [ "$all_set" = "1" ] && [ "${AUTO_YES:-}" = "1" ]; then
    success "Email already configured ($(env_get SMTP_USER))"
else
    echo "  Gmail app password: myaccount.google.com → Security → 2-Step → App passwords"
    prompt "SMTP_USER"     "Gmail address"
    prompt "SMTP_PASSWORD" "App password (16 chars)" "" secret
    prompt "EMAIL_TO"      "Deliver briefings to"
    success "Email configured"
fi

# ── 5. Data directories ────────────────────────────────────────────────────────
header "5. Data directories"
mkdir -p "$REPO_DIR/data/logs" "$REPO_DIR/data/transcripts" \
         "$REPO_DIR/data/briefings" "$REPO_DIR/data/episode_ledger" \
         "$REPO_DIR/data/themes"
success "data/ directories ready"

# ── 6. Claude CLI check ────────────────────────────────────────────────────────
header "6. Claude CLI"
CLAUDE_BIN=""
for candidate in \
    "$(command -v claude 2>/dev/null || true)" \
    "$HOME/.local/bin/claude" \
    "/usr/local/bin/claude"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] && CLAUDE_BIN="$candidate" && break
done

if [ -n "$CLAUDE_BIN" ]; then
    success "Found claude at $CLAUDE_BIN"
else
    warn "claude CLI not found — analysis step will be skipped until installed."
    echo "  Install: https://claude.ai/download  (or 'npm install -g @anthropic-ai/claude-code')"
fi

# ── 7. Scheduler ──────────────────────────────────────────────────────────────
header "7. Scheduler"

VENV_BIN="$VENV/bin"

if [ "$OS" = "Linux" ]; then
    # ── Pi / Linux: cron ──────────────────────────────────────────────────────

    LOGDIR="$REPO_DIR/data/logs"

    # Each entry: "guard_string|cron_line"
    # Guard string is grepped against existing crontab to detect duplicates.
    CRON_ENTRIES=(
        "podcast-updates --news|0 7 * * 1-6 ${VENV_BIN}/podcast-updates >> ${LOGDIR}/news.log 2>&1"
        "podcast-updates --tech|0 17 * * 5 ${VENV_BIN}/podcast-updates --config ${REPO_DIR}/config/shows_tech.yaml >> ${LOGDIR}/tech.log 2>&1"
        "podcast-updates --finance|0 9 * * 6 ${VENV_BIN}/podcast-updates --config ${REPO_DIR}/config/shows_finance.yaml >> ${LOGDIR}/finance.log 2>&1"
        "podcast-updates --parenting|0 6 * * 2 ${VENV_BIN}/podcast-updates --config ${REPO_DIR}/config/shows_parenting.yaml >> ${LOGDIR}/parenting.log 2>&1"
        "podcast-watch|@reboot ${VENV_BIN}/podcast-watch >> ${LOGDIR}/watcher.log 2>&1"
    )

    # Read current crontab (empty string if none)
    CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
    NEW_CRON="$CURRENT_CRON"

    for entry in "${CRON_ENTRIES[@]}"; do
        guard="${entry%%|*}"
        line="${entry#*|}"

        if echo "$CURRENT_CRON" | grep -qF "$guard"; then
            success "  Already in crontab: $guard"
        else
            NEW_CRON="${NEW_CRON}
${line}  # ${guard}"
            info "  Adding to crontab: $guard"
        fi
    done

    # Write updated crontab only if changed
    if [ "$NEW_CRON" != "$CURRENT_CRON" ]; then
        echo "$NEW_CRON" | crontab -
        success "Crontab updated"
    else
        success "Crontab already up to date"
    fi

    warn "Watcher (@reboot) starts on next reboot. Start it now with:"
    echo "    nohup ${VENV_BIN}/podcast-watch >> ${LOGDIR}/watcher.log 2>&1 &"

elif [ "$OS" = "Darwin" ]; then
    # ── Mac: launchd ──────────────────────────────────────────────────────────

    LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
    LAUNCHD_SRC="$REPO_DIR/launchd"
    mkdir -p "$LAUNCH_AGENTS"

    for plist_src in "$LAUNCHD_SRC"/com.podcast-updates.*.plist; do
        label="$(basename "$plist_src" .plist)"
        plist_dst="$LAUNCH_AGENTS/${label}.plist"

        # Substitute USERNAME with actual username
        sed "s|/Users/USERNAME|$HOME|g" "$plist_src" > "$plist_dst"

        # Unload first (ignore errors if not loaded)
        launchctl unload "$plist_dst" 2>/dev/null || true
        launchctl load "$plist_dst"
        success "  Loaded $label"
    done

    success "All launchd agents loaded"
fi

# ── 8. Syncthing reminder (first-time only) ───────────────────────────────────
if [ ! -f "$REPO_DIR/.syncthing-configured" ]; then
    header "8. Syncthing (cross-machine sync)"
    echo ""
    echo "  To sync transcripts, ledgers, and briefings between Pi and Mac:"
    echo ""
    echo "  1. Install Syncthing on both machines:"
    echo "       Pi:  sudo apt install syncthing"
    echo "       Mac: brew install syncthing  (or https://syncthing.net)"
    echo ""
    echo "  2. Open the web UI on each machine (http://localhost:8384)"
    echo "  3. Add each machine as a remote device"
    echo "  4. Share the folder: $REPO_DIR"
    echo "     Using the .stignore already in the repo (excludes audio/, .venv/, etc.)"
    echo ""
    echo "  Run 'touch $REPO_DIR/.syncthing-configured' to suppress this message."
fi

# ── Done ──────────────────────────────────────────────────────────────────────
header "=== Setup complete ==="
echo ""
echo "  Test a pipeline run:"
echo "    ${VENV_BIN}/podcast-updates 2025-01-01   # re-run a specific date"
echo "    ${VENV_BIN}/podcast-analyze 2025-01-01   # re-run analysis only"
echo ""
echo "  Start the watcher now:"
echo "    ${VENV_BIN}/podcast-watch &"
echo ""
if [ "$OS" = "Darwin" ]; then
    echo "  Check launchd agents:"
    echo "    launchctl list | grep podcast"
    echo ""
fi
