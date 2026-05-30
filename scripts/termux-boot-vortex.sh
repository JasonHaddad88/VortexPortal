#!/data/data/com.termux/files/usr/bin/sh
# B11.16: Termux:Boot autostart for a Vortex self-host.
#
# This script is meant for the Termux:Boot add-on. Copy it to
# ~/.termux/boot/ (filename can be anything; here we suggest
# `vortex-autostart`):
#
#   mkdir -p ~/.termux/boot
#   cp scripts/termux-boot-vortex.sh ~/.termux/boot/vortex-autostart
#   chmod +x ~/.termux/boot/vortex-autostart
#
# After installing the Termux:Boot app from F-Droid + once-per-install
# launching it once, this script runs at every device boot:
#
#   1. Acquire a Termux wake lock so Android doesn't kill the hub.
#   2. Start `serve.sh` (default MODE=hub) which:
#        - boots uvicorn on 127.0.0.1:8000
#        - launches `cloudflared tunnel --url http://127.0.0.1:8000`
#          to get a public `https://*.trycloudflare.com` URL
#        - starts a co-located self-register agent
#   3. The hub heartbeats its current public URL into the shared
#      Turso DB (`node_endpoints` table). The Driver APK polls
#      that table every 60 s and follows the rotating URL with no
#      user intervention -- so "the URL changed after reboot"
#      stops being a problem.
#
# Adjust REPO_DIR if your clone lives somewhere else.

set -eu

REPO_DIR="${VORTEX_REPO_DIR:-$HOME/VortexPortal}"
LOG_DIR="${VORTEX_LOG_DIR:-$HOME/.vortex_boot_logs}"
mkdir -p "$LOG_DIR"

# Hold a wake lock for the lifetime of the booted shell. Termux:Boot
# starts us in a background shell that's allowed to acquire it even
# before any Termux UI is open.
termux-wake-lock 2>/dev/null || true

# Run from the repo directory so serve.sh's APP_DIR fallback finds
# the agent/ + hub/ folders. nohup + redirect so we survive the
# Termux:Boot launcher exiting.
cd "$REPO_DIR"
nohup ./serve.sh \
    > "$LOG_DIR/boot-$(date +%Y%m%d-%H%M%S).log" 2>&1 &

echo "vortex autostart pid=$!" >> "$LOG_DIR/last-boot.txt"
