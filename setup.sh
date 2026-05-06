#!/data/data/com.termux/files/usr/bin/bash
# Vortex Termux first-run setup.
#
# Installs Python + websockets + httpx, copies the agent code into
# ~/server/, writes the Termux:Boot autostart hook. Idempotent — safe to
# re-run on a partial install.
#
# After setup, run:
#   PAIRING_CODE=123456 HUB_URL=https://<your-hub> bash ~/server/serve.sh
# to pair the device. Subsequent runs need no env vars.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/server"

if [ -z "${PREFIX:-}" ] || [ ! -d "$PREFIX" ]; then
    echo "ERROR: \$PREFIX is unset — this script must run inside Termux."
    exit 1
fi

echo "============================================================"
echo "  Vortex Agent — first-time setup"
echo "============================================================"
echo

# ----------------------------------------------------------------------------
# Storage permission for ~/storage/shared
# ----------------------------------------------------------------------------
echo "==> Requesting Android storage permission"
echo "    (If a system dialog appears, tap 'Allow'.)"
termux-setup-storage 2>/dev/null || true
sleep 1

# ----------------------------------------------------------------------------
# Packages
# ----------------------------------------------------------------------------
echo "==> Refreshing package lists"
yes | pkg update -y || true

echo "==> Installing essentials"
for p in python python-pip openssh curl; do
    if dpkg -s "$p" >/dev/null 2>&1; then
        echo "    already installed: $p"
    else
        echo "    installing: $p"
        if ! pkg install -y "$p"; then
            echo "    ERROR: failed to install $p"
            exit 1
        fi
    fi
done

echo "==> Installing optional extras (best-effort)"
for opt in git nano termux-api procps cloudflared; do
    if dpkg -s "$opt" >/dev/null 2>&1; then
        echo "    already installed: $opt"
    elif pkg install -y "$opt" >/dev/null 2>&1; then
        echo "    installed: $opt"
    else
        echo "    skipped: $opt (not available)"
    fi
done

# ----------------------------------------------------------------------------
# SSH (LAN management is nice to have)
# ----------------------------------------------------------------------------
echo "==> Configuring sshd"
if ! grep -q "^PasswordAuthentication yes" "$PREFIX/etc/ssh/sshd_config" 2>/dev/null; then
    echo "PasswordAuthentication yes" >> "$PREFIX/etc/ssh/sshd_config"
fi
ssh-keygen -A >/dev/null 2>&1 || true

if [ -f "$HOME/.ssh-passwd-set" ]; then
    echo "    SSH password already set (delete ~/.ssh-passwd-set to re-prompt)"
else
    echo "    Set a password for SSH login (username: $(whoami))"
    if passwd; then
        touch "$HOME/.ssh-passwd-set"
    fi
fi

# ----------------------------------------------------------------------------
# Python venv with pure-Python pins
# ----------------------------------------------------------------------------
mkdir -p "$APP_DIR"

# ----------------------------------------------------------------------------
# Sanity-check: Termux's Python is consistent with its system libs.
#
# Symptom we're guarding against: `pkg upgrade python` ships a newer Python
# that needs a newer libexpat, but libexpat didn't get upgraded in lockstep
# (partial system update). pyexpat then dlopen-fails on a missing symbol
# like XML_SetAllocTrackerActivationThreshold, which breaks xml.parsers.expat
# -> xmlrpc.client -> pip itself.
# ----------------------------------------------------------------------------
if ! python -c 'import xml.parsers.expat, ssl, ctypes' 2>/dev/null; then
    echo
    echo "============================================================"
    echo "  ERROR: Termux's Python is broken (stdlib import failed)."
    echo "============================================================"
    echo "  This usually means a partial pkg upgrade left Python and"
    echo "  its system C libraries (libexpat, openssl, etc.) at"
    echo "  mismatched versions."
    echo
    echo "  Fix:"
    echo "    yes | pkg update"
    echo "    yes | pkg upgrade -y"
    echo "    rm -rf $APP_DIR        # nuke the venv built against the old libs"
    echo "    bash setup.sh          # then retry"
    echo
    echo "  Diagnostic detail:"
    python -c 'import xml.parsers.expat' 2>&1 | tail -3 | sed 's/^/    /'
    exit 1
