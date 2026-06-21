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

## Option B — Oracle Cloud Always Free (truly free 24/7; needs a domain)

A free Ampere VM that never sleeps. Auto-HTTPS via Caddy + a domain you
own ([`docker-compose.yml`](docker-compose.yml) + [`Caddyfile`](Caddyfile)).

**First, in the Oracle console / your DNS:**
1. Create an **Always Free** Ubuntu VM; note its **public IP**.
2. In the VM's **Security List / NSG**, add **ingress** for TCP **80** and
   **443**.
3. Point a domain's **DNS A record** at that public IP
   (e.g. `vortex.yourdomain.com`).

**Then, on the VM** (clone this repo first):

```bash
VORTEX_SYNC_URL=libsql://your-db.turso.io \
VORTEX_SYNC_TOKEN=your-token \
RELAY_DOMAIN=vortex.yourdomain.com \
  bash deploy/oracle-setup.sh
```

It installs Docker, opens the firewall, and starts the hub + Caddy. Caddy
fetches a Let's Encrypt cert on first hit. Verify from your laptop:

```bash
curl -I https://vortex.yourdomain.com/login     # expect HTTP 200
```

No domain? Either grab a cheap/free one, or run `cloudflared` on the VM
exactly like the home-PC relay (see
[`scripts/relay-windows/README.md`](../scripts/relay-windows/README.md)
for the named-tunnel idea) instead of Caddy.

---

## Either way: it just joins your fleet

Once it's up and advertising its URL, there's nothing to configure on your
phone or PC — they read your shared database, see the new relay, and use
it when they're away from home. Run it next to your home-PC relay for
redundancy.

**Test the honest way:** phone on **cellular** (Wi-Fi off) → open the relay
URL → sign in → control your PC.
