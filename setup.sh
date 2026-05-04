#!/data/data/com.termux/files/usr/bin/bash
# One-time Termux setup: turns this phone into a server reachable from anywhere.
# Run with:   bash setup.sh
# Idempotent — safe to re-run on a partial/aborted install.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/server"

# ----------------------------------------------------------------------------
# Sanity checks
# ----------------------------------------------------------------------------
if [ -z "${PREFIX:-}" ] || [ ! -d "$PREFIX" ]; then
    echo "ERROR: \$PREFIX is unset — this script must run inside Termux."
    exit 1
fi

echo "============================================================"
echo "  Termux phone-as-a-server — first-time setup"
echo "============================================================"
echo

# ----------------------------------------------------------------------------
# Storage permission FIRST. Without it, ~/storage/shared doesn't exist and
# the file browser has nothing to show. Already-granted = silent no-op.
# ----------------------------------------------------------------------------
echo "==> Requesting Android storage permission"
echo "    (If a system dialog appears, tap 'Allow'.)"
termux-setup-storage 2>/dev/null || true
sleep 1

# ----------------------------------------------------------------------------
# Package install — split into essentials (must succeed) and optionals
# (best-effort) so a missing-mirror failure on a nice-to-have can't strand
# the install with no python or no openssh.
# ----------------------------------------------------------------------------
echo "==> Refreshing package lists"
yes | pkg update -y || true

echo "==> Installing essentials"
for p in python python-pip openssh cloudflared curl; do
    if dpkg -s "$p" >/dev/null 2>&1; then
        echo "    already installed: $p"
    else
        echo "    installing: $p"
        if ! pkg install -y "$p"; then
            echo "    ERROR: failed to install $p"
            echo "           Try 'pkg update && pkg upgrade' and re-run setup.sh."
            exit 1
        fi
    fi
done

echo "==> Installing optional extras (best-effort)"
for opt in git jq nano termux-api procps; do
    if dpkg -s "$opt" >/dev/null 2>&1; then
        echo "    already installed: $opt"
    elif pkg install -y "$opt" >/dev/null 2>&1; then
        echo "    installed: $opt"
    else
        echo "    skipped: $opt (not available)"
    fi
done

# ----------------------------------------------------------------------------
# SSH config: enable password auth, generate host keys, prompt for passwd
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
# Python venv with pure-Python pins (no Rust/C compilation on Termux ARM)
# ----------------------------------------------------------------------------
mkdir -p "$APP_DIR"

if [ -x "$APP_DIR/.venv/bin/uvicorn" ]; then
    echo "==> Python venv already built (skipping)"
else
    echo "==> Building Python venv at $APP_DIR/.venv"
    (
        cd "$APP_DIR"
        python -m venv .venv
        # shellcheck disable=SC1091
        source .venv/bin/activate
        pip install --quiet --upgrade pip setuptools wheel
        # Pure-Python pins:
        #   - FastAPI <0.100 keeps Pydantic v1 (no pydantic-core / no Rust)
        #   - plain uvicorn (no [standard]) skips uvloop/httptools/watchfiles
        # All install as prebuilt wheels in seconds; no compilation needed.
        # httpx is needed for the multi-device proxy added in V1.2.
        # Pure Python (depends on httpcore, h11, idna, sniffio, anyio,
        # certifi — all pure Python).
        pip install --quiet "fastapi<0.100" "pydantic<2" uvicorn httpx
    )
fi

# ----------------------------------------------------------------------------
# Ensure httpx is present even if the venv was built by an older setup.sh
# ----------------------------------------------------------------------------
if [ -x "$APP_DIR/.venv/bin/uvicorn" ] \
   && [ ! -d "$APP_DIR/.venv/lib"/python*/site-packages/httpx ] 2>/dev/null; then
    if ! "$APP_DIR/.venv/bin/python" -c 'import httpx' >/dev/null 2>&1; then
        echo "==> Installing httpx into existing venv (V1.2 dep)"
        (
            # shellcheck disable=SC1091
            source "$APP_DIR/.venv/bin/activate"
            pip install --quiet httpx
        )
    fi
fi

# ----------------------------------------------------------------------------
# HTTP Basic credentials → ~/server/.env (mode 600, verbatim KEY=VALUE)
# ----------------------------------------------------------------------------
# Hash a password with PBKDF2-SHA256 (pure-stdlib, no extra deps).
# Reads plaintext from $AUTH_PASS_INPUT to keep it off the command line/ps.
_hash_password() {
    AUTH_PASS_INPUT="$1" python -c '
import os, hashlib, base64
pw = os.environ["AUTH_PASS_INPUT"].encode()
salt = os.urandom(16)
iters = 200_000
digest = hashlib.pbkdf2_hmac("sha256", pw, salt, iters)
print(f"pbkdf2_sha256${iters}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}")
'
}

