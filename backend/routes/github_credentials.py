"""Per-project GitHub credentials.

One `GITHUB_TOKEN` in the environment, read once at import and shared by every customer, was
the last single-tenant thing on the GitHub surface (C-76): two customers could not connect
their own repositories, one tenant's access could not be revoked without revoking everyone's,
and Auto-Fix's write scope was deployment-wide or absent.

These endpoints are the per-project version, and they follow the shape the LLM connections
already established rather than inventing a second one:

* the token is encrypted at rest with `crypto_service`, the same keying as the BYOL provider
  keys — so rotating `SECRET_KEY` invalidates them and they are re-entered, which is the
  documented trade there too;
* **the token is never returned by any read.** `has_token` and a four-character hint are
  enough for a screen to show which credential is which, and a value that cannot be read back
  cannot be leaked by a screen, a log line, or an export;
* a credential can be *tested*, because "is this token any good" should be answerable before
  a review depends on it, and the answer is stored so the screen can show it later.

`scope_pattern` exists because a customer commonly holds one token per organisation, and
reaching for the wrong one produces a 404 that reads exactly like "the repository does not
exist" — GitHub answers 404 rather than 403 for anything a credential cannot see.
"""
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import database, scope
from backend.routes.v1_agents import get_caller
from backend.services.subject_service import subject_from_caller

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github-credentials", tags=["GitHub credentials"])


class CredentialRequest(BaseModel):
    label: str = Field(
        ..., description="What this credential is, for a person choosing between them.",
        examples=["Acme org — read only", "Platform team — write access"],
    )
    token: str = Field(
        "",
        description="A GitHub personal access token or App installation token. Stored "
                    "encrypted and never returned. Leave empty when updating to keep the "
                    "existing token and change only the label or scope.",
    )
    scope_pattern: Optional[str] = Field(
        None,
        description="The owner, or `owner/repo`, this credential is for. Leave empty to make "
                    "it this project's default for any repository.",
        examples=["acme-corp", "acme-corp/patient-portal"],
    )
    can_write: bool = Field(
        False,
        description="Whether this token has write access. Auto-Fix and the modernization PR "
                    "workflow need it; reviewing does not. Declaring it is advisory — GitHub "
                    "is still the authority and will refuse a write the scopes do not allow.",
    )
    webhook_secret: str = Field(
        "",
        description="The secret GitHub signs webhook deliveries with. Stored encrypted and "
                    "never returned. Until one is set, `/api/github/webhook` refuses this "
                    "repository outright -- an endpoint that accepts unsigned events is a way "
                    "to spend a budget from the internet. Leave empty on an update to keep "
                    "the existing secret.",
    )
    project_id: Optional[str] = Field(
        None, description="Which project this belongs to. Defaults to the caller's project.",
    )


def _project_for(caller: dict, requested: Optional[str]) -> Optional[str]:
    """Which project a credential may be filed under.

    A credential grants access to somebody's source control, so it cannot be filed against a
    project the caller cannot already see — otherwise anyone could attach a token to another
    customer's project and have reviews there run under it.
    """
    visible = scope.visible_from_caller(caller)
    if requested:
        if visible is not None and requested not in visible:
            raise HTTPException(404, f"No project with id '{requested}'.")
        return requested
    try:
        return subject_from_caller(caller).project_id
    except Exception:
        return None


@router.get("", summary="List this project's GitHub credentials (tokens never returned)")
def list_credentials(caller: dict = Depends(get_caller)):
    creds = database.list_github_credentials(scope.visible_from_caller(caller))
    return {"credentials": creds, "count": len(creds)}


@router.post("", summary="Add or replace a GitHub credential")
def save_credential(req: CredentialRequest, caller: dict = Depends(get_caller)):
    if not req.token.strip():
        raise HTTPException(400, "A token is required when creating a credential.")
    project_id = _project_for(caller, req.project_id)
    actor = caller.get("username") or caller.get("team") or "unknown"
    saved = database.save_github_credential(
        project_id=project_id, label=req.label, token=req.token.strip(),
        scope_pattern=req.scope_pattern, can_write=req.can_write, created_by=actor,
    )
    if req.webhook_secret.strip():
        database.set_github_webhook_secret(saved["id"], req.webhook_secret.strip())
        saved = next((c for c in database.list_github_credentials(None)
                      if c["id"] == saved["id"]), saved)
    logger.info("GitHub credential %s added by %s for project %s",
                saved.get("id"), actor, project_id)
    # This is the credential that lets the platform write to a customer's repository, so
    # who added one and whether it can write are exactly what an auditor asks. The label and
    # the scope, never the token.
    database.audit_event(
        "github_credential_created", "github_credential", saved.get("id", ""),
        actor=actor, project_id=project_id,
        details=f"label={req.label} scope={req.scope_pattern or '*'} "
                f"can_write={bool(req.can_write)}"
                + (" webhook_secret_set" if req.webhook_secret.strip() else ""))
    return {"credential": saved}


