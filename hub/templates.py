"""HTML templates + the futuristic theme.

Single CSS string inlined into every page. Black background, purple +
light-blue (cyan) highlights, glassmorphism topbar, neon glows on hover.
Lifted from app_v1.py and extended with login/register/pair/admin pages.
"""

from html import escape
from typing import Optional

from . import __VORTEX_VERSION__


def _qr_svg(text: str, *, box: int = 8, border: int = 2) -> str:
    """Render `text` as an SVG QR code suitable for inlining in HTML.

    Inline SVG = no extra HTTP round-trip, scales crisply at any zoom,
    and we don't need Pillow on the hub for image generation. Returns the
    full <svg ...>...</svg> markup; falls back to a plain message if the
    qrcode library isn't installed (so the page still loads).
    """
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ('<p style="color:var(--muted);font-size:.8rem">'
                '(QR unavailable: install <code>qrcode</code> in the hub venv)</p>')
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box,
        border=border,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    import io
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


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

/* ---------- QR pairing (V3.0) ---------- */
.qr-row {
  display: flex; gap: 1.25rem; align-items: stretch;
  margin: 1rem 0 0;
  flex-wrap: wrap;
}
.qr-card {
  background: #fff;
  padding: 0.85rem;
  border-radius: 12px;
  border: 1px solid var(--border-strong);
  box-shadow: 0 0 24px var(--purple-glow);
  display: flex; align-items: center; justify-content: center;
}
.qr-card svg { width: 220px; height: 220px; display: block; }
.qr-side {
  flex: 1; min-width: 240px;
  display: flex; flex-direction: column; justify-content: center; gap: 0.5rem;
}
.qr-side h4 {
  margin: 0;
  font-size: 0.7rem; letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--muted); font-weight: 600;
}
.qr-side ol {
  margin: 0; padding-left: 1.1rem;
  color: var(--text); font-size: 0.85rem;
  line-height: 1.5;
}
.qr-side ol li { margin: 0.25rem 0; }
.copy-btn {
  margin-left: 0.5rem;
  padding: 0.25rem 0.55rem;
  font-size: 0.65rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--cyan);
  background: transparent;
  border: 1px solid rgba(103,232,249,0.3);
  border-radius: 6px;
  cursor: pointer;
  font-family: inherit;
  transition: background .15s, border-color .15s;
}
.copy-btn:hover { background: rgba(103,232,249,0.1); border-color: var(--cyan); }
.copy-btn.ok { color: var(--success); border-color: rgba(52,211,153,.4); }

/* ---------- Camera viewer (V4.0) ---------- */
.cam-toolbar {
  display: flex; gap: 0.6rem; flex-wrap: wrap; align-items: center;
  margin-bottom: 1rem;
}
.cam-toolbar .cam-status {
  margin-left: auto;
  font-size: 0.7rem; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--muted);
}
.cam-stage {
  background: #000;
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  min-height: 220px;
  display: flex; align-items: center; justify-content: center;
  position: relative;
  overflow: hidden;
}
.cam-stage img {
  display: block;
  width: 100%; height: auto;
  max-height: 70vh;
  object-fit: contain;
}
.cam-stage .placeholder {
  color: var(--muted);
  font-size: 0.85rem; padding: 3rem 1rem; text-align: center;
}
.cam-stage .err {
  color: var(--danger);
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.78rem;
  padding: 1.5rem; text-align: center;
}
.cam-stage .spinner {
  position: absolute; top: 1rem; right: 1rem;
  width: 16px; height: 16px;
  border: 2px solid rgba(168,85,247,0.2);
  border-top-color: var(--purple);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  display: none;
}
.cam-stage .spinner.on { display: block; }
@keyframes spin { to { transform: rotate(360deg); } }

select.cam-pick {
  background: var(--surface-2);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 0.5rem 0.7rem;
  border-radius: 8px;
  font-size: 0.8rem;
  font-family: inherit;
  min-width: 9rem;
}

/* honest "feature requires APK" box */
.unsupported {
  border: 1px solid rgba(245, 158, 11, 0.4);
  background: rgba(245, 158, 11, 0.06);
  border-radius: 12px;
  padding: 1.25rem 1.5rem;
  color: var(--text);
  font-size: 0.88rem;
  line-height: 1.6;
}
.unsupported h3 {
  margin: 0 0 0.5rem;
  font-size: 0.85rem;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: #f59e0b;
}
.unsupported code {
  color: var(--cyan);
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.85em;
}

/* ---------- V5.1 dashboard card streamline ---------- */
/* Status column on the right of the card header: badge stacked above the
   trashcan, info-circle anchored alongside. */
.card .status-col {
  display: flex; flex-direction: column; align-items: flex-end;
  gap: 0.45rem;
  flex-shrink: 0;
}
.icon-btn {
  width: 28px; height: 28px;
  display: inline-flex; align-items: center; justify-content: center;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--muted);
  cursor: pointer;
  font-family: inherit;
  padding: 0;
  transition: color .15s, border-color .15s, background .15s, box-shadow .15s;
}
.icon-btn:hover {
  color: var(--text);
  border-color: var(--border-strong);
  background: rgba(168, 85, 247, 0.08);
}
.icon-btn.danger:hover {
  color: var(--danger);
  border-color: var(--danger);
  background: rgba(239, 68, 68, 0.08);
  box-shadow: 0 0 12px rgba(239, 68, 68, 0.25);
}
.icon-btn svg { width: 14px; height: 14px; display: block; }
.icon-btn.info {
  /* The inline button sits inline with .meta paragraphs */
  margin-left: 0.5rem;
  vertical-align: middle;
}

/* Compact 4-button action row */
.card .actions.compact {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0.4rem;
}
.card .actions.compact .btn {
  padding: 0.45rem 0.3rem;
  font-size: 0.68rem;
  letter-spacing: 0.08em;
  text-align: center;
}

