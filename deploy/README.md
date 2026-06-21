# Cloud relay — run the hub on an always-on cloud computer

A **relay** is the always-on, publicly-reachable box that bridges your
devices when they're on different networks (see the main
[GETTING-STARTED](../GETTING-STARTED.md) for the plain-English version).
You can run one on your home PC ([`scripts/relay-windows/`](../scripts/relay-windows/)),
**or** on a cloud computer that never sleeps — or **both** (devices use
whichever is online and fail over automatically; nothing to switch by
hand).

A cloud relay is just the hub pointed at your shared **Turso** database.
No Cloudflare tunnel (the platform gives you HTTPS) and no agent (a
headless relay isn't a controllable device). It advertises itself to your
devices automatically via `VORTEX_HUB_PUBLIC_URL`.

You'll need your two database values from
[GETTING-STARTED → Path 2, Step 1](../GETTING-STARTED.md#step-1-create-your-free-cloud-database-one-time):
`VORTEX_SYNC_URL` and `VORTEX_SYNC_TOKEN`.

---

## Option A — Fly.io (easiest; built-in HTTPS)

Fly builds the [`Dockerfile`](../Dockerfile) and serves HTTPS for you.

```bash
# one-time: install flyctl + log in
curl -L https://fly.io/install.sh | sh
fly auth login

# from the REPO ROOT (so the build sees hub/):
fly apps create my-vortex-relay              # pick a unique name
#   -> put that same name in deploy/fly.toml's `app = ...`

fly secrets set \
  VORTEX_SYNC_URL=libsql://your-db.turso.io \
  VORTEX_SYNC_TOKEN=your-token \
  --config deploy/fly.toml

fly deploy --config deploy/fly.toml

# tell the hub its own public URL so it advertises correctly:
fly secrets set VORTEX_HUB_PUBLIC_URL=https://my-vortex-relay.fly.dev \
  --config deploy/fly.toml
```

Verify: open `https://my-vortex-relay.fly.dev` — you should get the login
page. Your devices will discover it within a minute.

> **Always-on note:** `fly.toml` sets `auto_stop_machines = false` +
> `min_machines_running = 1` on purpose — a relay that scales to zero
> would drop cross-network control whenever it's idle. Fly's free
> allowance covers a small always-on machine for light use; check current
> pricing if you run several.

---

## Option B — Oracle Cloud Always Free (truly free 24/7)

A free VM that never sleeps. Auto-HTTPS via Caddy + a domain you point at
it ([`docker-compose.yml`](docker-compose.yml) + [`Caddyfile`](Caddyfile)).
Total cost can be **$0** (free VM + a free DuckDNS domain).

### 1. Create the VM (Oracle console)

- Compute → Instances → **Create**. Choose an **Always Free** shape:
  **Ampere A1 (ARM)** is the most generous and works fine — the Docker
  image is multi-arch, so ARM "just works." (If you see *"out of host
  capacity,"* retry later or pick a different Availability Domain/region;
  it's a known Always-Free quirk, not your setup.)
- Image: **Ubuntu**. Save the SSH key it offers. Note the **public IP**.

### 2. Open BOTH firewalls for 80 + 443

Oracle has **two** firewalls — open both, or HTTPS won't reach Caddy:
- **Cloud:** the VM's subnet **Security List** (or the instance's **NSG**)
  → add **ingress** rules: source `0.0.0.0/0`, TCP **80** and **443**.
- **Host:** the Ubuntu `iptables` — **the setup script does this for you**
  (and makes it survive reboots).

### 3. Point a domain at the IP

Any domain works. **Free option:** [duckdns.org](https://www.duckdns.org)
→ sign in, create a subdomain (e.g. `myvortex.duckdns.org`), set its IP to
the VM's public IP. Caddy gets a Let's Encrypt cert for it automatically.
(Or use a domain you own: add an **A record** → the VM IP.)

### 4. Run it (on the VM)

SSH into the VM (`ssh ubuntu@<public-ip>` with the key you saved), then:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/JasonHaddad88/VortexPortal.git
cd VortexPortal

VORTEX_SYNC_URL=libsql://your-db.turso.io \
VORTEX_SYNC_TOKEN=your-token \
RELAY_DOMAIN=myvortex.duckdns.org \
  bash deploy/oracle-setup.sh
```

It installs Docker, opens the host firewall (persistently), and starts the
hub + Caddy. Caddy fetches the TLS cert on the first request. Verify from
your laptop:

```bash
curl -I https://myvortex.duckdns.org/login      # expect HTTP 200
```

If the cert doesn't appear in ~30s: double-check DNS resolves to the VM IP
(`nslookup myvortex.duckdns.org`) and that **both** firewalls allow 80/443
— Caddy needs port 80 reachable to validate the certificate.

**No domain at all?** Run `cloudflared` on the VM exactly like the home-PC
relay (see [`scripts/relay-windows/README.md`](../scripts/relay-windows/README.md)'s
named-tunnel note) instead of Caddy.

---

## Either way: it just joins your fleet

Once it's up and advertising its URL, there's nothing to configure on your
phone or PC — they read your shared database, see the new relay, and use
it when they're away from home. Run it next to your home-PC relay for
redundancy.

**Test the honest way:** phone on **cellular** (Wi-Fi off) → open the relay
URL → sign in → control your PC.
