"""Plan management — bundle products (agents and workflows) into a sellable tier.

A plan holds a set of product ids with optional per-product monthly quotas. An API key
points at a plan via `api_keys.plan_id`; a key with no plan falls back to its legacy
`scopes` column, so assigning plans is opt-in and nothing breaks until you do it.

RBAC mirrors backend/routes/api_keys.py: super admin only, since a plan defines what
customers can reach.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import backend.database as database
from backend.products import registry
from backend.routes.auth import is_super_admin
from backend.services.auth_service import decode_token
from backend.services.entitlement_service import validate_product_ids

router = APIRouter(prefix="/api/v1/plans", tags=["Plans"])
_bearer = HTTPBearer(auto_error=False)


def _require_auth(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> dict:
    if not creds:
        raise HTTPException(status_code=401, detail="Bearer token required")
    user = decode_token(creds.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def _require_super_admin(user: dict = Depends(_require_auth)) -> dict:
    if not is_super_admin(user):
        raise HTTPException(status_code=403, detail="Super admin role required")
    return user


class PlanProduct(BaseModel):
    product_id: str = Field(..., description="A product id from GET /api/v1/products.")
    quota_calls_per_month: Optional[int] = Field(
        None, description="Max calls per calendar month. Null = unlimited."
    )
    quota_usd_per_month: Optional[float] = Field(
        None, description="Max spend per calendar month. Null = unlimited (the key's own budget cap still applies)."
    )


class CreatePlanRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    products: List[PlanProduct] = Field(default_factory=list)


class UpdatePlanRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class SetProductsRequest(BaseModel):
    products: List[PlanProduct]


def _validate(products: List[PlanProduct]) -> None:
    unknown = validate_product_ids([p.product_id for p in products])
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown product ids: {unknown}. See GET /api/v1/products for valid ids.",
        )


@router.get("", summary="List plans")
def list_plans(include_inactive: bool = False, user: dict = Depends(_require_super_admin)):
    return {"plans": database.list_plans(include_inactive=include_inactive)}


@router.get("/products", summary="Product ids assignable to a plan")
def assignable_products(user: dict = Depends(_require_super_admin)):
    return {"products": [registry.to_public_dict(p) for p in registry.PRODUCTS]}


@router.post("", status_code=201, summary="Create a plan")
def create_plan(req: CreatePlanRequest, user: dict = Depends(_require_super_admin)):
    _validate(req.products)
    try:
        plan_id = database.create_plan(
            name=req.name,
            description=req.description or "",
            products=[p.model_dump() for p in req.products],
            actor=user.get("username", "admin"),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail=f"A plan named '{req.name}' already exists")
        raise
    return database.get_plan(plan_id)


@router.get("/{plan_id}", summary="Get a plan")
def get_plan(plan_id: str, user: dict = Depends(_require_super_admin)):
    plan = database.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


@router.patch("/{plan_id}", summary="Rename / describe / (de)activate a plan")
def update_plan(plan_id: str, req: UpdatePlanRequest, user: dict = Depends(_require_super_admin)):
    if not database.get_plan(plan_id):
        raise HTTPException(status_code=404, detail="Plan not found")
    database.update_plan(
        plan_id, name=req.name, description=req.description,
        is_active=req.is_active, actor=user.get("username", "admin"),
    )
    return database.get_plan(plan_id)


@router.put("/{plan_id}/products", summary="Replace a plan's product list")
def set_products(plan_id: str, req: SetProductsRequest, user: dict = Depends(_require_super_admin)):
    _validate(req.products)
    if not database.set_plan_products(
        plan_id, [p.model_dump() for p in req.products], actor=user.get("username", "admin")
    ):
        raise HTTPException(status_code=404, detail="Plan not found")
    return database.get_plan(plan_id)


@router.delete("/{plan_id}", summary="Delete a plan")
def delete_plan(plan_id: str, user: dict = Depends(_require_super_admin)):
    if not database.delete_plan(plan_id, actor=user.get("username", "admin")):
        raise HTTPException(status_code=404, detail="Plan not found")
    # Keys that pointed here fall back to their `scopes` column rather than losing access.
    return {"deleted": True, "plan_id": plan_id}