/* ---------- V5.1 device-info modal ---------- */
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(6, 6, 10, 0.78);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  display: none;
  align-items: center; justify-content: center;
  z-index: 100;
  padding: 1.5rem;
}
.modal-backdrop.open { display: flex; }
.modal {
  width: 100%; max-width: 540px; max-height: 84vh;
  background: linear-gradient(180deg, var(--surface), var(--surface-2));
  border: 1px solid var(--border-strong);
  border-radius: 14px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.6),
              0 0 32px rgba(168, 85, 247, 0.18);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.modal .modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.95rem 1.25rem;
  border-bottom: 1px solid var(--border);
}
.modal .modal-head h3 {
  margin: 0;
  font-size: 0.85rem;
  font-weight: 600;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--muted);
}
.modal .modal-head .close {
  width: 28px; height: 28px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 1rem;
  line-height: 1;
}
.modal .modal-head .close:hover {
  color: var(--text);
  border-color: var(--cyan);
}
.modal .modal-body {
  overflow-y: auto;
  padding: 1rem 1.25rem 1.25rem;
}
.info-section {
  margin-bottom: 1.1rem;
}
.info-section:last-child { margin-bottom: 0; }
.info-section h4 {
  margin: 0 0 0.45rem;
  font-size: 0.65rem;
  font-weight: 600;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--purple);
}
.info-grid {
  display: grid;
  grid-template-columns: 130px 1fr;
  row-gap: 0.3rem;
  column-gap: 0.85rem;
  font-size: 0.78rem;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
.info-grid .k {
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: 0.66rem;
  align-self: center;
}
.info-grid .v {
  color: var(--text);
  word-break: break-word;
}
.info-grid .v.muted { color: var(--muted); font-style: italic; }
.info-grid .v.fp {
  font-size: 0.66rem;
  color: var(--muted);
}
.modal .err-banner {
  padding: 0.85rem 1rem;
  margin: 1rem 1.25rem;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.35);
  color: var(--danger);
  border-radius: 8px;
  font-size: 0.8rem;
}

/* ---------- V5.3 device-lock UI ---------- */
.lock-banner {
  display: flex; align-items: center; gap: 0.6rem;
  padding: 0.6rem 0.8rem;
  margin-top: 0.25rem;
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.4);
  border-radius: 8px;
  font-size: 0.78rem;
  color: #f59e0b;
}
.lock-banner .lock-icon { font-size: 0.9rem; }
.lock-banner span[data-lock-label] {
  flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--text);
}
/* Viewport blocking overlay used on camera/screen/files pages. */
.lock-overlay {
  position: fixed; inset: 0;
  background: rgba(6, 6, 10, 0.86);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: none;
  flex-direction: column; align-items: center; justify-content: center;
  gap: 0.85rem; text-align: center; padding: 2rem;
  z-index: 20;
}
.lock-overlay.show { display: flex; }
.lock-overlay .big { font-size: 2rem; }
.lock-overlay .who {
  color: var(--text); font-size: 0.95rem;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
.lock-overlay .sub { color: var(--muted); font-size: 0.78rem; max-width: 360px; }

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
         active: str = "", chrome: bool = True,
         version: str = __VORTEX_VERSION__) -> str:
    """Wrap body in the standard page chrome (topbar + footer)."""
    if chrome:
        nav_items = [
            ("/", "Dashboard"),
            ("/pair", "Add Device"),
        ]
        if user and user.get("is_admin"):
            nav_items.append(("/admin/invites", "Invites"))
            nav_items.append(("/settings", "Settings"))
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
                  invite: str = "", username: str = "",
                  open_mode: bool = False) -> str:
    err = f'<div class="flash error">{escape(error)}</div>' if error else ""
    if open_mode:
        invite_field = (
            f'<input type="hidden" name="invite" value="{escape(invite)}">'
        )
    else:
        invite_field = (
            '<label>Invite code '
            f'<input name="invite" value="{escape(invite)}" required autofocus>'
            '</label>'
        )
    body = f"""<div class="center-wrap"><div class="auth-card">
  <div class="brand-large">
    <div class="logo"></div>
    <h1>Vortex<span class="accent">Hub</span></h1>
    <p class="subtitle">Create account</p>
  </div>
  {err}
  <form method="post" action="/register">
    {invite_field}
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


def registration_closed_page() -> str:
    """Shown when VORTEX_REGISTRATION_MODE=closed and a non-bootstrap visitor
    hits /register. No form — just a dead end back to sign-in."""
    body = """<div class="center-wrap"><div class="auth-card">
  <div class="brand-large">
    <div class="logo"></div>
    <h1>Vortex<span class="accent">Hub</span></h1>
    <p class="subtitle">Registration closed</p>
  </div>
  <div class="flash error">
    New account registration is currently disabled by the administrator.
  </div>
  <div class="footer-link">
    Already have an account? <a href="/login">Sign in</a>
  </div>
</div></div>"""
    return page("Registration closed", body, chrome=False)


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


def dashboard_page(user: dict, devices: list, online: set,
                   selfreg: bool = False) -> str:
    cards = []
    for d in devices:
        did = escape(d["id"])
        name = escape(d["name"])
        is_online = d["id"] in online
        badge_cls = "online" if is_online else "offline"
        badge_lbl = "Online" if is_online else "Offline"
        last_seen = "never" if not d.get("last_seen") else f"Δ {_ago(d['last_seen'])}"
        # V5.1 layout: status-col on the right has the online/offline badge
        # stacked over a trashcan icon-button. Info circle sits in the meta
        # row and pops a modal on click. Action row is exactly 4 buttons.
        cards.append(f"""<div class="card" data-device-id="{did}" data-device-name="{name}">
  <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:.5rem;gap:.6rem">
    <h3 style="margin:0;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">{name}</h3>
    <div class="status-col">
      <span class="badge {badge_cls}" data-status>{badge_lbl}</span>
      <form method="post" action="/devices/{did}/delete" style="margin:0">
        <button class="icon-btn danger" type="submit" title="Delete this device"
                onclick="return confirm('Unpair {name}? This cannot be undone — you will need to re-pair to control it again.')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
            <path d="M10 11v6"/><path d="M14 11v6"/>
            <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
          </svg>
        </button>
      </form>
    </div>
  </div>
  <p class="meta">id: {did}
    <button class="icon-btn info" type="button" data-info-btn title="Device info">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"/>
        <line x1="12" y1="16" x2="12" y2="12"/>
        <circle cx="12" cy="8" r="0.5" fill="currentColor"/>
      </svg>
    </button>
  </p>
  <p class="meta" data-last-seen>last seen: {escape(last_seen)}</p>
  <div class="stats" data-stats hidden></div>
  <div class="lock-banner" data-lock-banner hidden>
    <span class="lock-icon">🔒</span>
    <span data-lock-label>In use</span>
    <button class="btn btn-small" type="button" data-take-control>Take control</button>
  </div>
  <div class="actions compact" data-actions>
    <a class="btn btn-primary" href="/devices/{did}/files/">Browse</a>
    <a class="btn" href="/devices/{did}/camera">Camera</a>
    <a class="btn" href="/devices/{did}/screen">Screen</a>
    <a class="btn" href="/devices/{did}">Edit</a>
  </div>
</div>""")

    if cards:
        grid_html = f'<div class="grid">{"".join(cards)}</div>'
    else:
        grid_html = """<div class="empty">
  <h3>No devices yet</h3>
  <p>If this machine runs <code>serve.sh</code>, click
  “+ Self-Register this device”. To enroll a different phone, use
  “Pair remote device”.</p>
