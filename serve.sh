#!/data/data/com.termux/files/usr/bin/bash
# VORTEX_SERVE_VERSION=11
# Vortex Termux launcher.
#
#   MODE=hub (default)  Runs the UI (uvicorn + cloudflared quick tunnel)
#                       AND a co-located agent in self-register-wait mode:
#                       open the public URL, log in, click "Self-Register"
#                       and THIS device comes online — no pairing code.
#   MODE=agent          Outbound agent only (a phone you control from a
#                       separate node). Enrol with a REUSABLE account
#                       token (V5.9, recommended) or a legacy 1-time code.
#
# First-run, default mode: just `bash serve.sh`, then self-register in
# the browser.
# Headless enrol with a reusable account token (mint it on any node at
# /enroll-tokens — works for every device, revocable):
#   MODE=agent VORTEX_ACCOUNT_TOKEN=<tok> HUB_URL=https://any-node bash serve.sh
# Legacy single-use code:
#   MODE=agent PAIRING_CODE=123456 HUB_URL=https://abc.trycloudflare.com bash serve.sh
# Either way device_id + token land in ~/.vortex_agent/config.json; the
# agent then auto-discovers nodes (no fixed HUB_URL needed afterwards).
#
#   NO_SELF_AGENT=1  (default mode) skip the co-located self-register
#                    agent — run a headless hub only.

set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/server}"
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"
MODE="${MODE:-hub}"
APP_PORT="${APP_PORT:-8000}"
SSH_PORT="${SSH_PORT:-8022}"

mkdir -p "$LOG_DIR"

# Print which serve.sh is running, so it's obvious when ~/server/ holds an
# older copy than the source folder. To compare: grep VORTEX_SERVE_VERSION
# on both files; if they differ, re-run setup.sh or run from the source dir.
SERVE_VERSION=$(grep -m1 '^# VORTEX_SERVE_VERSION=' "${BASH_SOURCE[0]}" 2>/dev/null \
    | sed 's/.*=//' || echo '?')
echo "==> serve.sh v$SERVE_VERSION ($(realpath "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}"))"

PUBLIC_URL_FILE="${VORTEX_PUBLIC_URL_FILE:-$HOME/.vortex_public_url}"

