# Always-on Windows relay

Turn a Windows PC you leave on into your account's **always-on relay** so
you can control your devices from anywhere — not just the same Wi-Fi. The
relay is just the Vortex hub + a Cloudflare tunnel; your phone and other
devices auto-discover its public URL and route through it when they're on
a different network. The same PC stays a controllable device too.

## Why a relay at all

Two devices on **different networks** are each behind NAT and can't dial
each other directly — something with a public address must bridge them
(this is exactly what AnyDesk/TeamViewer do with their own servers). On
the **same** Wi-Fi, Vortex connects peer-to-peer and needs no relay.

`cloudflared` makes an **outbound** tunnel to Cloudflare, which hands back
a public `https://…trycloudflare.com` URL — so this works **without port
forwarding or a public IP**, even behind a home router.

## Install (one time)

In an **elevated** PowerShell (Run as administrator), from the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\relay-windows\install-relay.ps1
```

This registers a `VortexRelay` scheduled task that, at every logon:
starts the hub + tunnel + agent, **keeps the PC awake** while running, and
**auto-restarts** on crash. Remove it with `uninstall-relay.ps1`.

First time only: open the printed local URL, sign in / create your account,
and click **+ Self-Register this device** so this PC appears in your fleet.

## Make it truly unattended

- **Survive a reboot with nobody present:** enable auto sign-in — run
  `netplwiz`, untick *"Users must enter a user name and password."* The
  task fires on the auto-logon.
- **A URL that never changes (optional):** quick tunnels rotate their URL
  on restart; Vortex already follows the rotation via `node_endpoints`
  auto-discovery, so you usually don't care. If you want a permanent URL
  (and own a domain), use a **named** Cloudflare tunnel:

  ```powershell
  .\bin\cloudflared.exe tunnel login
  .\bin\cloudflared.exe tunnel create vortex
  .\bin\cloudflared.exe tunnel route dns vortex vortex.yourdomain.com
  # ~/.cloudflared/config.yml:
  #   tunnel: vortex
  #   credentials-file: C:\Users\<you>\.cloudflared\<uuid>.json
  #   ingress:
  #     - hostname: vortex.yourdomain.com
  #       service: http://127.0.0.1:8000
  #     - service: http_status:404
  ```

  Then set `VORTEX_HUB_PUBLIC_URL=https://vortex.yourdomain.com` (Settings
  tab or env) so the hub advertises the stable URL.

## Verify

- `logs\public_url.txt` holds the current public URL once the tunnel is up.
- From your phone on **cellular** (Wi-Fi off), open that URL, sign in, and
  control this PC — that proves the cross-network path end-to-end.

## Don't want to keep a PC on?

A free 24/7 cloud VM (Oracle Cloud Always Free, Fly.io) runs the exact
same hub and never sleeps — better for "anytime" without leaving your own
machine on. Ask and we'll add the Dockerfile + deploy recipe.
