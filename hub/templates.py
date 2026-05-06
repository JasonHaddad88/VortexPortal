"""HTML templates + the futuristic theme.

Single CSS string inlined into every page. Black background, purple +
light-blue (cyan) highlights, glassmorphism topbar, neon glows on hover.
Lifted from app_v1.py and extended with login/register/pair/admin pages.
"""

from html import escape
from typing import Optional


CSS = r"""
:root {
  --bg: #06060a;
  --surface: #0e0e18;
  --surface-2: #14141f;
  --border: rgba(168, 85, 247, 0.18);
  --border-strong: rgba(168, 85, 247, 0.45);
  --text: #e2e8f0;
  --muted: #6b7280;
  --purple: #a855f7;
  --purple-glow: rgba(168, 85, 247, 0.45);
  --cyan: #67e8f9;
  --cyan-glow: rgba(103, 232, 249, 0.40);
  --danger: #ef4444;
  --success: #34d399;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 15px;
  line-height: 1.5;
  min-height: 100vh;
}
body {
  background:
    radial-gradient(ellipse at top left, rgba(168,85,247,0.10), transparent 60%),
    radial-gradient(ellipse at bottom right, rgba(103,232,249,0.08), transparent 60%),
    var(--bg);
  background-attachment: fixed;
}
a { color: var(--cyan); text-decoration: none; }
a:hover { text-decoration: underline; }

.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 1rem 2rem;
  border-bottom: 1px solid var(--border);
  background: rgba(14, 14, 24, 0.72);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  position: sticky; top: 0; z-index: 10;
}
.brand { display: flex; align-items: center; gap: 0.75rem; }
.brand .logo {
  width: 30px; height: 30px;
  background: linear-gradient(135deg, var(--purple), var(--cyan));
  border-radius: 8px;
  box-shadow: 0 0 18px var(--purple-glow);
  position: relative;
}
.brand .logo::after {
  content: '';
  position: absolute; inset: 5px;
  background: var(--bg);
  border-radius: 4px;
}
.brand h1 {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.brand h1 .accent {
  background: linear-gradient(90deg, var(--purple), var(--cyan));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
nav { display: flex; gap: 1.5rem; align-items: center; }
nav a {
  color: var(--muted);
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 0.5rem 0;
  border-bottom: 2px solid transparent;
  transition: color .2s, border-color .2s;
  text-decoration: none;
}
nav a:hover { color: var(--text); }
nav a.active { color: var(--cyan); border-bottom-color: var(--cyan); }
nav .user {
  color: var(--purple);
  font-size: 0.72rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  padding-left: 1rem;
  border-left: 1px solid var(--border);
}

main { padding: 2rem; max-width: 1200px; margin: 0 auto; }
.center-wrap {
  min-height: calc(100vh - 80px);
  display: flex; align-items: center; justify-content: center;
  padding: 2rem;
}

.section-head {
  display: flex; align-items: baseline; justify-content: space-between;
  margin: 0 0 1rem;
  gap: 1rem; flex-wrap: wrap;
}
.section-head h2 {
  margin: 0;
  font-size: 0.85rem;
  font-weight: 600;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--muted);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1.25rem;
  margin-bottom: 2rem;
}

.card {
  background: linear-gradient(180deg, var(--surface), var(--surface-2));
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.25rem;
  position: relative;
  transition: transform .15s, box-shadow .2s, border-color .2s;
  display: flex; flex-direction: column;
  min-height: 160px;
}
.card:hover {
  transform: translateY(-2px);
  border-color: var(--border-strong);
  box-shadow: 0 8px 32px var(--purple-glow);
}
.card h3 {
  margin: 0 0 0.5rem;
  font-size: 1.05rem;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.card .meta {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.72rem;
  color: var(--muted);
  word-break: break-all;
  margin: 0 0 0.4rem;
}
.card .actions {
  display: flex; gap: 0.5rem; flex-wrap: wrap;
  margin-top: auto;
  padding-top: 0.75rem;
}

.badge {
  display: inline-flex; align-items: center; gap: 0.4rem;
  padding: 0.2rem 0.55rem;
  border-radius: 999px;
  font-size: 0.65rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  border: 1px solid transparent;
  white-space: nowrap;
}
.badge::before {
  content: '';
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--muted);
}
.badge.online { color: var(--success); border-color: rgba(52,211,153,.3); background: rgba(52,211,153,.07); }
.badge.online::before { background: var(--success); box-shadow: 0 0 8px var(--success); }
.badge.offline { color: var(--danger); border-color: rgba(239,68,68,.3); background: rgba(239,68,68,.07); }
.badge.offline::before { background: var(--danger); }
.badge.unknown { color: var(--muted); border-color: rgba(107,114,128,.3); background: rgba(107,114,128,.07); }

.btn {
  display: inline-block;
  padding: 0.5rem 0.95rem;
  background: transparent;
  color: var(--cyan);
  border: 1px solid rgba(103, 232, 249, 0.3);
  border-radius: 8px;
  text-decoration: none;
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  cursor: pointer;
  font-family: inherit;
  transition: background .15s, box-shadow .15s, border-color .15s, color .15s;
}
.btn:hover { background: rgba(103, 232, 249, 0.1); border-color: var(--cyan); box-shadow: 0 0 16px var(--cyan-glow); text-decoration: none; }
.btn-primary { color: var(--purple); border-color: rgba(168, 85, 247, 0.35); }
.btn-primary:hover { background: rgba(168, 85, 247, 0.1); border-color: var(--purple); box-shadow: 0 0 16px var(--purple-glow); }
.btn-danger { color: var(--danger); border-color: rgba(239, 68, 68, 0.3); }
.btn-danger:hover { background: rgba(239, 68, 68, 0.1); border-color: var(--danger); box-shadow: 0 0 16px rgba(239,68,68,.3); }
.btn-small { padding: 0.3rem 0.65rem; font-size: 0.68rem; }

form { display: flex; flex-direction: column; gap: 0.75rem; }
form.inline { flex-direction: row; gap: 0.5rem; align-items: center; }
form.inline input { flex: 1; }
label { display: flex; flex-direction: column; gap: 0.35rem; font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; }
input, select, textarea {
  background: var(--surface-2);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 0.65rem 0.8rem;
  border-radius: 6px;
  font-size: 0.95rem;
  font-family: inherit;
  transition: border-color .15s, box-shadow .15s;
}
input:focus, select:focus, textarea:focus {
  outline: none; border-color: var(--purple);
  box-shadow: 0 0 0 3px var(--purple-glow);
}

.auth-card {
  width: 100%; max-width: 380px;
  background: linear-gradient(180deg, var(--surface), var(--surface-2));
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 2rem;
  box-shadow: 0 16px 48px rgba(0,0,0,.5);
}
.auth-card .brand-large {
  text-align: center; margin-bottom: 2rem;
}
.auth-card .brand-large .logo {
  width: 56px; height: 56px;
  margin: 0 auto 1rem;
  background: linear-gradient(135deg, var(--purple), var(--cyan));
  border-radius: 14px;
  box-shadow: 0 0 32px var(--purple-glow);
  position: relative;
}
.auth-card .brand-large .logo::after {
  content: ''; position: absolute; inset: 8px;
  background: var(--bg); border-radius: 8px;
}
.auth-card h1 {
  margin: 0;
  font-size: 1.1rem; font-weight: 600;
  letter-spacing: 0.16em; text-transform: uppercase;
  text-align: center;
}
.auth-card h1 .accent {
  background: linear-gradient(90deg, var(--purple), var(--cyan));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.auth-card .subtitle {
  text-align: center;
  font-size: 0.7rem; color: var(--muted);
  letter-spacing: 0.18em; text-transform: uppercase;
  margin: 0.5rem 0 1.75rem;
}
.auth-card .footer-link {
  text-align: center; margin-top: 1.5rem; font-size: 0.78rem;
  color: var(--muted);
}

.flash {
  padding: 0.75rem 1rem;
  border-radius: 8px;
  margin-bottom: 1rem;
  font-size: 0.85rem;
}
.flash.error { background: rgba(239,68,68,.1); border: 1px solid rgba(239,68,68,.3); color: var(--danger); }
.flash.success { background: rgba(52,211,153,.08); border: 1px solid rgba(52,211,153,.3); color: var(--success); }
.flash.info { background: rgba(103,232,249,.08); border: 1px solid rgba(103,232,249,.3); color: var(--cyan); }

.code-display {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 2.5rem;
  letter-spacing: 0.5rem;
  text-align: center;
  padding: 1.5rem;
  background: var(--surface-2);
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  color: var(--cyan);
  text-shadow: 0 0 16px var(--cyan-glow);
  margin: 1rem 0;
}
.invite-code {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.85rem;
  background: var(--surface-2);
  padding: 0.4rem 0.7rem;
  border-radius: 6px;
  border: 1px solid var(--border);
  word-break: break-all;
  user-select: all;
}

.breadcrumbs {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  padding: 1rem 0;
  margin-bottom: 1rem;
  border-bottom: 1px solid var(--border);
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.85rem;
}
.breadcrumbs .device-pill {
  display: inline-flex; align-items: center; gap: 0.4rem;
  padding: 0.2rem 0.6rem;
  background: rgba(168, 85, 247, 0.12);
  border: 1px solid rgba(168, 85, 247, 0.3);
  border-radius: 999px;
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--purple);
}
.breadcrumbs span.sep { color: var(--muted); }
.breadcrumbs a { color: var(--text); }

ul.dirlist { list-style: none; padding: 0; margin: 0; }
ul.dirlist li {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.55rem 0.85rem;
  border-radius: 6px;
  border: 1px solid transparent;
  transition: background .1s, border-color .1s;
  gap: 0.5rem;
}
ul.dirlist li:hover { background: rgba(168, 85, 247, 0.05); border-color: var(--border); }
ul.dirlist a {
  color: var(--text);
  text-decoration: none;
  flex: 1; min-width: 0;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.85rem;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
ul.dirlist a:hover { color: var(--cyan); }
ul.dirlist .size { color: var(--muted); font-size: 0.72rem; white-space: nowrap; }

.empty {
  text-align: center;
  padding: 3rem 1rem;
  color: var(--muted);
  border: 1px dashed var(--border);
  border-radius: 12px;
}
.empty h3 { color: var(--text); margin-top: 0; font-weight: 600; letter-spacing: 0.05em; }

.invites-list {
  display: flex; flex-direction: column; gap: 0.5rem;
  margin-bottom: 1.5rem;
}
.invites-list .row {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.6rem 0.8rem;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 0.8rem;
}
.invites-list .row .status { color: var(--muted); font-size: 0.7rem; letter-spacing: 0.1em; text-transform: uppercase; }
.invites-list .row .status.used { color: var(--success); }

/* ---------- Per-device stats on dashboard cards ---------- */
.stats {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.4rem 0.85rem;
  margin: 0.5rem 0 0.75rem;
  font-size: 0.72rem;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
.stats .stat {
  display: flex; align-items: center; gap: 0.4rem;
  color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.stats .stat .label {
  color: var(--muted);
  font-size: 0.6rem; text-transform: uppercase; letter-spacing: 0.1em;
  margin-right: 0.2rem;
}
.stats .bar {
  height: 4px; flex: 1; min-width: 30px;
  background: var(--surface-2);
  border-radius: 2px; overflow: hidden;
  border: 1px solid var(--border);
}
.stats .bar > i {
  display: block; height: 100%;
  background: linear-gradient(90deg, var(--cyan), var(--purple));
}
.stats .bar.warn > i { background: #f59e0b; }
.stats .bar.crit > i { background: var(--danger); }

/* ---------- Inline thumbnails in file listings ---------- */
ul.dirlist li .thumb {
  width: 40px; height: 40px;
  flex-shrink: 0;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  object-fit: cover;
  margin-right: 0.5rem;
}
ul.dirlist li .thumb.placeholder {
  display: inline-block;
}

/* ---------- Upload drop zone (V3.0) ---------- */
.upload-zone {
  border: 1px dashed var(--border);
  border-radius: 10px;
  padding: 0.85rem 1rem;
  margin: 0.5rem 0 1rem;
  display: flex; align-items: center; gap: 1rem;
  font-size: 0.78rem;
  color: var(--muted);
  transition: background .15s, border-color .15s;
}
.upload-zone.drag {
  background: rgba(168, 85, 247, 0.08);
  border-color: var(--purple);
  color: var(--text);
}
.upload-zone .hint { flex: 1; }
.upload-zone input[type=file] { display: none; }
.upload-progress {
  display: none;
  flex-direction: column;
  gap: 0.35rem;
  margin: 0.25rem 0 1rem;
}
.upload-progress.active { display: flex; }
.upload-progress .row {
  display: flex; align-items: center; gap: 0.6rem;
  font-size: 0.75rem;
}
.upload-progress .name {
  flex: 1; min-width: 0;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.upload-progress .bar {
  width: 140px; height: 4px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 2px; overflow: hidden;
}
.upload-progress .bar > i {
  display: block; height: 100%;
  background: linear-gradient(90deg, var(--cyan), var(--purple));
  width: 0;
  transition: width .12s;
}
.upload-progress .pct { width: 3rem; text-align: right; color: var(--muted); }
.upload-progress .row.done .bar > i { background: var(--success); }
.upload-progress .row.error .bar > i { background: var(--danger); }
.upload-progress .row.error .pct { color: var(--danger); }

footer {
  text-align: center;
  padding: 2rem 1rem;
  font-size: 0.7rem;
  color: var(--muted);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
"""


