from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from pydantic import BaseModel

from backend.services.auth_service import hash_password, verify_password, create_token, decode_token
import backend.database as database

router = APIRouter(prefix="/api/auth", tags=["Auth"])
_security = HTTPBearer(auto_error=False)

# "admin" is kept only as a legacy alias for "super_admin" (pre-existing installs already
# have a seed user with role "admin") — new users should be created as "super_admin" or
# "project_admin" going forward. See is_super_admin() below.
VALID_ROLES = ("super_admin", "project_admin", "admin", "reviewer", "security", "compliance", "developer")
SUPER_ADMIN_ROLES = ("super_admin", "admin")


def is_super_admin(user: Optional[dict]) -> bool:
    return bool(user) and user.get("role") in SUPER_ADMIN_ROLES


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    email: str
    full_name: str = ""
    password: str
    role: str = "developer"


def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security)):
    if not credentials:
        return None
    return decode_token(credentials.credentials)


def require_auth(user=Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin(user=Depends(require_auth)):
    if not is_super_admin(user):
        raise HTTPException(status_code=403, detail="Super admin role required")
    return user


@router.post("/login")
def login(req: LoginRequest):
    user = database.get_user_by_username(req.username)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.get("is_active"):
        raise HTTPException(status_code=403, detail="Account is deactivated")
    database.update_last_login(user["id"])
    token = create_token(user["id"], user["username"], user["role"])
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "full_name": user.get("full_name", ""),
            "role": user["role"],
        },
    }


@router.get("/me")
def get_me(user=Depends(require_auth)):
    db_user = database.get_user_by_id(user["sub"])
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": db_user["id"],
        "username": db_user["username"],
        "email": db_user["email"],
        "full_name": db_user.get("full_name", ""),
        "role": db_user["role"],
        "last_login": db_user.get("last_login"),
    }


@router.get("/users")
def list_users(user=Depends(require_admin)):
    return {"users": database.list_users()}


@router.post("/users")
def create_user(req: CreateUserRequest, user=Depends(require_admin)):
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}")
    if database.get_user_by_username(req.username):
        raise HTTPException(status_code=400, detail="Username already exists")
    pw_hash = hash_password(req.password)
    new_user = database.create_user(req.username, req.email, pw_hash, req.role, req.full_name)
    return {"message": "User created", "user": new_user}


@router.delete("/users/{user_id}")
def deactivate_user(user_id: str, user=Depends(require_admin)):
    if user_id == user["sub"]:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
    if not database.get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    database.deactivate_user(user_id)
    return {"message": "User deactivated"}
