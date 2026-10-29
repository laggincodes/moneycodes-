"""
WebsiteLead Hunter - FastAPI Application Entry Point.
"""
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

load_dotenv()

from app.database import init_db
from app.queue.job_worker import JobWorker
from app.api import jobs, leads, export, discovery, auth

worker_instance = JobWorker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize database tables and recover any interrupted jobs
    init_db()
    worker_instance.recover_interrupted_jobs()
    jobs.set_worker(worker_instance)
    yield
    # Shutdown logic if needed


app = FastAPI(
    title="WebsiteLead Hunter API",
    description="Automated Client Acquisition Platform for Freelance Web Developers",
    version="1.0.0",
    lifespan=lifespan,
)

# Strict Host Whitelisting
raw_hosts = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver,testclient,*.ts.net")
ALLOWED_HOSTS = [h.strip() for h in raw_hosts.split(",") if h.strip()]

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=ALLOWED_HOSTS,
)

# Trusted Proxy Configuration (trusts only the local reverse proxy for X-Forwarded-* headers)
raw_trusted_proxies = os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1")
TRUSTED_PROXIES = [p.strip() for p in raw_trusted_proxies.split(",") if p.strip()]

app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=TRUSTED_PROXIES,
)

# Strict CORS configuration
raw_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000",
)
ALLOWED_ORIGINS = [orig.strip() for orig in raw_origins.split(",") if orig.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept", "X-Requested-With", "X-CSRF-Token", "Idempotency-Key", "X-Admin-Bootstrap-Token"],
)

# Include API routers
app.include_router(auth.router)
app.include_router(jobs.router)
app.include_router(leads.router)
app.include_router(export.router)
app.include_router(discovery.router)


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "WebsiteLead Hunter API",
    }


# Mount compiled frontend PWA static shell if available
frontend_dist = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"))
if os.path.exists(frontend_dist):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::", "0"):
        raise ValueError("Security violation: Binding to 0.0.0.0 or wildcard interfaces is strictly prohibited.")
    uvicorn.run("app.main:app", host=host, port=port, reload=True)