</div>"""

    flash = ""
    if selfreg:
        flash = ('<div class="flash success">This device was registered to '
                 'your account. If it’s running <code>serve.sh</code> it will '
                 'come online within a few seconds.</div>')

    body = f"""
{flash}
<div class="section-head">
  <h2>// Your Devices</h2>
  <div style="display:flex;gap:.6rem">
    <a class="btn btn-primary" href="/self-register">+ Self-Register this device</a>
    <a class="btn" href="/pair">Pair remote device</a>
  </div>
</div>
{grid_html}

<!-- V5.1 device-info modal. One per page; populated on demand. -->
<div class="modal-backdrop" id="device-info-modal" hidden>
  <div class="modal" role="dialog" aria-labelledby="dim-title">
    <div class="modal-head">
      <h3 id="dim-title">// Device Info</h3>
      <button class="close" type="button" data-dim-close aria-label="Close">✕</button>
    </div>
    <div class="modal-body" id="dim-body">
      <div class="info-section"><span class="muted">Loading…</span></div>
    </div>
  </div>
</div>

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

async function takeControl(deviceId) {{
  try {{
    await fetch(`/devices/${{encodeURIComponent(deviceId)}}/lock`, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{context: 'override', force: true}}),
    }});
    // We grabbed the lock but the dashboard doesn't itself use the
    // device -- immediately release so we don't block ourselves when we
    // open Camera/Screen next. The point of "take control" here is to
    // boot the stale/other holder; the next page will re-acquire.
    await fetch(`/devices/${{encodeURIComponent(deviceId)}}/lock/release`, {{
      method: 'POST',
    }});
  }} catch (e) {{}}
  pollOnline();
}}

function applyLock(card, lock) {{
  const banner = card.querySelector('[data-lock-banner]');
  const actions = card.querySelector('[data-actions]');
  if (!banner || !actions) return;
  if (lock && !lock.mine) {{
    banner.querySelector('[data-lock-label]').textContent =
      'In use — ' + (lock.label || 'another session');
    banner.hidden = false;
    actions.hidden = true;
  }} else {{
    banner.hidden = true;
    actions.hidden = false;
  }}
}}

async function pollOnline() {{
  try {{
    const r = await fetch('/api/online', {{cache: 'no-store'}});
    if (!r.ok) return;
    const data = await r.json();
    const online = new Set(data.online || []);
    const locks = data.locks || {{}};
    document.querySelectorAll('[data-device-id]').forEach(card => {{
      const id = card.dataset.deviceId;
      const badge = card.querySelector('[data-status]');
      if (!badge) return;
      if (online.has(id)) {{ badge.className = 'badge online'; badge.textContent = 'Online'; }}
      else {{
        badge.className = 'badge offline'; badge.textContent = 'Offline';
        renderStats(card, null);
      }}
      applyLock(card, locks[id]);
    }});
  }} catch (e) {{}}
}}

document.addEventListener('click', (e) => {{
  const btn = e.target.closest('[data-take-control]');
  if (!btn) return;
  const card = btn.closest('[data-device-id]');
  if (card) takeControl(card.dataset.deviceId);
}});
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

// ---- V5.1: device-info modal ----
(function() {{
  const backdrop = document.getElementById('device-info-modal');
  const body = document.getElementById('dim-body');
  const titleEl = document.getElementById('dim-title');
  if (!backdrop || !body) return;

  function open(deviceId, deviceName) {{
    titleEl.textContent = '// ' + deviceName;
    body.innerHTML = '<div class="info-section"><span class="muted">Loading device info…</span></div>';
    backdrop.hidden = false;
    backdrop.classList.add('open');
    document.addEventListener('keydown', onKey);
    fetchInfo(deviceId);
  }}
  function close() {{
    backdrop.classList.remove('open');
    backdrop.hidden = true;
    document.removeEventListener('keydown', onKey);
  }}
  function onKey(e) {{ if (e.key === 'Escape') close(); }}

  backdrop.addEventListener('click', (e) => {{
    if (e.target === backdrop) close();
  }});
  backdrop.querySelector('[data-dim-close]').addEventListener('click', close);

  document.querySelectorAll('[data-info-btn]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const card = btn.closest('[data-device-id]');
      if (!card) return;
      open(card.dataset.deviceId, card.dataset.deviceName || 'Device');
    }});
  }});

  async function fetchInfo(deviceId) {{
    try {{
      const r = await fetch(`/api/devices/${{encodeURIComponent(deviceId)}}/info`,
                            {{cache: 'no-store'}});
      const data = await r.json();
      if (!data.ok) {{
        body.innerHTML = `<div class="err-banner">${{escapeHtml(data.error || 'Unknown error')}}</div>`;
        return;
      }}
      body.innerHTML = renderInfo(data.result);
    }} catch (e) {{
      body.innerHTML = `<div class="err-banner">Fetch failed: ${{escapeHtml(e.message)}}</div>`;
    }}
  }}

  function escapeHtml(s) {{
    if (s == null) return '';
    return String(s).replace(/[&<>\"']/g, c => ({{
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }}[c]));
  }}

  function row(k, v, opts) {{
    if (v == null || v === '' || v === undefined) {{
      return `<div class="k">${{escapeHtml(k)}}</div><div class="v muted">unknown</div>`;
    }}
    const cls = (opts && opts.cls) ? ' ' + opts.cls : '';
    return `<div class="k">${{escapeHtml(k)}}</div><div class="v${{cls}}">${{escapeHtml(v)}}</div>`;
  }}

  function section(title, rows) {{
    if (!rows || rows.length === 0) return '';
    return `<div class="info-section"><h4>${{escapeHtml(title)}}</h4>` +
           `<div class="info-grid">${{rows.join('')}}</div></div>`;
  }}

  function fmtBytesLocal(n) {{
    if (n == null) return null;
    return fmtBytes(n);
  }}

  function pct(used, total) {{
    if (!used || !total) return null;
    return Math.round(used / total * 100) + '%';
  }}

  function renderInfo(d) {{
    const dev = d.device || {{}};
    const cpu = d.cpu || {{}};
    const mem = d.memory || {{}};
    const sto = d.storage || {{}};
    const bat = d.battery || {{}};
    const net = d.network || {{}};

    const memTotal = mem.total;
    const memAvail = mem.available;
    const memUsed = (memTotal && memAvail) ? memTotal - memAvail : null;

    const stoTotal = sto.total;
    const stoFree = sto.free;
    const stoUsed = (stoTotal && stoFree != null) ? stoTotal - stoFree : null;

    const sections = [
      section('Device', [
        row('Model', dev.model),
        row('Manufacturer', dev.manufacturer),
        row('Brand', dev.brand),
        row('Android', dev.android_version
          ? dev.android_version + (dev.android_sdk ? ` (SDK ${{dev.android_sdk}})` : '')
          : null),
        row('SoC', dev.soc),
        row('Kernel', dev.kernel),
        row('Hostname', d.hostname),
        row('Platform', d.platform),
      ]),
      section('CPU', [
        row('Model', cpu.model),
        row('Architecture', cpu.architecture),
        row('Cores', cpu.cores),
        row('Max Freq', cpu.max_freq_mhz ? cpu.max_freq_mhz + ' MHz' : null),
      ]),
      section('Memory', [
        row('Total', fmtBytesLocal(memTotal)),
        row('Available', fmtBytesLocal(memAvail)),
        row('Used', memUsed != null
          ? `${{fmtBytesLocal(memUsed)}} (${{pct(memUsed, memTotal)}})`
          : null),
      ]),
      section('Storage', [
        row('Root', sto.root),
        row('Total', fmtBytesLocal(stoTotal)),
        row('Free', fmtBytesLocal(stoFree)),
        row('Used', stoUsed != null
          ? `${{fmtBytesLocal(stoUsed)}} (${{pct(stoUsed, stoTotal)}})`
          : null),
      ]),
      section('Battery', [
        row('Level', bat.percentage != null ? bat.percentage + '%' : null),
        row('Status', bat.status),
        row('Plugged', bat.plugged),
        row('Temperature', bat.temperature != null ? bat.temperature + ' °C' : null),
      ]),
      section('Network', [
        row('IP', net.ip),
        row('WiFi SSID', net.wifi_ssid),
        row('WiFi RSSI', net.wifi_rssi != null ? net.wifi_rssi + ' dBm' : null),
        row('WiFi Link', net.wifi_link_speed != null ? net.wifi_link_speed + ' Mbps' : null),
        row('WiFi Freq', net.wifi_frequency != null ? net.wifi_frequency + ' MHz' : null),
      ]),
      section('System', [
        row('Agent ver', d.agent_version),
        row('Uptime', d.uptime_s != null ? fmtUptime(d.uptime_s) : null),
        row('Load 1/5/15m', d.loadavg ? d.loadavg.map(x => x.toFixed(2)).join(', ') : null),
      ]),
    ];
    if (dev.build_fingerprint) {{
      sections.push(section('Build', [
        row('Fingerprint', dev.build_fingerprint, {{cls: 'fp'}}),
      ]));
    }}
    return sections.join('');
  }}
}})();
</script>
"""
    return page("Dashboard", body, user=user, active="/")


def self_register_page(user: dict, *, default_name: str,
                       default_characteristics: str,
                       agent_config: str) -> str:
    """V5.5: enroll the machine running this hub straight from the
    browser. No pairing code — the login session is the authorization."""
    body = f"""
