# Vortex Hub as an always-on cloud relay.
#
# A relay node is just the hub talking to your shared Turso database. It
# needs NO Cloudflare tunnel (Fly / a reverse proxy provide public HTTPS)
# and NO co-located agent (a headless relay isn't a controllable device).
# It uses the pure-Python Turso-over-HTTP backend, so there's no Rust /
# libsql build step -- the image stays tiny and builds in seconds.
#
# Provide at deploy time (Fly secrets / VM env):
#   VORTEX_SYNC_URL        libsql://your-db.turso.io   (your account DB)
#   VORTEX_SYNC_TOKEN      <turso auth token>
#   VORTEX_HUB_PUBLIC_URL  https://your-relay.example  (so it advertises
#                          the right URL to devices via node_endpoints)
FROM python:3.12-slim

WORKDIR /app

# Exactly the hub's runtime deps (remote-Turso path = httpx; no libsql).
RUN pip install --no-cache-dir \
    "fastapi<0.100" "pydantic<2" "uvicorn[standard]" \
    websockets httpx python-multipart qrcode

# The hub package is self-contained (imports nothing outside hub/).
COPY hub/ ./hub/

ENV APP_PORT=8080 \
    PYTHONUNBUFFERED=1
EXPOSE 8080

# --proxy-headers + --forwarded-allow-ips so the hub trusts the platform's
# TLS terminator (needed for the wss:// agent upgrade behind HTTPS).
CMD ["sh", "-c", "uvicorn hub.app:app --host 0.0.0.0 --port ${APP_PORT} --proxy-headers --forwarded-allow-ips=*"]
