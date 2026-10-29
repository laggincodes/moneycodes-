# WebsiteLead Hunter — Caddy Reverse Proxy & Private Mesh Setup

This directory contains the production Caddy reverse proxy configuration designed to expose WebsiteLead Hunter privately to your phone, desktop browser, and Windows CLI over an encrypted Tailscale mesh network.

## Architecture

```
[ Phone / Desktop / Remote CLI ]
               |
               | (Encrypted Tailscale Mesh WireGuard Tunnel)
               v
  [ Caddy Reverse Proxy (HTTPS 443) ]
  - Automated TLS Certificate (*.ts.net via Tailscale daemon / internal CA)
  - Unified Origin: serves frontend/dist and proxies /api/*
  - Overwrites untrusted X-Forwarded-* headers
               |
               | (Loopback 127.0.0.1:8000 only)
               v
  [ FastAPI Backend (Uvicorn) ]
  - ProxyHeadersMiddleware (trusts ONLY 127.0.0.1 proxy)
  - TrustedHostMiddleware (whitelists localhost, 127.0.0.1, *.ts.net)
  - HttpOnly, SameSite=Lax, Secure=true Session Cookies
  - CSRF Token Header Protection on Browser Mutations
```

## Security Guarantees

1. **Loopback Backend Binding:** FastAPI is strictly bound to `127.0.0.1:8000`. Direct connections across physical LAN interfaces (`0.0.0.0`, `192.168.x.x`) are prohibited and blocked at the code level.
2. **Untrusted Header Overwriting:** Caddy strips any incoming client `X-Forwarded-*` headers and injects genuine values from the established TCP connection.
3. **Restricted Proxy Trust:** FastAPI's `ProxyHeadersMiddleware` only evaluates forwarded headers arriving from `127.0.0.1` and `::1`.
4. **Single-Origin PWA Isolation:** Frontend assets and API endpoints reside on the exact same domain and port, eliminating cross-origin CORS attack surface.
5. **Private Network Containment:** No public DNS records, no open router ports, no port forwarding, no UPnP.

## Configuration Variables

| Variable | Description | Default |
|---|---|---|
| `TAILSCALE_HOST` | Fully Qualified Domain Name on your Tailscale tailnet (e.g. `leadfinder.tail1234.ts.net`) | `localhost` |
| `BACKEND_ADDR` | Loopback address of the running FastAPI backend | `127.0.0.1:8000` |
| `FRONTEND_DIR` | Absolute or relative path to the compiled frontend `dist` directory | `../frontend/dist` |

## Approved Future Deployment Commands (Offline Dry-Run for Phase 4)

*Do not run these commands during Phase 4 preparation. These are documented for future execution after Gate 4 approval.*

```powershell
# 1. Compile frontend production bundle
cd frontend
npm run build

# 2. Set environment variables
$env:TAILSCALE_HOST = "your-host.tailnet.ts.net"
$env:FRONTEND_DIR = "$PSScriptRoot/../frontend/dist"
$env:COOKIE_SECURE = "true"
$env:ALLOWED_HOSTS = "localhost,127.0.0.1,your-host.tailnet.ts.net,*.ts.net"

# 3. Start Caddy reverse proxy
caddy run --config Caddyfile
```