<div class="section-head"><h2>// Self-Register This Device</h2></div>
<div class="card" style="max-width:620px">
  <p style="margin-top:0;color:var(--muted);font-size:.85rem">
    Register <strong>the machine running this hub</strong> to your
    account. No pairing code needed — you’re already signed in. The
    credentials are written to
    <code style="color:var(--cyan)">{escape(agent_config)}</code>; a
    <code>serve.sh</code>-launched agent picks them up and the device
    comes online within seconds.
  </p>
  <form method="post" action="/self-register">
    <label>Device name
      <input name="device_name" value="{escape(default_name)}"
             placeholder="e.g. Living-room Pixel" maxlength="80" required autofocus>
    </label>
    <label>Characteristics (free-form — auto-detected, edit freely)
      <textarea name="characteristics" rows="6"
                style="resize:vertical;min-height:6rem"
                placeholder="OS, model, role, location, notes…">{escape(default_characteristics)}</textarea>
    </label>
    <button type="submit" class="btn btn-primary" style="align-self:flex-start;margin-top:.5rem">
      Register this device
    </button>
  </form>
</div>
<p style="margin-top:1rem;color:var(--muted);font-size:.78rem;max-width:620px">
  Wrong machine? This always registers the device the hub process runs
  on — not the browser you’re viewing from. To enroll a separate phone,
  use <a href="/pair">Pair remote device</a> instead.
</p>"""
    return page("Self-Register", body, user=user, active="/self-register")


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
    qr_svg = _qr_svg(cmd, box=8, border=2)
    cmd_for_js = _json_dumps_for_html(cmd)
    body = f"""
<div class="section-head"><h2>// Pairing Code</h2></div>
<div style="max-width:760px">
  <div class="code-display">{escape(code)}</div>
  <p style="color:var(--muted);font-size:.85rem;text-align:center">
    Code expires in 10 minutes. Single use.
  </p>

  <div class="qr-row">
    <div class="qr-card">{qr_svg}</div>
    <div class="qr-side">
      <h4>// Scan with phone</h4>
      <ol>
        <li>Open your phone's camera (or any QR-scanner app).</li>
        <li>Point it at this QR — your scanner will offer to copy the text.</li>
        <li>Tap to copy, switch to Termux, paste, hit enter.</li>
      </ol>
      <p style="margin:0;color:var(--muted);font-size:.7rem">
        The QR encodes the literal one-liner shell command — no app required.
      </p>
    </div>
  </div>

  <h3 style="margin-top:2rem;font-size:.85rem;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)">
    Or paste this manually:
    <button class="copy-btn" id="copy-cmd">Copy command</button>
  </h3>
  <div class="card" style="padding:1rem">
    <code id="pair-cmd" style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.78rem;color:var(--cyan);word-break:break-all;user-select:all;display:block">{escape(cmd)}</code>
  </div>

  <p style="margin-top:1.5rem;color:var(--muted);font-size:.78rem">
    Or set the code interactively when the agent prompts.
  </p>
  <p style="font-size:.85rem">
    Once paired, the device appears on your <a href="/">dashboard</a>.
  </p>
</div>
<script>
(function() {{
  const cmd = {cmd_for_js};
  const btn = document.getElementById('copy-cmd');
  if (!btn) return;
  btn.addEventListener('click', async () => {{
    try {{
      await navigator.clipboard.writeText(cmd);
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      btn.classList.add('ok');
      setTimeout(() => {{
        btn.textContent = orig;
        btn.classList.remove('ok');
      }}, 1400);
    }} catch (e) {{
      // Fallback: select the code element so the user can Ctrl-C
      const el = document.getElementById('pair-cmd');
      const sel = window.getSelection();
      const r = document.createRange();
      r.selectNodeContents(el);
      sel.removeAllRanges(); sel.addRange(r);
    }}
  }});
}})();
</script>"""
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
    <a class="btn" href="/devices/{escape(device['id'])}/camera">Camera</a>
    <a class="btn" href="/devices/{escape(device['id'])}/screen">Screen</a>
    <form method="post" action="/devices/{escape(device['id'])}/delete" style="display:inline;margin:0">
      <button class="btn btn-danger" type="submit"
              onclick="return confirm('Unpair {escape(device['name'])}? You will need to re-pair to control it again.')">
        Unpair
      </button>
    </form>
  </div>
</div>"""
    return page(device["name"], body, user=user, active="/")