if [ -f "$APP_DIR/.env" ]; then
    if ! grep -q '^AUTH_HASH=' "$APP_DIR/.env" 2>/dev/null \
       && grep -q '^AUTH_PASS=' "$APP_DIR/.env" 2>/dev/null; then
        echo "==> Detected legacy plaintext password in $APP_DIR/.env"
        printf "    Upgrade to PBKDF2-SHA256 hash now? [Y/n] "
        read -r yn
        case "$yn" in
            [Nn]*)
                echo "    Keeping plaintext (still supported by app.py, but less secure)."
                ;;
            *)
                read -r -s -p "    Re-enter password to hash: " AUTH_PASS; echo
                AUTH_HASH=$(_hash_password "$AUTH_PASS")
                if [ -z "$AUTH_HASH" ]; then
                    echo "    ERROR: hashing failed; .env left unchanged"
                    unset AUTH_PASS
                else
                    AUTH_USER_LEGACY=$(grep '^AUTH_USER=' "$APP_DIR/.env" | head -n1 | cut -d= -f2-)
                    umask 077
                    {
                        printf 'AUTH_USER=%s\n' "$AUTH_USER_LEGACY"
                        printf 'AUTH_HASH=%s\n' "$AUTH_HASH"
                    } > "$APP_DIR/.env"
                    chmod 600 "$APP_DIR/.env"
                    unset AUTH_PASS AUTH_USER_LEGACY
                    echo "    Upgraded to PBKDF2-SHA256 (200k iterations)."
                fi
                ;;
        esac
    else
        echo "==> Auth credentials already exist at $APP_DIR/.env (skipping)"
    fi
else
    echo
    echo "==> Set credentials for the public URL (HTTP Basic auth)"
    read -r -p "    Username: " AUTH_USER
    while :; do
        read -r -s -p "    Password: " AUTH_PASS; echo
        read -r -s -p "    Confirm:  " AUTH_PASS2; echo
        [ "$AUTH_PASS" = "$AUTH_PASS2" ] && break
        echo "    Passwords didn't match, try again."
    done
    AUTH_HASH=$(_hash_password "$AUTH_PASS")
    if [ -z "$AUTH_HASH" ]; then
        echo "ERROR: failed to hash password (Python error?)"
        exit 1
    fi
    umask 077
    {
        printf 'AUTH_USER=%s\n' "$AUTH_USER"
        printf 'AUTH_HASH=%s\n' "$AUTH_HASH"
    } > "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    unset AUTH_PASS AUTH_PASS2
    echo "    Saved to $APP_DIR/.env (mode 600, PBKDF2-SHA256). Edit to rotate."
fi

# ----------------------------------------------------------------------------
# Application: copy app.py (FastAPI multi-device dashboard) from the script
# directory into ~/server/. If an older version exists, back it up rather
# than clobbering — the user may have customized it.
# ----------------------------------------------------------------------------
echo "==> Installing app.py into $APP_DIR"
if [ -f "$SCRIPT_DIR/app.py" ]; then
    if [ -f "$APP_DIR/app.py" ]; then
        if grep -q '__VORTEX_VERSION__ = "1.2"' "$APP_DIR/app.py" 2>/dev/null; then
            echo "    app.py already at V1.2 (skipping)"
        else
            backup="$APP_DIR/app.py.bak.$(date +%s)"
            echo "    Backing up older app.py to $backup"
            mv "$APP_DIR/app.py" "$backup"
            cp "$SCRIPT_DIR/app.py" "$APP_DIR/app.py"
            echo "    Installed V1.2 app.py (multi-device dashboard)"
        fi
    else
        cp "$SCRIPT_DIR/app.py" "$APP_DIR/app.py"
        echo "    Installed V1.2 app.py (multi-device dashboard)"
    fi
else
    echo "    ERROR: app.py not found alongside setup.sh ($SCRIPT_DIR)"
    echo "           Place app.py next to setup.sh and re-run."
    exit 1
fi

# ----------------------------------------------------------------------------
# Initialize the device registry (mode 600 — contains remote credentials)
# ----------------------------------------------------------------------------
if [ ! -f "$APP_DIR/devices.json" ]; then
    echo "==> Creating empty device registry at $APP_DIR/devices.json"
    umask 077
    printf '{"devices": []}\n' > "$APP_DIR/devices.json"
    chmod 600 "$APP_DIR/devices.json"
fi

# ----------------------------------------------------------------------------
# Drop serve.sh next to app.py so the boot hook works
# ----------------------------------------------------------------------------
echo "==> Installing serve.sh into $APP_DIR"
if [ -f "$SCRIPT_DIR/serve.sh" ]; then
    if [ "$SCRIPT_DIR/serve.sh" != "$APP_DIR/serve.sh" ]; then
        cp "$SCRIPT_DIR/serve.sh" "$APP_DIR/serve.sh"
    fi
    chmod +x "$APP_DIR/serve.sh"
else
    echo "    WARNING: serve.sh not found alongside setup.sh ($SCRIPT_DIR)"
    echo "    Place serve.sh at $APP_DIR/serve.sh manually."
fi

# ----------------------------------------------------------------------------
# Termux:Boot autostart hook (only fires if Termux:Boot app is installed)
# ----------------------------------------------------------------------------
echo "==> Installing Termux:Boot autostart hook"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-server" <<BOOT
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
exec bash "$APP_DIR/serve.sh" >> "$APP_DIR/server.log" 2>&1
BOOT
chmod +x "$HOME/.termux/boot/start-server"

# ----------------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------------
echo
echo "============================================================"
echo "  Setup complete!"
echo "============================================================"
echo
echo "  Server dir : $APP_DIR"
echo "  App code   : $APP_DIR/app.py"
echo "  Creds      : $APP_DIR/.env  (rotate by editing)"
echo
echo "  Start now  : bash $APP_DIR/serve.sh"
echo "  Autostart  : install 'Termux:Boot' from F-Droid (NOT Play Store),"
echo "               open it once, then reboot."
echo
