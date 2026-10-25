"""
WebsiteLead Hunter - FastAPI Application Entry Point.
"""
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from app.database import init_db
from app.queue.job_worker import JobWorker
from app.api import jobs, leads, export, discovery

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

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers
app.include_router(jobs.router)
app.include_router(leads.router)
app.include_router(export.router)
app.include_router(discovery.router)


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "WebsiteLead Hunter API",
        "database": "sqlite",
        "llm_configured": bool(os.getenv("GEMINI_API_KEY")),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "127.0.0.1")
    uvicorn.run("app.main:app", host=host, port=port, reload=True)