def device_camera_page(user: dict, device: dict) -> str:
    """Live camera viewer. Loads the camera roster on mount, lets the user
    flip between front/back, and supports manual capture + an auto-refresh
    'live' mode (poor man's stream — termux-camera-photo isn't real video)."""
    did = device["id"]
    did_js = _json_dumps_for_html(did)
    name = device["name"]
    name_js = _json_dumps_for_html(name)
    body = f"""
{_lock_guard(did, "camera")}
<div class="section-head" style="align-items:center">
  <h2>// {escape(name)} · Camera</h2>
  <a class="btn btn-small" href="/devices/{escape(did)}">← Manage</a>
</div>

<div class="cam-toolbar">
  <select class="cam-pick" id="cam-pick" disabled>
    <option>loading…</option>
  </select>
  <button class="btn" id="cam-shoot">Capture</button>
  <label class="btn" style="cursor:pointer">
    <input type="checkbox" id="cam-live" style="vertical-align:middle;margin-right:.4rem">
    Auto-refresh
  </label>
  <button class="btn btn-primary" id="cam-stream">▶ Live stream</button>
  <button class="btn btn-small" id="cam-save" disabled>Save image</button>
  <span class="cam-status" id="cam-status">idle</span>
</div>

<div class="cam-stage" id="cam-stage">
  <div class="placeholder" id="cam-placeholder">
    No image yet — pick a camera and hit <b>Capture</b>.
  </div>
  <div class="spinner" id="cam-spinner"></div>
</div>

<p style="margin-top:1rem;color:var(--muted);font-size:.75rem">
  <strong>Capture / Auto-refresh</strong> use <code style="color:var(--cyan)">termux-camera-photo</code>
  via the Termux:API app from F-Droid. One-shot snapshots; auto-refresh polls every 6 s.
  Phone screen must be unlocked.<br>
  <strong>Live stream</strong> uses the Vortex Driver APK (V5.0) for real-time MJPEG video
  at the camera's native frame rate. Install the APK from the
  <a href="https://github.com/JasonHaddad88/VortexPortal/actions/workflows/driver-build.yml" target="_blank">latest GitHub Actions artifact</a>,
  open it on the phone, tap <em>Start service</em>, grant the camera permission.
</p>

<script>
(function() {{
  const did = {did_js};
  const pick = document.getElementById('cam-pick');
  const shoot = document.getElementById('cam-shoot');
  const live = document.getElementById('cam-live');
  const save = document.getElementById('cam-save');
  const status = document.getElementById('cam-status');
  const stage = document.getElementById('cam-stage');
  const placeholder = document.getElementById('cam-placeholder');
  const spinner = document.getElementById('cam-spinner');

  let liveTimer = null;
  let inFlight = false;
  let lastBlobUrl = null;

  function setStatus(s) {{ status.textContent = s; }}

  async function loadCameras() {{
    setStatus('loading cameras…');
    try {{
      const r = await fetch(`/api/devices/${{encodeURIComponent(did)}}/cameras`,
                            {{cache: 'no-store'}});
      const data = await r.json();
      pick.innerHTML = '';
      if (data.error || !data.cameras || !data.cameras.length) {{
        const opt = document.createElement('option');
        opt.textContent = '(none)';
        pick.appendChild(opt);
        showError(data.error || 'No cameras reported by agent');
        return;
      }}
      for (const cam of data.cameras) {{
        const opt = document.createElement('option');
        opt.value = cam.id;
        const facing = cam.facing ? ` (${{cam.facing}})` : '';
        opt.textContent = `Camera ${{cam.id}}${{facing}}`;
        pick.appendChild(opt);
      }}
      pick.disabled = false;
      setStatus('ready');
    }} catch (e) {{
      showError('Could not list cameras: ' + e.message);
    }}
  }}

  function showError(msg) {{
    placeholder.style.display = 'none';
    let err = stage.querySelector('.err');
    if (!err) {{
      err = document.createElement('div');
      err.className = 'err';
      stage.appendChild(err);
    }}
    err.textContent = msg;
    setStatus('error');
  }}

  function showImage(blob) {{
    placeholder.style.display = 'none';
    const err = stage.querySelector('.err');
    if (err) err.remove();
    let img = stage.querySelector('img');
    if (!img) {{
      img = document.createElement('img');
      stage.appendChild(img);
    }}
    if (lastBlobUrl) URL.revokeObjectURL(lastBlobUrl);
    lastBlobUrl = URL.createObjectURL(blob);
    img.src = lastBlobUrl;
    save.disabled = false;
  }}

  async function capture() {{
    if (inFlight) return;
    inFlight = true;
    spinner.classList.add('on');
    setStatus('capturing…');
    const t0 = performance.now();
    try {{
      const camId = pick.value || '0';
      const r = await fetch(
        `/devices/${{encodeURIComponent(did)}}/camera/capture` +
        `?camera_id=${{encodeURIComponent(camId)}}` +
        `&t=${{Date.now()}}`,  // bust any intermediary cache
        {{cache: 'no-store'}}
      );
      if (!r.ok) {{
        const text = await r.text();
        throw new Error(`HTTP ${{r.status}}: ${{text.slice(0, 200)}}`);
      }}
      const blob = await r.blob();
      showImage(blob);
      const ms = Math.round(performance.now() - t0);
      setStatus(`captured in ${{ms}} ms · ${{(blob.size/1024).toFixed(1)}} KB`);
    }} catch (e) {{
      showError(e.message);
      // Stop auto-refresh on error so we don't spam.
      live.checked = false;
      stopLive();
    }} finally {{
      inFlight = false;
      spinner.classList.remove('on');
    }}
  }}

  function startLive() {{
    if (liveTimer) return;
    liveTimer = setInterval(capture, 6000);
  }}
  function stopLive() {{
    if (liveTimer) {{ clearInterval(liveTimer); liveTimer = null; }}
  }}

  shoot.addEventListener('click', capture);
  live.addEventListener('change', () => {{
    if (live.checked) {{ capture(); startLive(); }}
    else {{ stopLive(); }}
  }});

  // ----- V5.0 M1: real-time MJPEG live stream from the Driver APK -----
  const stream = document.getElementById('cam-stream');
  let streaming = false;
  function startStream() {{
    // Stop snapshot polling while streaming -- they share the camera
    // hardware and the agent can only host one camera-using op at a time.
    live.checked = false;
    stopLive();
    placeholder.style.display = 'none';
    const err = stage.querySelector('.err');
    if (err) err.remove();
    let img = stage.querySelector('img');
    if (!img) {{
      img = document.createElement('img');
      stage.appendChild(img);
    }}
    if (lastBlobUrl) {{ URL.revokeObjectURL(lastBlobUrl); lastBlobUrl = null; }}
    // Bust intermediary caches; a t= query param forces a fresh stream.
    img.src = `/devices/${{encodeURIComponent(did)}}/camera/live?t=${{Date.now()}}`;
    img.onerror = () => {{
      showError('Stream failed. Is the Vortex Driver APK installed and "Start service" tapped?');
      streaming = false;
      stream.textContent = '▶ Live stream';
      stream.classList.add('btn-primary');
    }};
    streaming = true;
    stream.textContent = '■ Stop stream';
    stream.classList.remove('btn-primary');
    setStatus('streaming');
    // Save isn't meaningful during a live stream (the <img> isn't a single frame).
    save.disabled = true;
  }}
  function stopStream() {{
    streaming = false;
    stream.textContent = '▶ Live stream';
    stream.classList.add('btn-primary');
    const img = stage.querySelector('img');
    if (img) img.src = '';
    setStatus('stopped');
  }}
  stream.addEventListener('click', () => {{
    if (streaming) stopStream();
    else startStream();
  }});
  const safeName = ({name_js}).replace(/\\W+/g, '_').slice(0, 60) || 'device';
  save.addEventListener('click', () => {{
    if (!lastBlobUrl) return;
    const a = document.createElement('a');
    a.href = lastBlobUrl;
    a.download = `${{safeName}}-${{Date.now()}}.jpg`;
    document.body.appendChild(a); a.click(); a.remove();
  }});

  loadCameras();
}})();
</script>"""
    return page(f"{name} · Camera", body, user=user, active="/")