def page(title: str, body: str, *, user: Optional[dict] = None,
         active: str = "", chrome: bool = True, version: str = "3.0") -> str:
    """Wrap body in the standard page chrome (topbar + footer)."""
    if chrome:
        nav_items = [
            ("/", "Dashboard"),
            ("/pair", "Add Device"),
        ]
        if user and user.get("is_admin"):
            nav_items.append(("/admin/invites", "Invites"))
        links = "".join(
            f'<a class="{"active" if href == active else ""}" href="{href}">{escape(label)}</a>'
            for href, label in nav_items
        )
        user_chunk = ""
        if user:
            user_chunk = (
                f'<span class="user">{escape(user["username"])}</span>'
                f'<a href="/logout" style="margin-left:1rem;font-size:0.7rem">Logout</a>'
            )
        chrome_html = f"""<header class="topbar">
  <div class="brand">
    <span class="logo"></span>
    <h1>Vortex<span class="accent">Hub</span></h1>
  </div>
  <nav>{links}{user_chunk}</nav>
</header>
<main>{body}</main>
<footer>VORTEX HUB V{version}</footer>"""
    else:
        chrome_html = body

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — Vortex Hub</title>
<style>{CSS}</style>
</head><body>{chrome_html}</body></html>"""


def login_page(error: Optional[str] = None, next_url: str = "/") -> str:
    err = f'<div class="flash error">{escape(error)}</div>' if error else ""
    body = f"""<div class="center-wrap"><div class="auth-card">
  <div class="brand-large">
    <div class="logo"></div>
    <h1>Vortex<span class="accent">Hub</span></h1>
    <p class="subtitle">Sign in</p>
  </div>
  {err}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{escape(next_url)}">
    <label>Username <input name="username" autocomplete="username" required autofocus></label>
    <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
    <button type="submit" class="btn btn-primary" style="margin-top:.75rem">Sign In</button>
  </form>
  <div class="footer-link">
    Have an invite code? <a href="/register">Create account</a>
  </div>