@router.put("/{credential_id}", summary="Update a credential's label, scope, or token")
def update_credential(credential_id: str, req: CredentialRequest,
                      caller: dict = Depends(get_caller)):
    existing = database.get_github_credential(credential_id)
    if not existing:
        raise HTTPException(404, f"No credential with id '{credential_id}'.")
    visible = scope.visible_from_caller(caller)
    if visible is not None and existing.get("project_id") not in visible:
        raise HTTPException(404, f"No credential with id '{credential_id}'.")
    saved = database.save_github_credential(
        project_id=existing.get("project_id"), label=req.label, token=req.token.strip(),
        scope_pattern=req.scope_pattern, can_write=req.can_write,
        credential_id=credential_id,
    )
    if req.webhook_secret.strip():
        database.set_github_webhook_secret(credential_id, req.webhook_secret.strip())
        saved = next((c for c in database.list_github_credentials(None)
                      if c["id"] == credential_id), saved)
    # A replacement is as interesting as a creation: the token changed, and the row cannot
    # say what it changed to.
    database.audit_event(
        "github_credential_updated", "github_credential", credential_id,
        actor=(caller.get("username") or caller.get("team") or "unknown"),
        project_id=existing.get("project_id"),
        details=f"label={req.label} scope={req.scope_pattern or '*'} "
                f"can_write={bool(req.can_write)} token_replaced=1"
                + (" webhook_secret_set" if req.webhook_secret.strip() else ""))
    return {"credential": saved}


@router.delete("/{credential_id}", summary="Delete a credential")
def delete_credential(credential_id: str, caller: dict = Depends(get_caller)):
    existing = database.get_github_credential(credential_id)
    if not existing:
        raise HTTPException(404, f"No credential with id '{credential_id}'.")
    visible = scope.visible_from_caller(caller)
    if visible is not None and existing.get("project_id") not in visible:
        raise HTTPException(404, f"No credential with id '{credential_id}'.")
    database.delete_github_credential(credential_id)
    logger.info("GitHub credential %s deleted by %s",
                credential_id, caller.get("username") or caller.get("team"))
    database.audit_event(
        "github_credential_deleted", "github_credential", credential_id,
        actor=(caller.get("username") or caller.get("team") or "unknown"),
        project_id=existing.get("project_id"),
        details=f"label={existing.get('label', '')}")
    return {"deleted": True, "id": credential_id}


class TestRequest(BaseModel):
    repo_url: Optional[str] = Field(
        None,
        description="Test against a specific repository. Without it the test only confirms "
                    "the token is valid and names the account it belongs to — which does not "
                    "prove it can see the repository you care about.",
        examples=["https://github.com/acme-corp/patient-portal"],
    )


@router.post("/{credential_id}/test", summary="Check that a credential works")
def test_credential(credential_id: str, req: TestRequest,
                    caller: dict = Depends(get_caller)):
    """Ask GitHub who this token is, and optionally whether it can see one repository.

    Two questions, and the second is the one that matters: a token can be perfectly valid and
    still not see the repository someone is about to point a review at, and those two failures
    are indistinguishable from the 404 a review would produce. Testing against the actual
    repository is therefore offered, and the result is recorded so the screen can show it
    without re-testing.
    """
    existing = database.get_github_credential(credential_id)
    if not existing:
        raise HTTPException(404, f"No credential with id '{credential_id}'.")
    visible = scope.visible_from_caller(caller)
    if visible is not None and existing.get("project_id") not in visible:
        raise HTTPException(404, f"No credential with id '{credential_id}'.")

    from backend.services import crypto_service
    from backend.services.github_service import GitHubService

    token = crypto_service.decrypt(existing.get("token_encrypted") or "")
    if not token:
        detail = ("The stored token could not be decrypted. This happens when SECRET_KEY has "
                  "changed since it was saved — re-enter the token.")
        database.record_github_credential_test(credential_id, "failed", detail)
        return {"ok": False, "detail": detail}

    gh = GitHubService(token=token, token_source="project",
                       token_label=existing.get("label") or "")
    try:
        with httpx.Client(headers=gh.headers, timeout=20) as client:
            who = client.get(f"{gh.BASE_URL}/user")
            if who.status_code == 401:
                detail = "GitHub rejected the token — it is invalid, expired or revoked."
                database.record_github_credential_test(credential_id, "failed", detail)
                return {"ok": False, "detail": detail}
            gh._raise_friendly(who)
            account = who.json().get("login", "unknown")
            scopes = who.headers.get("x-oauth-scopes", "")

        result = {"ok": True, "account": account, "scopes": scopes or "(fine-grained token)"}

        if req.repo_url:
            repo_gh = GitHubService(token=token, token_source="project",
                                    token_label=existing.get("label") or "")
            try:
                overview = repo_gh.get_repo_overview(req.repo_url)
                result["repository"] = overview.get("repo")
                result["repository_readable"] = True
            except Exception as exc:
                result["repository_readable"] = False
                result["repository_detail"] = str(exc)[:600]
                detail = f"Token is valid as {account}, but cannot read {req.repo_url}."
                database.record_github_credential_test(credential_id, "partial", detail)
                result["ok"] = False
                result["detail"] = detail
                return result

        detail = f"Valid as {account}" + (
            f", can read {result.get('repository')}" if req.repo_url else "")
        database.record_github_credential_test(credential_id, "ok", detail)
        result["detail"] = detail
        return result
    except Exception as exc:
        detail = str(exc)[:600]
        database.record_github_credential_test(credential_id, "failed", detail)
        logger.exception("GitHub credential test failed for %s", credential_id)
        return {"ok": False, "detail": detail}


@router.get("/resolve", summary="Which credential would be used for a repository")
def resolve_for(repo_url: str, caller: dict = Depends(get_caller)):
    """Answers "which token will this review use", before running one.

    Exists because that question was unanswerable, and it is the first one worth asking when a
    repository 404s. Names the credential; never returns it.
    """
    from backend.services.source_bundle import _owner_repo
    try:
        project_id = subject_from_caller(caller).project_id
    except Exception:
        project_id = None
    resolved = database.resolve_github_token(project_id, _owner_repo(repo_url))
    return {
        "repo": _owner_repo(repo_url),
        "token_source": resolved["source"],
        "label": resolved.get("label"),
        "credential_id": resolved.get("credential_id"),
        "authenticated": bool(resolved.get("token")),
    }