def device_screen_page(user: dict, device: dict) -> str:
    """V5.0-M2 viewer + V5.0-M3 remote control.

    The `<img>` is the live MJPEG stream; mouse events on it are
    translated to phone-screen pixel coords (using the screen-size
    queried from the agent) and POSTed as `tap` / `swipe` commands.
    Nav buttons (Back / Home / Recents) post their own commands."""
    did = device["id"]
    did_js = _json_dumps_for_html(did)
    body = f"""
{_lock_guard(did, "screen")}
<div class="section-head" style="align-items:center">
  <h2>// {escape(device['name'])} · Screen</h2>
  <a class="btn btn-small" href="/devices/{escape(did)}">← Manage</a>
</div>

<div class="cam-toolbar">
  <button class="btn btn-primary" id="scr-stream">▶ Live stream</button>
  <button class="btn" id="nav-back" title="Hardware back button">◀ Back</button>
  <button class="btn" id="nav-home" title="Home">⌂ Home</button>
  <button class="btn" id="nav-recents" title="Recent apps">▣ Recents</button>
  <button class="btn" id="nav-notifs" title="Pull down notification shade">⤓ Notifs</button>
  <span class="cam-status" id="scr-status">idle</span>
</div>

<div class="cam-stage" id="scr-stage" style="cursor:crosshair">
  <div class="placeholder" id="scr-placeholder">
    Tap <b>▶ Live stream</b> to mirror the phone's screen here.<br>
    For remote control: install <strong>Vortex Driver</strong>, tap
    <em>Arm screen sharing</em> (consent dialog), AND enable
    <em>Vortex Driver</em> in <strong>Settings → Accessibility</strong>.
  </div>
</div>

<p style="margin-top:1rem;color:var(--muted);font-size:.75rem">
  <strong>Click</strong> on the stream to tap there. <strong>Click and drag</strong> to swipe.
  <strong>Right-click</strong> for a long-press.
  Coordinates are translated from the rendered image size to real
  phone-screen pixels; the phone's actual display resolution is fetched
  from the Driver APK on page load.
</p>

<script>
(function() {{
  const did = {did_js};
  const btn = document.getElementById('scr-stream');
  const status = document.getElementById('scr-status');
  const stage = document.getElementById('scr-stage');
  const placeholder = document.getElementById('scr-placeholder');

  let streaming = false;
  // Phone's REAL screen size in pixels. Filled in via /api/.../screen-size
  // once the Driver responds. Until then, taps fall back to using the
  // <img>'s naturalWidth/Height (the captured frame size, not the real
  // screen) which is approximately correct -- ScreenEngine downscales
  // proportionally so the relative ratios match.
  let realScreen = null;

  async function loadScreenSize() {{
    try {{
      const r = await fetch(`/api/devices/${{encodeURIComponent(did)}}/screen-size`,
                            {{cache: 'no-store'}});
      const data = await r.json();
      if (data.ok && data.result && data.result.w && data.result.h) {{
        realScreen = {{w: data.result.w, h: data.result.h}};
      }}
    }} catch (e) {{}}
  }}

  function setStatus(s) {{ status.textContent = s; }}
  function showError(msg) {{
    placeholder.style.display = 'none';
    let err = stage.querySelector('.err');
    if (!err) {{ err = document.createElement('div'); err.className = 'err'; stage.appendChild(err); }}
    err.textContent = msg;
    setStatus('error');
  }}

  function startStream() {{
    placeholder.style.display = 'none';
    const err = stage.querySelector('.err'); if (err) err.remove();
    let img = stage.querySelector('img');
    if (!img) {{ img = document.createElement('img'); stage.appendChild(img); attachInput(img); }}
    img.src = `/devices/${{encodeURIComponent(did)}}/screen/live?t=${{Date.now()}}`;
    img.onerror = () => {{
      showError('Stream failed. Make sure the Vortex Driver APK is installed, the service is started, and screen sharing is armed.');
      streaming = false;
      btn.textContent = '▶ Live stream';
      btn.classList.add('btn-primary');
    }};
    streaming = true;
    btn.textContent = '■ Stop stream';
    btn.classList.remove('btn-primary');
    setStatus('streaming');
    loadScreenSize();
  }}
  function stopStream() {{
    streaming = false;
    btn.textContent = '▶ Live stream';
    btn.classList.add('btn-primary');
    const img = stage.querySelector('img');
    if (img) img.src = '';
    setStatus('stopped');
  }}
  btn.addEventListener('click', () => {{
    if (streaming) stopStream(); else startStream();
  }});

  // ---- M3: input ----

  /** Translate a (clientX, clientY) on the <img> into phone-pixel coords. */
  function toPhoneCoords(img, evt) {{
    const rect = img.getBoundingClientRect();
    const xFrac = (evt.clientX - rect.left) / rect.width;
    const yFrac = (evt.clientY - rect.top) / rect.height;
    if (xFrac < 0 || xFrac > 1 || yFrac < 0 || yFrac > 1) return null;
    const w = (realScreen && realScreen.w) || img.naturalWidth || 1080;
    const h = (realScreen && realScreen.h) || img.naturalHeight || 2400;
    return [Math.round(xFrac * w), Math.round(yFrac * h)];
  }}

  async function postInput(cmd) {{
    try {{
      const r = await fetch(`/devices/${{encodeURIComponent(did)}}/input`, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(cmd),
      }});
      if (!r.ok) {{
        const text = await r.text();
        const detail = (() => {{
          try {{ return JSON.parse(text).detail; }} catch (_) {{ return text; }}
        }})();
        setStatus('input error: ' + detail.slice(0, 120));
      }} else {{
        // brief confirm flash so the user knows the click registered
        setStatus(`${{cmd.type}} ok`);
      }}
    }} catch (e) {{
      setStatus('input failed: ' + e.message);
    }}
  }}

  // Drag-vs-tap detection: distinguish a click from a swipe by total
  // pixel movement during the press. Below DRAG_THRESHOLD = a tap;
  // above = a swipe from down-coords to up-coords.
  const DRAG_THRESHOLD_PX = 8;
  function attachInput(img) {{
    let downCoords = null;
    let downAt = 0;

    img.addEventListener('mousedown', (evt) => {{
      if (evt.button !== 0 && evt.button !== 2) return;  // only left + right
      const c = toPhoneCoords(img, evt);
      if (!c) return;
      downCoords = c;
      downAt = performance.now();
    }});

    img.addEventListener('mouseup', (evt) => {{
      if (!downCoords) return;
      const upCoords = toPhoneCoords(img, evt);
      if (!upCoords) {{ downCoords = null; return; }}
      const dx = upCoords[0] - downCoords[0];
      const dy = upCoords[1] - downCoords[1];
      const dist = Math.hypot(dx, dy);
      const elapsed = performance.now() - downAt;
      if (dist >= DRAG_THRESHOLD_PX) {{
        postInput({{
          type: 'swipe',
          from: downCoords,
          to: upCoords,
          duration_ms: Math.max(80, Math.min(800, Math.round(elapsed))),
        }});
      }} else if (evt.button === 2) {{
        postInput({{type: 'long_press', x: downCoords[0], y: downCoords[1], duration_ms: 600}});
      }} else {{
        postInput({{type: 'tap', x: downCoords[0], y: downCoords[1]}});
      }}
      downCoords = null;
    }});

    // Suppress the browser's right-click context menu so right-click can
    // mean long-press without the menu popping up.
    img.addEventListener('contextmenu', (evt) => evt.preventDefault());
  }}

  // Nav buttons. These work even without screen sharing armed -- they
  // only need the AccessibilityService.
  document.getElementById('nav-back').addEventListener('click', () => postInput({{type: 'back'}}));
  document.getElementById('nav-home').addEventListener('click', () => postInput({{type: 'home'}}));
  document.getElementById('nav-recents').addEventListener('click', () => postInput({{type: 'recents'}}));
  document.getElementById('nav-notifs').addEventListener('click', () => postInput({{type: 'notifications'}}));

  // Pre-warm the screen-size lookup on page load so taps work even before
  // streaming starts (e.g., user just wants to fire a Back press).
  loadScreenSize();
}})();
</script>"""
    return page(f"{device['name']} · Screen", body, user=user, active="/")


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
    body = f"""{_lock_guard(device['id'], "files")}
<div class="breadcrumbs">
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


def _lock_guard(device_id: str, context: str) -> str:
    """Reusable per-page lock guard (V5.3) for the camera/screen/files
    pages. Acquires the device lock on load, heartbeats every 12 s
    (lease TTL is 30 s server-side), releases on page unload via
    sendBeacon (survives unload where fetch wouldn't), and shows a
    full-viewport blocking overlay with a 'Take control' (force) button
    if the device is held by another session.

    Returns plain HTML+JS (single braces). It's injected as a *runtime
    value* into the page f-strings, so it must NOT use the {{ }} doubling
    those f-strings need.
    """
    did = _json_dumps_for_html(device_id)
    ctx = _json_dumps_for_html(context)
    js = """
<div class="lock-overlay" id="lock-overlay">
  <div class="big">🔒</div>
  <div class="who" id="lock-who">This device is in use</div>
  <div class="sub">Another session (a different browser or device) is
    controlling this device. Taking control will boot that session.</div>
  <button class="btn btn-primary" id="lock-take">Take control</button>
  <a class="btn btn-small" href="/">← Back to dashboard</a>
</div>
<script>
(function() {
  const DID = __DID__, CTX = __CTX__;
  const overlay = document.getElementById('lock-overlay');
  const who = document.getElementById('lock-who');
  let heartbeat = null;

  function startHeartbeat() {
    if (heartbeat) return;
    heartbeat = setInterval(async () => {
      try {
        const r = await fetch(`/devices/${encodeURIComponent(DID)}/lock/refresh`,
                              {method: 'POST'});
        if (r.status === 409) {
          who.textContent = 'Control was taken by another session';
          overlay.classList.add('show');
          stopHeartbeat();
        }
      } catch (e) {}
    }, 12000);
  }
  function stopHeartbeat() {
    if (heartbeat) { clearInterval(heartbeat); heartbeat = null; }
  }
  async function acquire(force) {
    try {
      const r = await fetch(`/devices/${encodeURIComponent(DID)}/lock`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({context: CTX, force: !!force}),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.acquired) {
        overlay.classList.remove('show');
        startHeartbeat();
        return true;
      }
      who.textContent = 'In use — ' + (data.label || 'another session');
      overlay.classList.add('show');
      stopHeartbeat();
      return false;
    } catch (e) {
      // Network hiccup: don't hard-block the UI -- the server-side 409
      // guards on the control endpoints still protect the hardware.
      return true;
    }
  }
  function release() {
    try {
      navigator.sendBeacon(`/devices/${encodeURIComponent(DID)}/lock/release`);
    } catch (e) {}
  }
  document.getElementById('lock-take').addEventListener('click', () => acquire(true));
  window.addEventListener('pagehide', release);
  window.addEventListener('beforeunload', release);
  acquire(false);
})();
</script>
"""
    return js.replace("__DID__", did).replace("__CTX__", ctx)


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
# V5.4: Settings page (admin only)
# ---------------------------------------------------------------------------
_SETTINGS_META = {
    "VORTEX_SYNC_URL": (
        "Remote database URL",
        "libsql://… Turso / libSQL replica endpoint. Blank = local SQLite only.",
    ),
    "VORTEX_SYNC_TOKEN": (
        "Remote database token",
        "libSQL auth token (JWT). Write-only — leave blank to keep the stored one.",
    ),
    "VORTEX_HUB_DB": (
        "Local database path",
        "Where the local SQLite / replica file lives. Blank = default (~/vortex/hub.db).",
    ),
    "APP_PORT": (
        "Hub port",
        "TCP port uvicorn binds to. Default 8000.",
    ),
    "CLOUDFLARE_TUNNEL_TOKEN": (
        "Cloudflare tunnel token",
        "Named-tunnel token for a stable public URL. Write-only — blank keeps it.",
    ),
    "VORTEX_HUB_PUBLIC_URL": (
        "Public URL override",
        "Forces the URL shown in pair links & invites (e.g. https://hub.example.com).",
    ),
    "VORTEX_LOCK_TTL": (
        "Device lock TTL (seconds)",
        "How long an in-use lock survives without a heartbeat. Minimum 5.",
    ),
    "VORTEX_SESSION_TTL": (
        "Session TTL (seconds)",
        "Login cookie lifetime. Minimum 300. Default = 30 days.",
    ),
    "VORTEX_REGISTRATION_MODE": (
        "Registration mode",
        "open = anyone may sign up · invite = code required · closed = no new accounts.",
    ),
}

_TIER_A = ["VORTEX_SYNC_URL", "VORTEX_SYNC_TOKEN", "VORTEX_HUB_DB",
           "APP_PORT", "CLOUDFLARE_TUNNEL_TOKEN"]
_TIER_B = ["VORTEX_HUB_PUBLIC_URL", "VORTEX_LOCK_TTL",
           "VORTEX_SESSION_TTL", "VORTEX_REGISTRATION_MODE"]

_SETTINGS_TEST_JS = """
<script>
(function(){
  var btn=document.getElementById('testdb');
  if(!btn)return;
  var out=document.getElementById('testdb-result');
  btn.addEventListener('click',async function(){
    var u=document.querySelector('[name=VORTEX_SYNC_URL]');
    var t=document.querySelector('[name=VORTEX_SYNC_TOKEN]');
    var url=(u?u.value:'').trim();
    var token=(t?t.value:'').trim();
    out.style.display='block';out.className='flash info';
    out.textContent='Testing connection…';
    btn.disabled=true;
    try{
      var r=await fetch('/api/settings/test-db',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:url,token:token})});
      var j=await r.json();
      if(j.ok){out.className='flash success';
        out.textContent=j.message||'Connected OK.';}
      else{out.className='flash error';out.textContent=j.error||'Failed.';}
    }catch(e){out.className='flash error';out.textContent=String(e);}
    btn.disabled=false;
  });
})();
</script>"""


def _settings_field(s: dict) -> str:
    key = s["key"]
    label, help_ = _SETTINGS_META.get(key, (key, ""))
    overridden = s.get("source") == "env"
    dis = " disabled" if overridden else ""
    hs = "text-transform:none;letter-spacing:0;color:var(--muted);font-size:.72rem;line-height:1.4"

    if key == "VORTEX_REGISTRATION_MODE":
        cur = s["value"] or "invite"
        opts = "".join(
            f'<option value="{m}"{" selected" if m == cur else ""}>{m}</option>'
            for m in ("open", "invite", "closed")
        )
        ctrl = f'<select name="{key}"{dis}>{opts}</select>'
    elif s["secret"]:
        ph = escape(s.get("secret_hint") or "not set")
        ctrl = (f'<input type="password" name="{key}" value="" '
                f'placeholder="{ph}" autocomplete="new-password"{dis}>')
    else:
        ctrl = (f'<input type="text" name="{key}" '
                f'value="{escape(s["value"])}"{dis}>')

    notes = f'<small style="{hs}">{escape(help_)}</small>'
    if s["secret"]:
        notes += (f'<small style="{hs}">Write-only — leave blank to keep '
                  f'the current value.</small>')
    if overridden:
        notes += (f'<small style="{hs};color:var(--cyan)">Overridden by an '
                  f'environment variable; edit the env to change this.</small>')
    return f'<label>{escape(label)}{ctrl}{notes}</label>'


def settings_page(user: dict, settings: list, status: dict,
                  saved: bool = False) -> str:
    by_key = {s["key"]: s for s in settings}

    flash = ""
    if saved:
        flash = ('<div class="flash success">Settings saved. '
                 'Restart-required changes apply after the next hub '
                 'restart.</div>')

    tier_a = "".join(_settings_field(by_key[k]) for k in _TIER_A
                     if k in by_key)
    tier_b = "".join(_settings_field(by_key[k]) for k in _TIER_B
                     if k in by_key)

    env_files = status.get("env_files") or []
    env_files_txt = ", ".join(env_files) if env_files else "none"

    status_rows = "".join(
        f'<div class="row"><span class="k">{escape(k)}</span>'
        f'<span class="v">{escape(str(v))}</span></div>'
        for k, v in [
            ("Version", f"V{status.get('version','')}"),
            ("DB backend", status.get("backend", "")),
            ("Local DB path", status.get("db_path", "")),
            ("Public URL", status.get("public_url", "")),
            ("Config file", status.get("config_path", "")),
            (".env files read", env_files_txt),
            ("User accounts", status.get("users", "")),
        ]
    )

    body = f"""
