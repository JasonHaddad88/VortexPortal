#!/usr/bin/env bash
# Always-on Vortex relay on a fresh Ubuntu VM (Oracle Cloud Always Free,
# or any cloud VM). Installs Docker, opens the OS firewall, and brings up
# the hub + Caddy (auto-HTTPS) as self-restarting containers.
#
# Run from the repo root, as a sudo-capable user:
#   VORTEX_SYNC_URL=libsql://your-db.turso.io \
#   VORTEX_SYNC_TOKEN=your-token \
#   RELAY_DOMAIN=vortex.yourdomain.com \
#     bash deploy/oracle-setup.sh
#
# DO THESE FIRST in the cloud console / DNS (can't be scripted from inside
# the VM):
#   1. Open INGRESS for TCP 80 and 443 in the VM's Security List / NSG.
#   2. Point RELAY_DOMAIN's DNS A record at this VM's public IP.
set -euo pipefail

: "${VORTEX_SYNC_URL:?set VORTEX_SYNC_URL (your Turso database URL)}"
: "${VORTEX_SYNC_TOKEN:?set VORTEX_SYNC_TOKEN (your Turso auth token)}"
: "${RELAY_DOMAIN:?set RELAY_DOMAIN -- a domain pointed at this VM public IP}"

# --- Docker -----------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi

# --- OS firewall ------------------------------------------------------------
# Oracle's Ubuntu images ship a restrictive INPUT chain (a REJECT near the
# end); insert ACCEPT rules ABOVE it for 80 + 443 so Caddy is reachable.
# NOTE: this is the *host* firewall only -- you must ALSO open 80 + 443 as
# ingress in the VM's Security List / NSG in the Oracle console.
echo "==> Opening TCP 80 + 443 on the host firewall"
sudo iptables -I INPUT -p tcp --dport 80  -j ACCEPT || true
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT || true
# Persist across reboots (Oracle Ubuntu has iptables but not always the
# persistence package); non-interactive so the script never blocks.
if ! command -v netfilter-persistent >/dev/null 2>&1; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq || true
  echo 'iptables-persistent iptables-persistent/autosave_v4 boolean true' | sudo debconf-set-selections
  echo 'iptables-persistent iptables-persistent/autosave_v6 boolean true' | sudo debconf-set-selections
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent >/dev/null 2>&1 || true
fi
sudo netfilter-persistent save 2>/dev/null || true

# --- Bring it up ------------------------------------------------------------
cd "$(dirname "$0")"   # deploy/
export VORTEX_SYNC_URL VORTEX_SYNC_TOKEN RELAY_DOMAIN
echo "==> Building + starting the relay (hub + Caddy)"
sudo -E docker compose up -d --build

cat <<EOF

==> Done. Caddy will fetch a TLS certificate for https://${RELAY_DOMAIN}
    on the first request (give it ~30s + correct DNS).

Verify from your laptop:
    curl -I https://${RELAY_DOMAIN}/login        # expect HTTP 200

Your devices auto-discover this relay (it heartbeats its URL into your
shared database). Run it ALONGSIDE your home PC relay -- devices use
whichever is online and fall back automatically.

Logs:    sudo docker compose logs -f
Stop:    sudo docker compose down
EOF