</div></div>"""
    return page("Sign in", body, chrome=False)


def register_page(error: Optional[str] = None,
                  invite: str = "", username: str = "") -> str:
    err = f'<div class="flash error">{escape(error)}</div>' if error else ""
    body = f"""<div class="center-wrap"><div class="auth-card">
  <div class="brand-large">
    <div class="logo"></div>
    <h1>Vortex<span class="accent">Hub</span></h1>
    <p class="subtitle">Create account</p>
  </div>
  {err}
  <form method="post" action="/register">
    <label>Invite code <input name="invite" value="{escape(invite)}" required autofocus></label>
    <label>Username <input name="username" value="{escape(username)}" autocomplete="username" required maxlength="40"></label>
    <label>Password <input name="password" type="password" autocomplete="new-password" required minlength="8"></label>
    <label>Confirm <input name="password2" type="password" autocomplete="new-password" required minlength="8"></label>
    <button type="submit" class="btn btn-primary" style="margin-top:.75rem">Register</button>
  </form>
  <div class="footer-link">
    Already have an account? <a href="/login">Sign in</a>
  </div>
</div></div>"""
    return page("Register", body, chrome=False)


def first_run_page(error: Optional[str] = None) -> str:
    """Bootstrap form: no users in DB yet, so there's no invite to require."""
    err = f'<div class="flash error">{escape(error)}</div>' if error else ""
    body = f"""<div class="center-wrap"><div class="auth-card">
  <div class="brand-large">
    <div class="logo"></div>
    <h1>Vortex<span class="accent">Hub</span></h1>
    <p class="subtitle">First-run setup</p>
  </div>
  <div class="flash info">No users yet. Create the admin account.</div>
  {err}
  <form method="post" action="/register">
    <input type="hidden" name="invite" value="__bootstrap__">
    <label>Username <input name="username" autocomplete="username" required maxlength="40" autofocus></label>
    <label>Password <input name="password" type="password" autocomplete="new-password" required minlength="8"></label>
    <label>Confirm <input name="password2" type="password" autocomplete="new-password" required minlength="8"></label>
    <button type="submit" class="btn btn-primary" style="margin-top:.75rem">Create Admin</button>
  </form>
</div></div>"""
    return page("First-run", body, chrome=False)


