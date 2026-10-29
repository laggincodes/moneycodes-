"""
WebsiteLead Hunter - Standalone Worker Daemon Process.

Usage:
    python -m app.queue.worker_daemon

Supervised by Windows Task Scheduler, NSSM, systemd, or run directly in development.
Listens for SIGINT / SIGTERM / SIGBREAK for graceful shutdown.
"""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import signal
import sys
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# Add backend directory to sys.path if not present
backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.database import init_db
from app.queue.durable_worker import DurableJobWorker


class SensitiveDataFilter(logging.Filter):
    """
    Logging filter that redacts credentials, session tokens, passwords,
    and sensitive authentication values from log records before they are formatted.
    """
    PATTERNS = [
        re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
        re.compile(r"(?i)(api[_\-]?key\s*[:=]\s*)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
        re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]{4,}", re.IGNORECASE),
        re.compile(r"(?i)(session[_\-]?token\s*[:=]\s*)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
        re.compile(r"(?i)(csrf[_\-]?token\s*[:=]\s*)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
        re.compile(r"(?i)(admin[_\-]?bootstrap[_\-]?token\s*[:=]\s*)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
        re.compile(r"(?i)(admin[_\-]?token\s*[:=]\s*)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
        re.compile(r"(?i)(x-admin-bootstrap-token\s*[:=]\s*)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
        re.compile(r"(?i)(x-csrf-token\s*[:=]\s*)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
        re.compile(r"(?i)(https?://[^:\s]+:)[^@\s]+@", re.IGNORECASE),  # Redact URL basic auth
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            msg = record.msg
            for pat in self.PATTERNS:
                msg = pat.sub(r"\1[REDACTED]", msg)
            record.msg = msg
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (self._sanitize_str(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, (list, tuple)):
                record.args = tuple(
                    self._sanitize_str(v) if isinstance(v, str) else v
                    for v in record.args
                )
        return True

    def _sanitize_str(self, val: str) -> str:
        s = str(val)
        for pat in self.PATTERNS:
            s = pat.sub(r"\1[REDACTED]", s)
        return s


def setup_worker_logging(
    log_dir: Optional[str] = None,
    max_bytes: Optional[int] = None,
    backup_count: Optional[int] = None,
) -> logging.Logger:
    """
    Configures bounded rotating file logging and console output for the worker daemon.
    Guarantees:
    - Bounded disk footprint: maxBytes (default 5MB) x backupCount (default 5) = max 25-30MB.
    - Sensitive token / credential redaction on all log records.
    - Graceful fallback to console logging if file system is unwritable or locked.
    """
    root_logger = logging.getLogger()
    # Avoid duplicate handlers on re-configuration
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    log_level = getattr(logging, os.getenv("WORKER_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root_logger.setLevel(log_level)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
    sec_filter = SensitiveDataFilter()

    # 1. Console StreamHandler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(sec_filter)
    root_logger.addHandler(console_handler)

    # 2. Bounded RotatingFileHandler
    resolved_dir = log_dir or os.getenv("WORKER_LOG_DIR")
    if not resolved_dir:
        resolved_dir = os.path.join(backend_dir, "logs")

    resolved_max_bytes = max_bytes if max_bytes is not None else int(os.getenv("WORKER_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
    resolved_backups = backup_count if backup_count is not None else int(os.getenv("WORKER_LOG_BACKUP_COUNT", "5"))

    try:
        os.makedirs(resolved_dir, exist_ok=True)
        log_file = os.path.join(resolved_dir, "worker.log")
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=resolved_max_bytes,
            backupCount=resolved_backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(sec_filter)
        root_logger.addHandler(file_handler)
    except Exception as exc:
        root_logger.warning(
            f"Could not initialize rotating file logger at '{resolved_dir}': {exc}. Operating with console logging only."
        )

    daemon_logger = logging.getLogger("worker_daemon")
    return daemon_logger


def main():
    logger = setup_worker_logging()
    logger.info("Initializing WebsiteLead Hunter standalone worker daemon...")
    init_db()

    poll_interval = float(os.getenv("WORKER_POLL_INTERVAL", "2.0"))
    lease_duration = int(os.getenv("WORKER_LEASE_DURATION", "30"))
    heartbeat_interval = float(os.getenv("WORKER_HEARTBEAT_INTERVAL", "10.0"))

    worker = DurableJobWorker(
        lease_duration_sec=lease_duration,
        heartbeat_interval_sec=heartbeat_interval,
    )

    def handle_shutdown(signum, frame):
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        logger.info(f"Received shutdown signal ({sig_name}). Requesting clean stop...")
        worker.request_stop()

    # Register portable signal handlers
    try:
        signal.signal(signal.SIGINT, handle_shutdown)
    except Exception as exc:
        logger.warning(f"Could not register SIGINT handler: {exc}")

    try:
        signal.signal(signal.SIGTERM, handle_shutdown)
    except Exception as exc:
        logger.warning(f"Could not register SIGTERM handler: {exc}")

    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, handle_shutdown)
        except Exception as exc:
            logger.warning(f"Could not register SIGBREAK handler: {exc}")

    logger.info(f"Worker {worker.worker_id} entering polling loop (interval={poll_interval}s, lease={lease_duration}s)...")
    try:
        asyncio.run(worker.run_daemon(poll_interval_sec=poll_interval))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt caught. Worker stopped.")
    except Exception as exc:
        logger.exception(f"Fatal error in worker daemon: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