cleanup() {
    echo "==> Shutting down"
    rm -f "$PUBLIC_URL_FILE" 2>/dev/null || true
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
#
# We always invoke pip as `python -m pip` rather than the `pip` shim --
# Termux's `python -m venv` doesn't always create the shim in .venv/bin/,
# and `ensurepip` guarantees the underlying module is there.
# ----------------------------------------------------------------------------
VPY="$VENV/bin/python"

if [ ! -x "$VPY" ]; then
    echo "==> Building venv at $VENV"
    mkdir -p "$APP_DIR"
    python -m venv "$VENV"
fi

if ! "$VPY" -m pip --version >/dev/null 2>&1; then
    echo "==> Bootstrapping pip into the venv (ensurepip)"
    "$VPY" -m ensurepip --upgrade --default-pip
fi

# Install / top up dependencies. pip install on something already installed
# is a no-op, so this is cheap on warm runs.
if [ "$MODE" = "hub" ]; then
    if ! "$VPY" -c 'import fastapi, uvicorn, websockets, httpx, pydantic, multipart, qrcode' 2>/dev/null; then
        echo "==> Installing hub dependencies"
        "$VPY" -m pip install --quiet --upgrade pip setuptools wheel
        "$VPY" -m pip install --quiet "fastapi<0.100" "pydantic<2" uvicorn websockets httpx python-multipart qrcode
    fi
    # Remote DB (VORTEX_SYNC_URL): two transports. libsql-experimental
    # gives the local+remote *embedded replica* (offline reads) but is a
    # Rust extension with no usable Termux/Android wheel -- so we DON'T
    # try to pip-build it here (it just wastes a long failing compile on
    # a phone). When it's absent the hub automatically uses the V5.11
    # pure-Python Turso HTTP backend (httpx, already installed) -- remote
    # -only, no offline reads, but fully works on Termux. On a glibc
    # Linux box you can `pip install libsql-experimental` yourself for
    # the embedded replica; the hub prefers it if present.
    if [ -n "${VORTEX_SYNC_URL:-}" ]; then
        if "$VPY" -c 'import libsql_experimental' 2>/dev/null; then
            echo "==> Remote DB: embedded replica (libsql-experimental present)"
        else
            echo "==> Remote DB: pure-Python Turso HTTP backend (no Rust needed)"
        fi
    fi
else
    if ! "$VPY" -c 'import websockets, httpx' 2>/dev/null; then
        echo "==> Installing agent dependencies"
        "$VPY" -m pip install --quiet --upgrade pip setuptools wheel
        "$VPY" -m pip install --quiet websockets httpx
    fi
    # Optional: Pillow for V3.0 image thumbnails. Best-effort; if the
    # install fails the agent still works -- thumbnails just return a clear
    # error to the hub, which falls back to filename-only listings.
    if ! "$VPY" -c 'import PIL' 2>/dev/null; then
        "$VPY" -m pip install --quiet Pillow 2>/dev/null || true
    fi
    # V5.34: desktop screen-mirror + remote-control deps (mss, pyautogui).
    # Only meaningful off Termux (a PC being controlled); best-effort so a
    # headless/SBC agent that can't build them still runs every other op.
    if [ -z "${TERMUX_VERSION:-}" ] && [ ! -d /data/data/com.termux/files ]; then
        if ! "$VPY" -c 'import mss, pyautogui' 2>/dev/null; then
            "$VPY" -m pip install --quiet mss pyautogui 2>/dev/null || true
        fi
    fi
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
    # Use `python -m uvicorn` instead of the `uvicorn` shim -- same reason
    # we use `python -m pip` above (Termux doesn't always create the shim).
    nohup "$VPY" -m uvicorn hub.app:app \
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
    # Tell the hub where it's reachable (V5.15): presence + cross-node
    # relay need this, and a quick tunnel's URL is only knowable now.
    if [ -n "$PUBLIC_URL" ]; then
        printf '%s' "$PUBLIC_URL" > "$PUBLIC_URL_FILE" 2>/dev/null || true
    fi

    LAN_IP=$(ip route get 1.1.1.1 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)

    # Co-located self-register agent: waits for the browser self-register
    # flow to drop ~/.vortex_agent/config.json, then connects THIS device
    # to the local hub. HUB_URL pins it to localhost so it never depends
    # on the (rotating) public tunnel being up. Skip with NO_SELF_AGENT=1.
    SELF_AGENT_PID=""
    if [ "${NO_SELF_AGENT:-}" != "1" ]; then
        echo "==> Starting co-located agent (self-register-wait mode)"
        VORTEX_SELFREG_WAIT=1 HUB_URL="http://127.0.0.1:$APP_PORT" \
            nohup "$VPY" -m agent.agent \
            > "$LOG_DIR/agent.log" 2>&1 &
        SELF_AGENT_PID=$!
    fi

    echo
    echo "============================================================"
    echo "  Public URL : ${PUBLIC_URL:-<see logs/cloudflared.log>}"
    echo "  LAN URL    : http://${LAN_IP:-<wifi-ip>}:$APP_PORT"
    echo "  Logs       : $LOG_DIR/"
    echo "============================================================"
    if [ -n "$SELF_AGENT_PID" ]; then
        echo "  Next: open the Public URL, log in, click"
        echo "  '+ Self-Register this device' — it comes online in seconds."
    fi
    echo "Press Ctrl+C to stop."
    wait "$UVICORN_PID" "$CF_PID" ${SELF_AGENT_PID:+"$SELF_AGENT_PID"}

else
    # Legacy agent mode — connect outbound to a separate hub, enroll with
    # a pairing code (PAIRING_CODE / HUB_URL env, or interactive prompt).
    echo "==> Starting Vortex Agent (legacy pairing-code mode)"
    exec "$VPY" -m agent.agent
fi