def dashboard_page(user: dict, devices: list, online: set) -> str:
    cards = []
    for d in devices:
        did = escape(d["id"])
        name = escape(d["name"])
        is_online = d["id"] in online
        badge_cls = "online" if is_online else "offline"
        badge_lbl = "Online" if is_online else "Offline"
        last_seen = "never" if not d.get("last_seen") else f"Δ {_ago(d['last_seen'])}"
        cards.append(f"""<div class="card" data-device-id="{did}">
  <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:.5rem;gap:.5rem">
    <h3 style="margin:0;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">{name}</h3>
    <span class="badge {badge_cls}" data-status>{badge_lbl}</span>
  </div>
  <p class="meta">id: {did}</p>
  <p class="meta" data-last-seen>last seen: {escape(last_seen)}</p>
  <div class="stats" data-stats hidden></div>
  <div class="actions">
    <a class="btn btn-primary" href="/devices/{did}/files/">Browse</a>
    <a class="btn" href="/devices/{did}">Manage</a>
    <form method="post" action="/devices/{did}/delete" style="display:inline;margin:0;margin-left:auto">
      <button class="btn btn-danger btn-small" type="submit"
              onclick="return confirm('Unpair {name}? This cannot be undone — you will need to re-pair to control it again.')"
              title="Unpair this device">Delete</button>
    </form>
  </div>
</div>""")

    if cards:
        grid_html = f'<div class="grid">{"".join(cards)}</div>'
    else:
        grid_html = """<div class="empty">
  <h3>No devices paired yet</h3>
  <p>Click “+ Add Device” to pair your first phone.</p>
</div>"""

    body = f"""
<div class="section-head">
  <h2>// Your Devices</h2>
  <a class="btn btn-primary" href="/pair">+ Add Device</a>
</div>
{grid_html}
<script>
function fmtBytes(n) {{
  if (n == null) return '?';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {{ n /= 1024; i++; }}
  return n.toFixed(n < 10 && i > 0 ? 1 : 0) + ' ' + u[i];
}}
function fmtUptime(s) {{
  if (s == null) return '?';
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600);  s -= h * 3600;
  const m = Math.floor(s / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}}
function bar(usedFrac, label, valueText) {{
  const pct = Math.max(0, Math.min(1, usedFrac));
  const cls = pct > 0.9 ? 'crit' : pct > 0.75 ? 'warn' : '';
  return `<div class="stat"><span class="label">${{label}}</span>` +
         `<div class="bar ${{cls}}"><i style="width:${{(pct*100).toFixed(0)}}%"></i></div>` +
         `<span>${{valueText}}</span></div>`;
}}
function renderStats(card, s) {{
  const el = card.querySelector('[data-stats]');
  if (!el) return;
  if (!s) {{ el.hidden = true; el.innerHTML = ''; return; }}
  const parts = [];
  if (s.battery && s.battery.percentage != null) {{
    const p = s.battery.percentage;
    const charging = (s.battery.status || '').toUpperCase().includes('CHARG');
    const arrow = charging ? '⚡' : '';
    parts.push(bar(1 - p / 100, 'Battery', `${{p}}% ${{arrow}}`));
  }}
  if (s.storage && s.storage.total) {{
    const used = s.storage.total - s.storage.free;
    parts.push(bar(used / s.storage.total, 'Disk',
                   `${{fmtBytes(s.storage.free)}} free`));
  }}
  if (s.memory && s.memory.total) {{
    const free = s.memory.available || 0;
    const used = s.memory.total - free;
    parts.push(bar(used / s.memory.total, 'RAM',
                   `${{fmtBytes(free)}} free`));
  }}
  if (s.uptime_s != null) {{
    parts.push(`<div class="stat"><span class="label">Up</span><span>${{fmtUptime(s.uptime_s)}}</span></div>`);
  }}
  el.innerHTML = parts.join('');
  el.hidden = parts.length === 0;
}}

async function pollOnline() {{
  try {{
    const r = await fetch('/api/online', {{cache: 'no-store'}});
    if (!r.ok) return;
    const online = new Set((await r.json()).online || []);
    document.querySelectorAll('[data-device-id]').forEach(card => {{
      const id = card.dataset.deviceId;
      const badge = card.querySelector('[data-status]');
      if (!badge) return;
      if (online.has(id)) {{ badge.className = 'badge online'; badge.textContent = 'Online'; }}
      else {{
        badge.className = 'badge offline'; badge.textContent = 'Offline';
        renderStats(card, null);
      }}
    }});
  }} catch (e) {{}}
}}
async function pollStats() {{
  try {{
    const r = await fetch('/api/devices/stats', {{cache: 'no-store'}});
    if (!r.ok) return;
    const stats = (await r.json()).stats || {{}};
    document.querySelectorAll('[data-device-id]').forEach(card => {{
      renderStats(card, stats[card.dataset.deviceId]);
    }});
  }} catch (e) {{}}
}}
pollOnline(); pollStats();
setInterval(pollOnline, 5000);
setInterval(pollStats, 15000);
</script>
"""
    return page("Dashboard", body, user=user, active="/")


