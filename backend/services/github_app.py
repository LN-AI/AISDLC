"""GitHub App authentication — the only credential that can write a check run.

**Why this exists at all.** A personal access token can set a *commit status*: a coloured
dot with a description, which branch protection can require. It cannot create a **check
run**, and a check run is what puts a finding on the line it is about, inside the diff, in
the place a reviewer is already looking. That is a hard API restriction, not a missing
feature — `POST /repos/{owner}/{repo}/check-runs` is refused for anything but an
installation token (see `github_service.py`, where the PAT path says so).

So: an App, or annotations are impossible. `scripts/lmsdlc.py --sarif` gets findings into
the Security tab without one, which covers most of the value and none of the placement.

## What a deployment has to set up

Three environment variables, and one App created once per organisation:

    GITHUB_APP_ID                 the numeric id from the App's settings page
    GITHUB_APP_PRIVATE_KEY        the PEM, or…
    GITHUB_APP_PRIVATE_KEY_PATH   …a path to it

The App needs **Checks: Read and write** (to create the check run), **Pull requests: Read
and write** (to comment), **Contents: Read** (to read `.lmignore` and to re-read a file at
a reviewed commit), and **Metadata: Read**, which GitHub forces. Then install it on the
organisation, or on the specific repositories. Nothing here needs a webhook secret — that
is separate and already configured per credential (D-236).

Unset means unconfigured, and unconfigured means the commit-status path, unchanged. There
is no half-configured state that silently degrades: `is_configured()` is all three or none.

## Two implementation notes worth knowing before changing this

**The JWT is signed with `cryptography`, not PyJWT.** PyJWT is importable in this
environment but is not in `requirements.txt` — it arrives transitively through litellm, and
building an authentication path on a dependency nobody declared is how it disappears in the
next upgrade. `cryptography` is declared, is already used by `crypto_service`, and signs
RS256 in about fifteen lines. That also avoids adding a wheel to a project that pins
`litellm<1.92.0` specifically to stay installable on Windows.

**Installation tokens are cached, and deliberately expire early.** GitHub issues them for
an hour. They are held in memory keyed by owner/repo and dropped five minutes before
expiry, because a token that expires mid-request fails a write that has already been paid
for. In memory only: a token is a bearer credential with write access to a customer's
repository, and writing it to SQLite would mean it outlives the process that needed it.
A restart re-mints; that costs two HTTP calls and is the right trade.
"""

import base64
import json
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from backend import config

logger = logging.getLogger(__name__)

API = "https://api.github.com"

# GitHub refuses an App JWT with `exp` more than 10 minutes out. Nine, so a slow clock
# cannot push it over.
JWT_TTL_SECONDS = 9 * 60

# And `iat` is backdated, because GitHub rejects a token issued "in the future" — which a
# server whose clock is a few seconds fast otherwise produces on every request.
JWT_SKEW_SECONDS = 60

# Dropped this long before GitHub says it expires. A token that runs out mid-request fails
# a write after the review has already been paid for.
TOKEN_EARLY_EXPIRY_SECONDS = 5 * 60

_token_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}
_cache_lock = threading.Lock()


class AppAuthError(Exception):
    """The App is configured and could not be used, with a sentence saying which part."""


def _private_key_pem() -> str:
    """The PEM, from the variable or from the file it points at."""
    inline = (getattr(config, "GITHUB_APP_PRIVATE_KEY", "") or "").strip()
    if inline:
        # A PEM pasted into an environment variable usually arrives with literal `\n`
        # rather than newlines — every deployment tool does this, and the resulting
        # "could not deserialize key" says nothing about the cause.
        return inline.replace("\\n", "\n")
    path = (getattr(config, "GITHUB_APP_PRIVATE_KEY_PATH", "") or "").strip()
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise AppAuthError(
            f"GITHUB_APP_PRIVATE_KEY_PATH points at {path}, which cannot be read ({exc})."
        ) from exc


def is_configured() -> bool:
    """All of it, or none of it.

    An App id with no key, or a key with no id, cannot mint a token — and reporting that as
    "configured" would mean every pull-request review tried the App path, failed, and fell
    back, once per review, with a log line nobody reads.
    """
    try:
        return bool((getattr(config, "GITHUB_APP_ID", "") or "").strip()
                    and _private_key_pem().strip())
    except AppAuthError:
        return False


