# Getting Started — a plain-English guide

This guide gets you from *nothing* to *controlling your devices from
anywhere*, with no jargon. If a word looks technical, it's explained in
the **[Glossary](#glossary)** at the bottom.

You don't have to do everything at once. There are two paths:

- **Path 1 — Control your PC from any phone or laptop's web browser.**
  ~10 minutes, nothing to install on the phone, no cloud account.
  Great first win.
- **Path 2 — Add your phone as a controllable device (with the app).**
  Adds the free cloud database so your phone and PC share one account.

Then a short section makes it **always-on** so it works when you're away.

---

## What you'll end up with

One account. Any device signed into it can see and control your other
devices — screen, files, camera — from across the room or across the
country. It's like AnyDesk/TeamViewer, but it's yours.

**The one idea to understand:** when two devices are on the **same Wi-Fi**,
they talk directly. When they're on **different networks** (you're out,
your PC is home), they need a always-on "middleman" with a public web
address to pass messages between them. That middleman is called a
**relay**, and you'll run one for free on your own PC. (More in the
Glossary — this is normal and exactly how the big remote-control apps
work too.)

---

## Before you start

- A **Windows PC** (this is your main machine / relay).
- Optional: an **Android phone** (for Path 2).
- About **20 minutes**.

---

## Path 1 — Control your PC from any browser

### Step 1. Install Python (one time)

1. Go to <https://www.python.org/downloads/> and click the big
   **Download Python** button.
2. Run the installer. **Important:** on the first screen, tick
   **"Add Python to PATH"**, then click **Install Now**.

### Step 2. Download the app

1. On the project's GitHub page, click the green **`< > Code`** button →
   **Download ZIP**.
2. Unzip it somewhere easy, like `C:\VortexPortal`.

### Step 3. Start it

1. Open the unzipped folder. Click the address bar at the top of the
   window, type `powershell`, and press **Enter** — a blue/black command
   window opens already in the right place.
2. Type this and press Enter:
   ```powershell
   .\serve.ps1
   ```
   - If it complains about "execution policy," run this once, then try
     again:
     ```powershell
     Set-ExecutionPolicy -Scope Process Bypass
     ```
3. The first run takes a minute (it sets itself up). When it's ready it
   prints a few lines, including:
   ```
   Public URL : https://something-random.trycloudflare.com
   ```
   That **Public URL** is your personal control panel, reachable from
   anywhere. Keep this window open — closing it stops everything.

### Step 4. Create your account

1. Open the **Public URL** in any browser (on the PC or your phone).
2. The first visit asks you to **create your admin account** — pick a
   username and password. This is *your* account; remember it.

### Step 5. Add this PC as a controllable device

1. On the dashboard, click **`+ Self-Register`** (it pre-fills this PC's
   details — just confirm).
2. Within a few seconds the PC shows up as **online** in your device
   list.

### Step 6. Control it

Click your PC in the list. You can now:
- **Screen** — see and control the desktop (move the mouse, click, type).
- **Files** — browse, download, and upload.
- **Camera** (if it has one).

Open the same **Public URL** on your phone's browser and log in — you're
now controlling your PC from your phone. 🎉