def pair_start_page(user: dict) -> str:
    body = f"""
<div class="section-head"><h2>// Pair a New Device</h2></div>
<div class="card" style="max-width:560px">
  <p style="margin-top:0;color:var(--muted);font-size:.85rem">
    Generate a one-time pairing code. The agent on your phone will use it to
    enroll itself with this hub.
  </p>
  <form method="post" action="/pair">
    <label>Device name (optional)
      <input name="device_name" placeholder="e.g. Pixel 8 Pro" maxlength="80">
    </label>
    <button type="submit" class="btn btn-primary" style="align-self:flex-start;margin-top:.5rem">
      Generate Code
    </button>
  </form>
</div>"""
    return page("Add Device", body, user=user, active="/pair")


def pair_code_page(user: dict, code: str, hub_url: str,
                   device_name: Optional[str]) -> str:
    name_arg = ""
    if device_name:
        name_arg = f" DEVICE_NAME={_shell_quote(device_name)}"
    cmd = (f"PAIRING_CODE={code} HUB_URL={_shell_quote(hub_url)}{name_arg} "
           f"bash ~/server/serve.sh")
    body = f"""
<div class="section-head"><h2>// Pairing Code</h2></div>
<div style="max-width:640px">
  <div class="code-display">{escape(code)}</div>
  <p style="color:var(--muted);font-size:.85rem;text-align:center">
    Code expires in 10 minutes. Single use.
  </p>

  <h3 style="margin-top:2rem;font-size:.85rem;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)">
    On the device, run:
  </h3>
  <div class="card" style="padding:1rem">
    <code style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.78rem;color:var(--cyan);word-break:break-all;user-select:all;display:block">{escape(cmd)}</code>
  </div>

  <p style="margin-top:1.5rem;color:var(--muted);font-size:.78rem">
    Or set the code interactively when the agent prompts.
  </p>
  <p style="font-size:.85rem">
    Once paired, the device appears on your <a href="/">dashboard</a>.
  </p>
</div>"""
    return page("Pairing", body, user=user, active="/pair")


