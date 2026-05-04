#!/data/data/com.termux/files/usr/bin/bash
# Boot the phone-as-a-server: optional sshd on LAN, FastAPI on localhost,
# Cloudflare Tunnel that exposes it to the entire internet.
#
# Self-healing: if python/cloudflared/the venv are missing it installs them
# on the spot, so this works on a fresh Termux as long as setup.sh has run
# at least once to create ~/server/.env and ~/server/app.py.

set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/server}"
APP_MODULE="${APP_MODULE:-app:app}"     # uvicorn target (module:variable)
APP_PORT="${APP_PORT:-8000}"
SSH_PORT="${SSH_PORT:-8022}"            # Termux's unprivileged sshd port
TUNNEL_NAME="${TUNNEL_NAME:-}"          # named CF tunnel; empty = quick tunnel

LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"

cleanup() {
    echo "==> Shutting down"
    pkill -P $$ 2>/dev/null || true
    pkill -f "uvicorn $APP_MODULE" 2>/dev/null || true
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
        echo "    Refreshing pkg lists and retrying..."
        yes | pkg update -y || true
        pkg install -y "$pkg_name"
    fi
}

echo "==> Checking dependencies"
need python python
need pip   python-pip
need curl  curl
need cloudflared cloudflared
# sshd is optional. Try to install, but a failure here doesn't stop the show.
command -v sshd >/dev/null 2>&1 || pkg install -y openssh 2>/dev/null \
    || echo "    (openssh missing; LAN SSH will be disabled)"

# Build the FastAPI venv if it's missing or incomplete. Pure-Python pins
# match setup.sh — see that file for the rationale on Pydantic v1 / no [standard].
if [ ! -x "$APP_DIR/.venv/bin/uvicorn" ]; then
    echo "==> Building Python venv at $APP_DIR/.venv"
    mkdir -p "$APP_DIR"
    (
        cd "$APP_DIR"
        python -m venv .venv
        # shellcheck disable=SC1091
        source .venv/bin/activate
        pip install --quiet --upgrade pip setuptools wheel
        # httpx (V1.2+) is needed for the multi-device proxy. Pure Python.
        pip install --quiet "fastapi<0.100" "pydantic<2" uvicorn httpx
    )
fi

# Top up the venv if it predates V1.2 (missing httpx).
if [ -x "$APP_DIR/.venv/bin/uvicorn" ] \
   && ! "$APP_DIR/.venv/bin/python" -c 'import httpx' >/dev/null 2>&1; then
    echo "==> Adding httpx to existing venv (V1.2 dep)"
    (
        # shellcheck disable=SC1091
        source "$APP_DIR/.venv/bin/activate"
        pip install --quiet httpx
    )
fi

# These need user input — defer to setup.sh.
if [ ! -f "$APP_DIR/.env" ] || [ ! -f "$APP_DIR/app.py" ]; then
    echo
    echo "ERROR: $APP_DIR/.env or app.py is missing."
    echo "       Run 'bash setup.sh' once to set credentials and write the app."
    exit 1
fi

# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------
echo "==> Acquiring wake lock"
termux-wake-lock 2>/dev/null || true

if command -v sshd >/dev/null 2>&1; then
    echo "==> Starting sshd on port $SSH_PORT"
    pkill sshd 2>/dev/null || true
    sshd
else
    echo "==> sshd unavailable; skipping LAN SSH"
fi

echo "==> Starting FastAPI ($APP_MODULE) on 127.0.0.1:$APP_PORT"
cd "$APP_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate
nohup uvicorn "$APP_MODULE" \
    --host 127.0.0.1 --port "$APP_PORT" \
    --proxy-headers --forwarded-allow-ips='*' \
    > "$LOG_DIR/uvicorn.log" 2>&1 &
UVICORN_PID=$!
deactivate

# Wait until the app is listening (or 15s, whichever comes first)
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$APP_PORT/health" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

echo "==> Starting Cloudflare Tunnel"
if [ -n "$TUNNEL_NAME" ]; then
    # Named tunnel — stable URL, requires `cloudflared tunnel login` + a
    # ~/.cloudflared/config.yml mapping the tunnel to http://127.0.0.1:$APP_PORT.
    cloudflared tunnel run "$TUNNEL_NAME" > "$LOG_DIR/cloudflared.log" 2>&1 &
else
    # Quick tunnel — random *.trycloudflare.com URL, no account needed.
    cloudflared tunnel --no-autoupdate \
        --url "http://127.0.0.1:$APP_PORT" \
        > "$LOG_DIR/cloudflared.log" 2>&1 &
fi
CF_PID=$!

# Surface the public URL
echo "==> Waiting for public URL..."
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
echo "  SSH        : ssh -p $SSH_PORT $(whoami)@${LAN_IP:-<wifi-ip>}"
echo "  Logs       : $LOG_DIR/"
echo "============================================================"
echo "Press Ctrl+C to stop."

wait "$UVICORN_PID" "$CF_PID"