fi

# Termux's `python -m venv` doesn't always create the `pip` shim in
# .venv/bin/, so we always invoke pip as `python -m pip` and bootstrap it
# via `ensurepip` to guarantee it's installed inside the venv.
VPY="$APP_DIR/.venv/bin/python"

if [ -x "$VPY" ]; then
    echo "==> Python venv already exists (skipping create)"
else
    echo "==> Building Python venv at $APP_DIR/.venv"
    python -m venv "$APP_DIR/.venv"
fi

if ! "$VPY" -m pip --version >/dev/null 2>&1; then
    echo "==> Bootstrapping pip into the venv (ensurepip)"
    "$VPY" -m ensurepip --upgrade --default-pip
fi

echo "==> Ensuring agent dependencies (websockets, httpx)"
"$VPY" -m pip install --quiet --upgrade pip setuptools wheel
"$VPY" -m pip install --quiet websockets httpx

# ----------------------------------------------------------------------------
# Copy agent + hub code into $APP_DIR (hub is optional but useful for the
# "promote a phone to hub" flow).
# ----------------------------------------------------------------------------
for d in agent hub; do
    if [ -d "$SCRIPT_DIR/$d" ]; then
        echo "==> Installing $d/ into $APP_DIR"
        cp -r "$SCRIPT_DIR/$d" "$APP_DIR/"
    else
        echo "    WARNING: $SCRIPT_DIR/$d not found — copy it manually"
    fi
done

# ----------------------------------------------------------------------------
# Drop serve.sh next to the code so the boot hook works
# ----------------------------------------------------------------------------
echo "==> Installing serve.sh into $APP_DIR"
if [ -f "$SCRIPT_DIR/serve.sh" ]; then
    if [ "$SCRIPT_DIR/serve.sh" != "$APP_DIR/serve.sh" ]; then
        cp "$SCRIPT_DIR/serve.sh" "$APP_DIR/serve.sh"
    fi
    chmod +x "$APP_DIR/serve.sh"
else
    echo "    WARNING: serve.sh not found alongside setup.sh"
fi

# ----------------------------------------------------------------------------
# Termux:Boot autostart — only fires the agent (the typical case).
# ----------------------------------------------------------------------------
echo "==> Installing Termux:Boot autostart hook (agent mode)"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-vortex-agent" <<BOOT
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
exec bash "$APP_DIR/serve.sh" >> "$APP_DIR/agent.log" 2>&1
BOOT
chmod +x "$HOME/.termux/boot/start-vortex-agent"

# ----------------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------------
echo
echo "============================================================"
echo "  Setup complete!"
echo "============================================================"
echo
echo "  Agent code  : $APP_DIR/agent/"
echo "  Hub code    : $APP_DIR/hub/   (only used if MODE=hub)"
echo "  Logs        : $APP_DIR/logs/"
echo
echo "  To pair this device with a hub:"
echo "    1. On your hub, log in and click 'Add Device' to get a code."
echo "    2. On this phone, run:"
echo "         PAIRING_CODE=123456 HUB_URL=https://your-hub bash $APP_DIR/serve.sh"
echo "    3. After it pairs, future runs need no env vars:"
echo "         bash $APP_DIR/serve.sh"
echo
echo "  Autostart on boot: install 'Termux:Boot' from F-Droid (NOT Play"
echo "  Store), open it once, then reboot. It will start the agent and"
echo "  keep it reconnected."
echo
echo "  To run THIS phone as a hub instead:"
echo "    MODE=hub bash $APP_DIR/serve.sh"
echo