def device_manage_page(user: dict, device: dict, online: bool) -> str:
    badge_cls = "online" if online else "offline"
    badge_lbl = "Online" if online else "Offline"
    body = f"""
<div class="section-head" style="align-items:center">
  <h2>// {escape(device['name'])}</h2>
  <span class="badge {badge_cls}">{badge_lbl}</span>
</div>
<div class="card" style="max-width:560px">
  <p class="meta">id: {escape(device['id'])}</p>
  <p class="meta">paired: {escape(_ago(device['paired_at']))} ago</p>
  <p class="meta">last seen: {escape(_ago(device['last_seen']) + ' ago' if device.get('last_seen') else 'never')}</p>

  <h3 style="margin-top:1rem;font-size:.78rem;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)">Rename</h3>
  <form method="post" action="/devices/{escape(device['id'])}/rename" class="inline">
    <input name="name" value="{escape(device['name'])}" required maxlength="80">
    <button type="submit" class="btn">Save</button>
  </form>

  <div class="actions" style="margin-top:1.5rem">
    <a class="btn btn-primary" href="/devices/{escape(device['id'])}/files/">Browse Files</a>
    <form method="post" action="/devices/{escape(device['id'])}/delete" style="display:inline;margin:0">
      <button class="btn btn-danger" type="submit"
              onclick="return confirm('Unpair {escape(device['name'])}? You will need to re-pair to control it again.')">
        Unpair
      </button>
    </form>
  </div>
</div>"""
    return page(device["name"], body, user=user, active="/")


