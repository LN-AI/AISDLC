import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import jwt as pyjwt

from backend.config import SECRET_KEY, JWT_EXPIRY_HOURS

logger = logging.getLogger(__name__)

# Roles live here rather than in routes/auth.py because services need them too — the
# governance guards check them to decide who may bind a request to a project — and a
# service importing a router would invert the layering. routes/auth.py re-exports these,
# so `from backend.routes.auth import is_super_admin` keeps working.
#
# "admin" is a legacy alias for "super_admin": pre-existing installs seeded a user with
# that role, so both must be accepted.
VALID_ROLES = ("super_admin", "project_admin", "admin", "reviewer", "security", "compliance", "developer")
SUPER_ADMIN_ROLES = ("super_admin", "admin")


def is_super_admin(user: Optional[Dict[str, Any]]) -> bool:
    return bool(user) and user.get("role") in SUPER_ADMIN_ROLES


def hash_password(password: str) -> str:
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + ":" + key.hex()


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, key_hex = stored_hash.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


def create_token(user_id: str, username: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.utcnow(),
    }
    return pyjwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return pyjwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        logger.info("JWT rejected: expired")
        return None
    except pyjwt.InvalidTokenError as e:
        logger.warning("JWT rejected: invalid (%s)", e)
        return None
