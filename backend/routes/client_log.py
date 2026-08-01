"""Receives error reports from the frontend so they land in the same server
logs as everything else. The frontend is responsible for redacting anything
sensitive before it gets here (see frontend/logger.js) — this endpoint just
caps sizes defensively and never echoes anything back beyond a status."""
import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, field_validator

logger = logging.getLogger("frontend")
router = APIRouter(prefix="/api/client-log", tags=["Client Logging"])

_MAX_FIELD_LEN = 2000
_VALID_LEVELS = {"error", "warn", "info"}


class ClientLogEntry(BaseModel):
    level: str = "error"
    message: str
    source: Optional[str] = None
    url: Optional[str] = None
    line: Optional[int] = None
    col: Optional[int] = None
    stack: Optional[str] = None

    @field_validator("message", "source", "url", "stack", mode="before")
    @classmethod
    def _truncate(cls, v):
        if isinstance(v, str):
            return v[:_MAX_FIELD_LEN]
        return v


@router.post("")
async def log_client_event(entry: ClientLogEntry):
    level = entry.level if entry.level in _VALID_LEVELS else "error"
    log_fn = {"error": logger.error, "warn": logger.warning, "info": logger.info}[level]
    log_fn(
        "%s | source=%s url=%s line=%s col=%s stack=%s",
        entry.message, entry.source or "-", entry.url or "-", entry.line, entry.col,
        entry.stack or "-",
    )
    return {"status": "logged"}