def files_page(user: dict, device: dict, rel: str, entries: list) -> str:
    from urllib.parse import quote
    rows = []
    if rel:
        rows.append('<li><a href="../">../</a><span class="size"></span></li>')
    rel_prefix = (rel.rstrip("/") + "/") if rel else ""
    for e in entries:
        is_dir = bool(e.get("is_dir"))
        name = e["name"] + ("/" if is_dir else "")
        href = quote(e["name"]) + ("/" if is_dir else "")
        size = ""
        if not is_dir and e.get("size") is not None:
            size = f"{e['size']:,} bytes"
        thumb = ""
        if not is_dir and e.get("is_image"):
            # loading="lazy" so a directory of 500 photos doesn't fire 500
            # requests on first render; browser only fetches when scrolled
            # into view. The thumb endpoint sets immutable cache headers.
            thumb_url = (f"/devices/{quote(device['id'])}/thumb/"
                         f"{quote(rel_prefix + e['name'])}?size=80")
            thumb = (f'<img class="thumb" loading="lazy" decoding="async" '
                     f'src="{thumb_url}" alt="">')
        rows.append(
            f'<li>{thumb}<a href="{href}">{escape(name)}</a>'
            f'<span class="size">{size}</span></li>'
        )

    title_path = "/" + rel
    # JS escapes for safety inside the data-* attributes / template literals.
    js_did = device['id']
    js_dir_prefix = rel_prefix
    body = f"""<div class="breadcrumbs">
  <span class="device-pill">{escape(device['name'])}</span>
  <span class="sep">·</span>
  <a href="/devices/{escape(device['id'])}">manage</a>
  <span class="sep">·</span>
  <span>{escape(title_path)}</span>
</div>

<div class="upload-zone" id="upload-zone">
  <span class="hint">Drop files here to upload to <code>{escape(title_path)}</code>, or
    <a href="#" id="upload-pick" style="color:var(--cyan)">choose files</a>.
  </span>
  <input type="file" id="upload-input" multiple>
</div>
<div class="upload-progress" id="upload-progress"></div>

<ul class="dirlist">{''.join(rows)}</ul>

<script>
(function() {{
  const did = {json_dumps(js_did)};
  const dir = {json_dumps(js_dir_prefix)};
  const zone = document.getElementById('upload-zone');
  const input = document.getElementById('upload-input');
  const pick = document.getElementById('upload-pick');
  const progress = document.getElementById('upload-progress');

  pick.addEventListener('click', e => {{ e.preventDefault(); input.click(); }});
  input.addEventListener('change', () => {{
    if (input.files && input.files.length) uploadFiles(input.files);
    input.value = '';
  }});

  ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, e => {{
    e.preventDefault(); e.stopPropagation(); zone.classList.add('drag');
  }}));
  ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, e => {{
    e.preventDefault(); e.stopPropagation(); zone.classList.remove('drag');
  }}));
  zone.addEventListener('drop', e => {{
    if (e.dataTransfer && e.dataTransfer.files.length)
      uploadFiles(e.dataTransfer.files);
  }});

  function makeRow(name) {{
    const r = document.createElement('div');
    r.className = 'row';
    r.innerHTML = `<span class="name">${{escapeHtml(name)}}</span>` +
                  `<div class="bar"><i></i></div>` +
                  `<span class="pct">0%</span>`;
    return r;
  }}
  function escapeHtml(s) {{
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }}

  async function uploadFiles(files) {{
    progress.classList.add('active');
    let allOk = true;
    for (const f of files) {{
      const row = makeRow(f.name);
      progress.appendChild(row);
      const bar = row.querySelector('.bar > i');
      const pct = row.querySelector('.pct');

      try {{
        await new Promise((resolve, reject) => {{
          const xhr = new XMLHttpRequest();
          const url = `/devices/${{encodeURIComponent(did)}}/files/` +
                      encodeURIComponent(dir + f.name);
          xhr.open('PUT', url);
          xhr.upload.onprogress = e => {{
            if (!e.lengthComputable) return;
            const p = Math.round(e.loaded / e.total * 100);
            bar.style.width = p + '%';
            pct.textContent = p + '%';
          }};
          xhr.onload = () => {{
            if (xhr.status >= 200 && xhr.status < 300) resolve();
            else reject(new Error('HTTP ' + xhr.status + ': ' + xhr.responseText));
          }};
          xhr.onerror = () => reject(new Error('network error'));
          xhr.send(f);
        }});
        row.classList.add('done');
        bar.style.width = '100%';
        pct.textContent = 'done';
      }} catch (e) {{
        row.classList.add('error');
        pct.textContent = 'error';
        row.title = e.message;
        allOk = false;
      }}
    }}
    if (allOk) {{
      // Reload after a short pause so the user sees the green ticks first.
      setTimeout(() => location.reload(), 500);
    }}
  }}
}})();
</script>"""
    return page(f"Files {title_path}", body, user=user, active="/")