> **Heads-up:** while the PowerShell window is open, your PC is reachable.
> Close it and everything stops. The **[Always-on](#make-it-always-on)**
> section fixes that so you don't have to keep it open.

---

## Path 2 — Add your phone as a controllable device

Path 1 lets a phone *control* your PC through a browser. To make the
**phone itself** controllable (and use the proper app), your phone and PC
need to share one account. They do that through a small **free cloud
database** — think of it as your account's shared address book that every
device reads.

### Step 1. Create your free cloud database (one time)

1. Go to <https://turso.tech> and sign up (free tier is plenty).
2. Create a **database** (any name).
3. From the database's page, copy two things — keep them handy:
   - the **Database URL** (looks like `libsql://your-db-xyz.turso.io`)
   - an **auth token** (a long string — use the "Create token" / "Tokens"
     option). If you only see a command-line way, the token is whatever
     that command prints.

   These two values are your account's **database link**.

### Step 2. Tell your PC hub about the database

1. In your dashboard (the Public URL), open the **Settings** page.
2. Paste the **Database URL** into *Remote database URL* and the **auth
   token** into *Remote database token*. Save.
3. Stop the PowerShell window (click it, press **Ctrl + C**) and run
   `.\serve.ps1` again so it reconnects using the shared database.
4. Re-create / sign into your account if asked — now your account lives in
   the shared database that the phone can also read.

### Step 3. Install the phone app

1. On the project's GitHub page, open the **Actions** tab → the latest
   **Build Vortex Driver APK** run → download the **`app-debug.apk`**
   file (it's under "Artifacts" at the bottom).
2. Copy it to your phone and tap it to install. Android will ask you to
   **allow installing from this source** — say yes.

### Step 4. Sign in on the phone

1. Open the **Vortex Driver** app.
2. If it asks for the database, paste the **same** Database URL + auth
   token from Step 1. *(If your build already has them baked in, it skips
   straight to sign-in.)*
3. **Sign in** with the same username and password you made on the PC.
4. Grant the permissions it asks for (notifications, and — when you first
   use them — screen sharing and accessibility). These let the phone be
   mirrored and controlled.

Your phone now appears in your device list alongside your PC. You can
control either from the other, or from any browser. ✅

---

## Use another device as a second screen

You can turn the device you're holding (a laptop, tablet, old phone) into
an extra screen for your PC.

1. Open your PC in the device list → **Screen** → **▶ Live stream**.
2. If your PC has more than one display, a **display picker** appears —
   choose which one to show.
3. Tap **⛶ Full screen**. That device is now showing your PC's screen,
   edge to edge.

**Mirror vs. a real extra screen.** Out of the box this *mirrors* a screen
your PC already has. To get a genuine **extended** screen — extra desktop
space you can drag windows onto — your PC needs a *second display to
exist*: a spare monitor, a ~$5 **"HDMI dummy plug,"** or a free **virtual
display driver**.

**One-command virtual screen (Windows).** To add a free, signed virtual
monitor automatically, run this once in an **elevated** PowerShell (Run as
administrator):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\relay-windows\virtual-display.ps1
```

It downloads and installs the signed
[Virtual-Display-Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver)
(no Windows test mode needed) and extends your desktop onto it. Then pick
the new display in step 2. Remove it later with the same command plus
`-Remove`.

**Does this work on Android too?**

| | As the **screen you hold** | As the **PC you extend from** |
|---|---|---|
| **Windows PC** | ✅ | ✅ (with a real/virtual 2nd display) |
| **Android** | ✅ works now (any browser) | ❌ mirror only — no true extend |

An Android phone or tablet makes a great second screen *for your PC* today
(just open the PC's Screen tab on it and tap Full screen). But Android
isn't a multi-monitor desktop, so you can't *extend* an Android device's
own screen onto something else — only mirror it.

---

## Make it always-on

So far, control works only while that PowerShell window is open on your
PC. To keep your PC available as your **relay** (the always-on middleman)
even after restarts:

1. Keep the PC powered on (or set it to not sleep).
2. Open PowerShell **as Administrator** (right-click → *Run as
   administrator*), go to the folder, and run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\relay-windows\install-relay.ps1
   ```
   This makes the app start automatically, stops the PC from sleeping
   while it runs, and restarts it if it ever crashes. Full details and a
   "survive a reboot" tip are in
   [`scripts/relay-windows/README.md`](scripts/relay-windows/README.md).

**Want it running even when your PC is off?** You can also run the exact
same thing on a free, always-on cloud computer (Oracle Cloud Always Free
or Fly.io) — copy-paste recipe in [`deploy/README.md`](deploy/README.md).
Run **both** if you like — your devices automatically use whichever is
online, and fall back to the other if one goes down. Nothing to switch by
hand.

**Your three choices for the always-on relay:**

| Where it runs | Good for | Cost | Stays on when your PC is off? | Setup |
|---|---|---|---|---|
| **Your Windows PC** | the simplest start; a PC you leave on | free | ❌ | one installer ([guide](scripts/relay-windows/README.md)) |
| **Fly.io** (cloud) | easiest cloud, no domain needed | free for light use | ✅ | a couple of commands ([guide](deploy/README.md)) |
| **Oracle Always Free** (cloud) | free forever, runs 24/7 | $0 (+ a free DuckDNS web address) | ✅ | one script + a web address ([guide](deploy/README.md)) |

Not sure? Start with your **PC** to see it working, then move to a **cloud**
one later so you don't have to keep the PC on.

**Test it the honest way:** turn your phone's **Wi-Fi off** (so it's on
cellular, a different network), open your Public URL, sign in, and control
your PC. If that works, you're reachable from anywhere.

---

## Troubleshooting

**"Device offline" even though it's on.**
The device's app/agent isn't connected right now. On a PC, make sure
`serve.ps1` (or the relay task) is running. On a phone, open the Vortex
app once so it reconnects.

**I can control on home Wi-Fi but not when I'm out.**
That's the relay. Make sure your PC (or a cloud computer) is on and
running the app — see [Always-on](#make-it-always-on). Cross-network needs
that middleman running.

**The Public URL changed after I restarted.**
The free tunnel picks a new address each restart. Your devices follow it
automatically, so usually you don't care. If you want a permanent address,
see the "named tunnel" tip in
[`scripts/relay-windows/README.md`](scripts/relay-windows/README.md).

**"Screen" works but typing/scrolling doesn't on a PC.**
Click *on* the screen view first to give it focus, then type or scroll.

**The phone can't be mirrored.**
Open the Vortex app on the phone and accept the **screen sharing** and
**accessibility** permission prompts — Android won't let an app capture or
control the screen without them.

**PowerShell says "running scripts is disabled."**
Run `Set-ExecutionPolicy -Scope Process Bypass` in that same window, then
re-run the command.

---

## Glossary

- **Account** — your username + password. Everything you control belongs
  to it.
- **Hub** — the program that runs on your PC and serves the control
  dashboard. Starting it is just running `serve.ps1`.
- **Agent / the app** — the small piece that runs *on* a device so it can
  be controlled. On a PC it's started for you; on Android it's the Vortex
  Driver app.
- **Relay** — an always-on computer with a public web address that passes
  messages between two devices on **different** networks. Needed because
  home routers block direct incoming connections (this is normal — AnyDesk
  and TeamViewer run their own relays; here you run your own for free).
- **Tunnel (Cloudflare)** — the trick that gives your home PC a public web
  address without changing any router settings. It's what prints the
  *Public URL*.
- **Cloud database (Turso)** — your account's shared address book in the
  cloud, so every device (PC + phone app) reads the same account and
  device list. Free.
- **Direct vs. relay** — on the **same Wi-Fi**, devices talk directly
  (fastest, no middleman). On **different networks**, they go through the
  relay.
- **Self-Register** — the button that adds the current PC to your account
  as a controllable device.

---

Stuck on a step? The technical details live in
[`README.md`](README.md) and [`driver/README.md`](driver/README.md).
