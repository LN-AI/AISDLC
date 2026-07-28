"""Symmetric encryption for secrets stored at rest — currently BYOL provider API keys.

Keyed from config.SECRET_KEY via SHA-256 so no extra environment variable is
required to get started. Rotating SECRET_KEY invalidates previously stored
BYOL credentials (teams simply re-submit them via PUT /api/v1/llm-config).
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from backend import config


def _fernet() -> Fernet:
    digest = hashlib.sha256(config.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ""