def _json_dumps_for_html(value) -> str:
    """JSON for inline JS literals; escapes < and / so a stray "</script>" in
    a path can't break out of the script tag."""
    import json as _json
    return (_json.dumps(value)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


# alias for f-string-friendly use above
json_dumps = _json_dumps_for_html


def files_error_page(user: dict, device: dict, message: str) -> str:
    body = f"""<div class="breadcrumbs">
  <span class="device-pill">{escape(device['name'])}</span>
  <span class="sep">·</span>
  <a href="/devices/{escape(device['id'])}">manage</a>
</div>
<div class="empty">
  <h3>Unavailable</h3>
  <p>{escape(message)}</p>
  <p style="margin-top:1rem"><a class="btn" href="/">Back to dashboard</a></p>
</div>"""
    return page(f"Files — {device['name']}", body, user=user, active="/")


def admin_invites_page(user: dict, invites: list, hub_url: str) -> str:
    rows = []
    for inv in invites:
        if inv["used_by"]:
            status_html = '<span class="status used">Used</span>'
        else:
            status_html = '<span class="status">Unused</span>'
        link = f"{hub_url.rstrip('/')}/register?invite={inv['code']}"
        rows.append(f"""<div class="row">
  <span class="invite-code">{escape(inv['code'])}</span>
  {status_html}
  <a class="btn btn-small" href="/register?invite={escape(inv['code'])}" style="margin-left:auto">Open Link</a>
</div>""")
    rows_html = "".join(rows) if rows else (
        '<div class="empty"><p>No invites yet. Generate one below.</p></div>'
    )

    body = f"""
<div class="section-head"><h2>// Invite Codes</h2></div>
<div class="invites-list">{rows_html}</div>

<div class="card" style="max-width:480px">
  <form method="post" action="/admin/invites">
    <p style="margin:0;color:var(--muted);font-size:.85rem">
      Generate a one-time invite code. Recipients can self-register at
      <code style="color:var(--cyan)">/register?invite=...</code>.
    </p>
    <button type="submit" class="btn btn-primary" style="align-self:flex-start;margin-top:.5rem">
      Generate Invite
    </button>
  </form>
</div>"""
    return page("Invites", body, user=user, active="/admin/invites")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ago(ts: int) -> str:
    import time as _t
    delta = max(0, int(_t.time()) - int(ts))
    if delta < 60: return f"{delta}s"
    if delta < 3600: return f"{delta//60}m"
    if delta < 86400: return f"{delta//3600}h"
    return f"{delta//86400}d"


def _shell_quote(s: str) -> str:
    if not s or any(c in s for c in " '\"$`\\!"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s
