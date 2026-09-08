"""GET /api/v1/frameworks — what the compliance chips on an agent screen mean.

Served as its own collection rather than folded into `GET /api/v1/products`, for two
reasons. The glossary is the same for every caller, so repeating it inside each of 28
product rows would send the same fourteen entries back over and over; and the control lists
it carries are long — SOC 2 alone resolves to 45 controls — which would roughly triple a
payload the UI fetches on every page load to decide what to grey out.

Authenticated, like every other `/api/v1` read. Nothing here is customer data, but an
unauthenticated endpoint is a thing a future reader has to stop and reason about, and the
browser is holding a token already.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.products import frameworks
from backend.routes.v1_agents import get_caller

router = APIRouter(prefix="/api/v1/frameworks", tags=["Products"])


@router.get(
    "",
    summary="The compliance frameworks products map to, and what each one is",
    description="Every token that can appear in a product's `frameworks` list, with a "
                "display name, what kind of thing it is (a law, an audit report, a "
                "weakness catalogue, a method), a plain-language summary, who it binds, "
                "and the control ids a finding citing it could carry.\n\n"
                "`mapping_caveat` is the sentence a screen should show alongside: a "
                "mapping says findings carry that framework's control ids. It is not a "
                "certification and not an audit result.",
)
def list_frameworks(caller: dict = Depends(get_caller)):
    return {
        "frameworks": frameworks.all_public(),
        "count": len(frameworks.FRAMEWORKS),
        "mapping_caveat": frameworks.MAPPING_CAVEAT,
        "kinds": frameworks.KIND_LABELS,
    }


@router.get(
    "/{framework_id}",
    summary="One framework, with the controls a finding citing it could carry",
)
def get_framework(framework_id: str, caller: dict = Depends(get_caller)):
    entry = frameworks.FRAMEWORKS.get(framework_id)
    if not entry:
        # Named rather than a bare 404: the tokens are wire identifiers and easy to get
        # subtly wrong ("SOC 2" for `SOC2`, "21 CFR Part 11" for `21CFR11`), so the answer
        # says what the valid set is instead of leaving the caller to guess.
        raise HTTPException(
            status_code=404,
            detail=f"No framework '{framework_id}'. Known: "
                   + ", ".join(sorted(frameworks.FRAMEWORKS)),
        )
    return {
        "framework": frameworks.to_public_dict(entry),
        "mapping_caveat": frameworks.MAPPING_CAVEAT,
    }
