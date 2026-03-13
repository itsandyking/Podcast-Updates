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

# ── 3. Transcription engine (per-machine, via .env.local — not synced) ────────
header "3. Transcription engine"
LOCAL_ENV="$REPO_DIR/config/.env.local"
if [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
    engine="mlx-whisper"
else
    engine="faster-whisper"
fi
mkdir -p "$(dirname "$LOCAL_ENV")"
echo "# Machine-specific overrides (not synced via Syncthing)
TRANSCRIPTION_ENGINE=$engine" > "$LOCAL_ENV"
success "TRANSCRIPTION_ENGINE=$engine (set in config/.env.local)"

# ── 4. Email credentials ───────────────────────────────────────────────────────
header "4. Email credentials (iCloud SMTP)"

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
    echo "  App-specific password: appleid.apple.com → Sign-In & Security → App-Specific Passwords"
    prompt "SMTP_USER"     "Apple ID email"
    prompt "SMTP_PASSWORD" "App-specific password (xxxx-xxxx-xxxx-xxxx)" "" secret
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

LOGDIR="$REPO_DIR/data/logs"

# ── 7. Auto-sync (git pull on new commits) ────────────────────────────────────
header "7. Auto-sync"

if [ "$OS" = "Linux" ]; then
    SYNC_GUARD="scripts/sync.sh"
    CURRENT_CRON_SYNC="$(crontab -l 2>/dev/null || true)"
    if echo "$CURRENT_CRON_SYNC" | grep -qF "$SYNC_GUARD"; then
        success "Auto-sync already in crontab"
    else
        # Append outside the managed pipeline block — it's independent
        ( echo "$CURRENT_CRON_SYNC"; echo "*/5 * * * * $REPO_DIR/scripts/sync.sh" ) | crontab -
        success "Auto-sync added to crontab (every 5 min)"
    fi
elif [ "$OS" = "Darwin" ]; then
    SYNC_PLIST="$HOME/Library/LaunchAgents/com.podcast-updates.sync.plist"
    cat > "$SYNC_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Generated by setup.sh — re-run setup.sh to update -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.podcast-updates.sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>${REPO_DIR}/scripts/sync.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
  <key>StartInterval</key>
  <integer>300</integer>
  <key>StandardOutPath</key>
  <string>${LOGDIR}/sync.log</string>
  <key>StandardErrorPath</key>
  <string>${LOGDIR}/sync.log</string>
</dict>
</plist>
PLIST
    launchctl unload "$SYNC_PLIST" 2>/dev/null || true
    launchctl load "$SYNC_PLIST"
    success "Auto-sync launchd agent loaded (every 5 min)"
fi

# ── 8. Scheduler ──────────────────────────────────────────────────────────────
header "8. Scheduler"

VENV_BIN="$VENV/bin"

# Read schedule.cron from a shows.yaml file using Python (single source of truth).
# Returns empty string if the key is absent (e.g. a future pipeline with no schedule yet).
yaml_cron() {
    local cfg="$1"
    "$VENV_BIN/python" -c "
import yaml, sys
raw = yaml.safe_load(open('$cfg'))
print(raw.get('schedule', {}).get('cron', ''))
" 2>/dev/null || true
}

# Collect every shows_*.yaml plus shows.yaml, sorted by name
config_files=()
for cfg in "$REPO_DIR/config/shows.yaml" "$REPO_DIR/config"/shows_*.yaml; do
    [ -f "$cfg" ] && config_files+=("$cfg")
done

if [ "$OS" = "Linux" ]; then
    # ── Pi / Linux: cron ──────────────────────────────────────────────────────
    # Build the managed block fresh from config each run — fully idempotent for
    # schedule changes, new pipelines, and deleted pipelines.

    MARKER_BEGIN="# BEGIN podcast-updates (managed by setup.sh — do not edit)"
    MARKER_END="# END podcast-updates"

    # Generate the managed block
    BLOCK="$MARKER_BEGIN"$'\n'
    BLOCK+="@reboot ${VENV_BIN}/podcast-watch >> ${LOGDIR}/watcher_pi.log 2>&1"$'\n'

    for cfg in "${config_files[@]}"; do
        cron_expr="$(yaml_cron "$cfg")"
        [ -z "$cron_expr" ] && continue

        name="$(basename "$cfg" .yaml | sed 's/shows_//')"
        [ "$name" = "shows" ] && name="news"
        logfile="${LOGDIR}/${name}.log"

        if [ "$cfg" = "$REPO_DIR/config/shows.yaml" ]; then
            line="${cron_expr} ${VENV_BIN}/podcast-updates >> ${logfile} 2>&1"
        else
            line="${cron_expr} ${VENV_BIN}/podcast-updates --config ${cfg} >> ${logfile} 2>&1"
        fi

        BLOCK+="${line}"$'\n'
        info "  $name: $cron_expr"
    done

    BLOCK+="$MARKER_END"

    # Replace the managed block in the existing crontab (or append if first time)
    CURRENT_CRON="$(crontab -l 2>/dev/null || true)"

    if echo "$CURRENT_CRON" | grep -qF "$MARKER_BEGIN"; then
        # Remove old managed block, insert new one
        NEW_CRON="$(echo "$CURRENT_CRON" | \
            sed "/^# BEGIN podcast-updates/,/^# END podcast-updates/d")"
        NEW_CRON="${NEW_CRON}"$'\n'"${BLOCK}"
    else
        NEW_CRON="${CURRENT_CRON}"$'\n'"${BLOCK}"
    fi

    echo "$NEW_CRON" | crontab -
    success "Crontab updated (${#config_files[@]} pipeline(s) + watcher)"

    warn "Watcher (@reboot) starts on next reboot. Start it now with:"
    echo "    nohup ${VENV_BIN}/podcast-watch >> ${LOGDIR}/watcher_pi.log 2>&1 &"

elif [ "$OS" = "Darwin" ]; then
    # ── Mac: launchd ──────────────────────────────────────────────────────────
    # Plists are regenerated from config each run — picks up schedule changes
    # and new pipelines automatically.

    LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
    mkdir -p "$LAUNCH_AGENTS"

    # Generate a plist for each config file from its schedule.cron value
    for cfg in "${config_files[@]}"; do
        name="$(basename "$cfg" .yaml | sed 's/shows_//')"
        [ "$name" = "shows" ] && name="news"
        label="com.podcast-updates.${name}"
        plist_dst="$LAUNCH_AGENTS/${label}.plist"
        logfile="${LOGDIR}/${name}.log"

        if [ "$cfg" = "$REPO_DIR/config/shows.yaml" ]; then
            prog_args="<string>${VENV_BIN}/podcast-updates</string>"
        else
            prog_args="<string>${VENV_BIN}/podcast-updates</string>
    <string>--config</string>
    <string>${cfg}</string>"
        fi

        cron_expr="$(yaml_cron "$cfg")"

        # Convert cron expression to launchd StartCalendarInterval dicts
        if [ -n "$cron_expr" ]; then
            cal_interval="$("$VENV_BIN/python" - "$cron_expr" <<'PYEOF'
import sys
fields = sys.argv[1].split()
minute, hour, _, _, weekdays = fields

keys = {"Minute": minute, "Hour": hour}
wd_map = {"0":"0","1":"1","2":"2","3":"3","4":"4","5":"5","6":"6"}

if weekdays == "*":
    days = []
elif "-" in weekdays:
    lo, hi = weekdays.split("-")
    days = list(range(int(lo), int(hi)+1))
elif "," in weekdays:
    days = [int(d) for d in weekdays.split(",")]
else:
    days = [int(weekdays)]

if not days:
    # Every day
    print(f"  <dict><key>Hour</key><integer>{hour}</integer>"
          f"<key>Minute</key><integer>{minute}</integer></dict>")
else:
    for d in days:
        print(f"  <dict><key>Weekday</key><integer>{d}</integer>"
              f"<key>Hour</key><integer>{hour}</integer>"
              f"<key>Minute</key><integer>{minute}</integer></dict>")
PYEOF
)"
        fi

        if [ -n "$cron_expr" ]; then
            schedule_xml="  <key>StartCalendarInterval</key>
  <array>
