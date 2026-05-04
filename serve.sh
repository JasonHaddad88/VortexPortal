#!/data/data/com.termux/files/usr/bin/bash
# Vortex Termux launcher.
#
#   MODE=agent (default)  Runs the agent, connecting outbound to a hub.
#   MODE=hub              Runs the hub (uvicorn + cloudflared quick tunnel).
#
# First-run pairing for agent mode:
#   PAIRING_CODE=123456 HUB_URL=https://abc.trycloudflare.com bash serve.sh
# Once paired, the device_id + token are saved to ~/.vortex_agent/config.json
# and subsequent runs reconnect automatically.

set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/server}"
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"
MODE="${MODE:-agent}"
APP_PORT="${APP_PORT:-8000}"
SSH_PORT="${SSH_PORT:-8022}"

mkdir -p "$LOG_DIR"

cleanup() {
    echo "==> Shutting down"
    pkill -P $$ 2>/dev/null || true
    pkill -f "uvicorn hub.app:app" 2>/dev/null || true
    pkill -f "agent.agent" 2>/dev/null || true
    pkill -f "cloudflared tunnel" 2>/dev/null || true
    pkill sshd 2>/dev/null || true
    termux-wake-unlock 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ----------------------------------------------------------------------------
# Preflight: install whatever's missing.
# ----------------------------------------------------------------------------
need() {
    local cmd="$1" pkg_name="${2:-$1}"
    command -v "$cmd" >/dev/null 2>&1 && return 0
    echo "==> Installing missing dep: $pkg_name"
    if ! pkg install -y "$pkg_name"; then
        yes | pkg update -y || true
        pkg install -y "$pkg_name"
    fi
}

echo "==> Checking dependencies"
need python python
need pip python-pip
if [ "$MODE" = "hub" ]; then
    need cloudflared cloudflared
    need curl curl
fi

# ----------------------------------------------------------------------------
# Python venv. Pure-Python pins to avoid Rust builds on ARM.
# ----------------------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    echo "==> Building venv at $VENV"
    mkdir -p "$APP_DIR"
    python -m venv "$VENV"
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    pip install --quiet --upgrade pip setuptools wheel
    if [ "$MODE" = "hub" ]; then
        pip install --quiet "fastapi<0.100" "pydantic<2" uvicorn websockets httpx python-multipart
    else
        pip install --quiet websockets httpx
    fi
    deactivate
fi

# Top up missing deps if the venv predates new requirements.
if [ "$MODE" = "hub" ]; then
    "$VENV/bin/python" -c 'import fastapi, uvicorn, websockets, httpx, pydantic, multipart' 2>/dev/null \
        || "$VENV/bin/pip" install --quiet "fastapi<0.100" "pydantic<2" uvicorn websockets httpx python-multipart
else
    "$VENV/bin/python" -c 'import websockets, httpx' 2>/dev/null \
        || "$VENV/bin/pip" install --quiet websockets httpx
fi

# ----------------------------------------------------------------------------
# Code: copy hub/ + agent/ from script dir into $APP_DIR if needed.
# ----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for d in agent hub; do
    if [ -d "$SCRIPT_DIR/$d" ] && [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
        cp -r "$SCRIPT_DIR/$d" "$APP_DIR/"
    fi
done

cd "$APP_DIR"

# ----------------------------------------------------------------------------
# Acquire wake lock so Android doesn't doze the connection.
# ----------------------------------------------------------------------------
echo "==> Acquiring wake lock"
termux-wake-lock 2>/dev/null || true

# ----------------------------------------------------------------------------
# Mode dispatch
# ----------------------------------------------------------------------------
if [ "$MODE" = "hub" ]; then
    if command -v sshd >/dev/null 2>&1; then
        echo "==> Starting sshd on port $SSH_PORT"
        pkill sshd 2>/dev/null || true
        sshd
    fi

    echo "==> Starting Vortex Hub on 127.0.0.1:$APP_PORT"
    nohup "$VENV/bin/uvicorn" hub.app:app \
        --host 127.0.0.1 --port "$APP_PORT" \
        --proxy-headers --forwarded-allow-ips='*' \
        > "$LOG_DIR/uvicorn.log" 2>&1 &
    UVICORN_PID=$!

    # Wait for /health
    for _ in $(seq 1 30); do
        curl -fsS "http://127.0.0.1:$APP_PORT/health" >/dev/null 2>&1 && break
        sleep 0.5
    done

    echo "==> Starting Cloudflare quick tunnel"
    cloudflared tunnel --no-autoupdate \
        --url "http://127.0.0.1:$APP_PORT" \
        > "$LOG_DIR/cloudflared.log" 2>&1 &
    CF_PID=$!

    PUBLIC_URL=""
    for _ in $(seq 1 60); do
        PUBLIC_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' \
            "$LOG_DIR/cloudflared.log" 2>/dev/null | head -n1 || true)
        [ -n "$PUBLIC_URL" ] && break
        sleep 1
    done

    LAN_IP=$(ip route get 1.1.1.1 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)

    echo
    echo "============================================================"
    echo "  Public URL : ${PUBLIC_URL:-<see logs/cloudflared.log>}"
    echo "  LAN URL    : http://${LAN_IP:-<wifi-ip>}:$APP_PORT"
    echo "  Logs       : $LOG_DIR/"
    echo "============================================================"
    echo "Press Ctrl+C to stop."
    wait "$UVICORN_PID" "$CF_PID"

else
    # Agent mode — connect outbound to the hub.
    echo "==> Starting Vortex Agent"
    exec "$VENV/bin/python" -m agent.agent
fi