<div class="section-head"><h2>// Settings</h2></div>
{flash}

<div class="card" style="max-width:640px">
  <h3 style="margin:0 0 .25rem;color:var(--cyan)">Hub status</h3>
  <div class="kv">{status_rows}</div>
</div>

<form method="post" action="/settings" id="tierA">
<div class="card" style="max-width:640px;margin-top:1.25rem">
  <h3 style="margin:0 0 .25rem;color:var(--purple)">Connection &amp; database</h3>
  <div class="flash info" style="margin:.5rem 0 1rem">
    These are read once at boot — changes apply only after a hub restart.
  </div>
  {tier_a}
  <div style="display:flex;gap:.6rem;align-items:center;margin-top:.5rem">
    <button type="submit" class="btn btn-primary">Save</button>
    <button type="button" class="btn" id="testdb">Test connection</button>
  </div>
  <div id="testdb-result" class="flash" style="display:none;margin-top:.75rem"></div>
</div>
</form>

<form method="post" action="/settings">
<div class="card" style="max-width:640px;margin-top:1.25rem">
  <h3 style="margin:0 0 .25rem;color:var(--purple)">Behaviour</h3>
  <div class="flash info" style="margin:.5rem 0 1rem">
    These apply immediately — no restart needed.
  </div>
  {tier_b}
  <div style="margin-top:.5rem">
    <button type="submit" class="btn btn-primary">Save</button>
  </div>
</div>
</form>
{_SETTINGS_TEST_JS}
<style>
.kv .row {{ display:flex; justify-content:space-between; gap:1rem;
  padding:.4rem 0; border-bottom:1px solid var(--border); font-size:.85rem; }}
.kv .row:last-child {{ border-bottom:none; }}
.kv .row .k {{ color:var(--muted); text-transform:uppercase;
  letter-spacing:.08em; font-size:.7rem; }}
.kv .row .v {{ color:var(--text); word-break:break-all; text-align:right; }}
</style>"""
    return page("Settings", body, user=user, active="/settings")


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