${cal_interval}
  </array>"
        else
            schedule_xml="  <!-- no schedule.cron in config -->"
        fi

        cat > "$plist_dst" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Generated by setup.sh from $(basename "$cfg") — re-run setup.sh to update -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    ${prog_args}
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
${schedule_xml}
  <key>StandardOutPath</key>
  <string>${logfile}</string>
  <key>StandardErrorPath</key>
  <string>${logfile}</string>
</dict>
</plist>
PLIST

        launchctl unload "$plist_dst" 2>/dev/null || true
        launchctl load "$plist_dst"
        success "  Loaded $label"
    done

    # Watcher plist (KeepAlive daemon, no schedule)
    watcher_dst="$LAUNCH_AGENTS/com.podcast-updates.watch.plist"
    cat > "$watcher_dst" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Generated by setup.sh — re-run setup.sh to update -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.podcast-updates.watch</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VENV_BIN}/podcast-watch</string>
    <string>--interval</string>
    <string>900</string>
    <string>--concurrency</string>
    <string>4</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>${LOGDIR}/watcher_mac.log</string>
  <key>StandardErrorPath</key>
  <string>${LOGDIR}/watcher_mac.log</string>
</dict>
</plist>
PLIST

    launchctl unload "$watcher_dst" 2>/dev/null || true
    launchctl load "$watcher_dst"
    success "  Loaded com.podcast-updates.watch"

    success "All launchd agents loaded (${#config_files[@]} pipeline(s) + watcher)"
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