def _b64(data: bytes) -> str:
    """base64url without padding, which is what JWS requires."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt() -> str:
    """A short-lived RS256 assertion identifying the App itself.

    Not an installation token: this can list installations and mint their tokens, and
    nothing else. It cannot read a repository.
    """
    app_id = (getattr(config, "GITHUB_APP_ID", "") or "").strip()
    pem = _private_key_pem()
    if not (app_id and pem):
        raise AppAuthError(
            "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY (or …_PATH) must both be set before "
            "a check run can be created. Without them the review falls back to a commit "
            "status, which branch protection can still require but which cannot carry "
            "annotations."
        )

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as exc:                                     # noqa: BLE001
        raise AppAuthError(
            f"The GitHub App private key could not be read ({type(exc).__name__}). It must "
            f"be the PEM GitHub gave you when you generated the key, starting with "
            f"'-----BEGIN'. If it is in an environment variable, its newlines may have been "
            f"flattened."
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AppAuthError(
            "The GitHub App key is not an RSA key. GitHub issues RSA keys for Apps; this is "
            "probably a different key that ended up in the same variable."
        )

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - JWT_SKEW_SECONDS,
               "exp": now + JWT_TTL_SECONDS,
               "iss": app_id}
    signing_input = (
        _b64(json.dumps(header, separators=(",", ":")).encode())
        + "." + _b64(json.dumps(payload, separators=(",", ":")).encode())
    ).encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + _b64(signature)


def installation_id(owner: str, repo: str, jwt_token: str = "") -> int:
    """Which installation covers this repository.

    Asked per repository rather than held as configuration, because an App installed on an
    organisation gains and loses repositories without anything here being told.
    """
    token = jwt_token or app_jwt()
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            f"{API}/repos/{owner}/{repo}/installation",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
        )
    if resp.status_code == 404:
        raise AppAuthError(
            f"The GitHub App is not installed on {owner}/{repo}. Install it on the "
            f"organisation (or on that repository) from the App's settings page — the App "
            f"existing is not the same as it having access."
        )
    if resp.status_code >= 400:
        raise AppAuthError(
            f"GitHub refused to name the installation for {owner}/{repo} "
            f"(HTTP {resp.status_code}). If this is a 401, the App id and the private key "
            f"do not belong to the same App."
        )
    return int((resp.json() or {}).get("id") or 0)


def installation_token(owner: str, repo: str) -> str:
    """A token that can write to this repository. Cached until shortly before it expires."""
    key = (owner.lower(), repo.lower())
    now = time.time()
    with _cache_lock:
        cached = _token_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]

    jwt_token = app_jwt()
    inst = installation_id(owner, repo, jwt_token)
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{API}/app/installations/{inst}/access_tokens",
            headers={"Authorization": f"Bearer {jwt_token}",
                     "Accept": "application/vnd.github+json"},
        )
    if resp.status_code >= 400:
        raise AppAuthError(
            f"GitHub refused an installation token for {owner}/{repo} "
            f"(HTTP {resp.status_code}): {(resp.text or '')[:200]}"
        )
    data = resp.json() or {}
    token = data.get("token") or ""
    if not token:
        raise AppAuthError("GitHub returned no token for that installation.")

    # `expires_at` is an ISO timestamp an hour out. Parsed rather than assumed, because an
    # assumption here means a token used after it stopped working.
    ttl = 3600.0
    raw = data.get("expires_at") or ""
    if raw:
        try:
            from datetime import datetime, timezone
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            ttl = max(60.0, (when - datetime.now(timezone.utc)).total_seconds())
        except Exception:                                        # noqa: BLE001
            pass
    with _cache_lock:
        _token_cache[key] = (token, now + max(30.0, ttl - TOKEN_EARLY_EXPIRY_SECONDS))
    logger.info("Minted a GitHub App installation token for %s/%s (valid %.0fs)",
                owner, repo, ttl)
    return token


def token_for(owner: str, repo: str) -> str:
    """The installation token, or `""` when the App cannot be used.

    The one function callers should use. Swallowing the error here is deliberate: every
    caller's fallback is the commit-status path, which works, so an App problem must
    degrade the *richness* of the report and never the report itself. The reason is logged
    at warning level, once per attempt, because a silently absent check run is otherwise
    indistinguishable from a check run GitHub chose not to show.
    """
    if not (owner and repo and is_configured()):
        return ""
    try:
        return installation_token(owner, repo)
    except Exception as exc:                                     # noqa: BLE001
        logger.warning("GitHub App token unavailable for %s/%s, falling back to a commit "
                       "status: %s", owner, repo, exc)
        return ""


def status() -> Dict[str, Any]:
    """What to show on a setup screen: configured or not, and what is missing.

    Never returns the key or a token — only whether one could be produced. The JWT is
    minted to prove the key parses, which is the failure a person most needs to find out
    about before a review depends on it, and it involves no network call.
    """
    app_id = (getattr(config, "GITHUB_APP_ID", "") or "").strip()
    try:
        pem = _private_key_pem()
    except AppAuthError as exc:
        return {"configured": False, "app_id_set": bool(app_id), "key_set": False,
                "error": str(exc)}
    if not (app_id and pem):
        missing = [n for n, v in (("GITHUB_APP_ID", app_id),
                                  ("GITHUB_APP_PRIVATE_KEY", pem)) if not v]
        return {
            "configured": False,
            "app_id_set": bool(app_id),
            "key_set": bool(pem),
            "error": "",
            "note": (
                f"Not configured ({', '.join(missing)} unset). Pull-request reviews report "
                f"a commit status, which branch protection can require but which cannot "
                f"put a finding on the line it is about. A GitHub App is the only "
                f"credential GitHub accepts for a check run."
            ),
        }
    try:
        app_jwt()
    except AppAuthError as exc:
        return {"configured": False, "app_id_set": True, "key_set": True,
                "error": str(exc)}
    return {
        "configured": True, "app_id_set": True, "key_set": True, "error": "",
        "note": ("Configured. A pull-request review will create a check run with inline "
                 "annotations where the App is installed, and fall back to a commit status "
                 "where it is not."),
    }
