"""App-wide logging setup — the single place logging is configured.

Every module gets its logger the normal stdlib way: `logging.getLogger(__name__)`.
This module only owns *how* those records are formatted/routed, plus a
per-request correlation id (`request_id`) that main.py's HTTP middleware
populates so every log line emitted while handling a request can be tied
back to it.

Level and destination are controlled by env vars so ops can turn up verbosity
in a running deployment without a code change:
  LOG_LEVEL  - DEBUG | INFO | WARNING | ERROR (default INFO)
  LOG_FILE   - optional path; when set, logs also go to this file (rotating)
"""
import logging
import logging.handlers
import os
import sys
from contextvars import ContextVar

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [req:%(request_id)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def configure_logging() -> None:
    """Idempotent — safe to call from every entry point (main.py, scripts, tests)."""
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    request_id_filter = _RequestIdFilter()
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # Log content often includes text pulled from GitHub/Jira (titles, comments) that
    # isn't guaranteed ASCII. Legacy Windows console codepages can't encode arbitrary
    # unicode, so reconfigure stdout to replace unencodable characters instead of
    # crashing the logging call itself.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    handlers: list[logging.Handler] = []

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(request_id_filter)
    handlers.append(console_handler)

    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(request_id_filter)
        handlers.append(file_handler)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = handlers

    # Third-party libraries are chatty at DEBUG/INFO — keep them at WARNING unless
    # the operator explicitly asked for DEBUG-level app logs.
    if level > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "openai", "anthropic", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
