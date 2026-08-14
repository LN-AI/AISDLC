"""The target project skeleton, generated deterministically.

The export used to be a bag of `.cs` files. No `.sln`, no `.csproj`, no `Program.cs`, no DI
registration, no `appsettings.json` — nothing that makes a folder of C# into something
`dotnet build` will even look at. Every converted file was individually plausible and the
whole was not a project.

**Why no model is called here.** A `.csproj` is a list of package references with version
numbers, and a model asked for one will produce versions that look right and do not exist.
The package set is not a judgement call either: it is implied by decisions the charter
already made — `data_access` says whether EF Core is in play, `logging` says whether
Serilog is, `testing` says whether xUnit is. Deriving it is a lookup. Generating it is a
lottery. So this stage owns rows in the queue for resumability and metering symmetry, but
`llm` is False for it in `stages.py` and no `call_tool` happens.

A target stack with no template registered falls back to a single LLM scaffold unit, which
is worse but is not nothing — the alternative would be refusing to convert to anything but
.NET 8.
"""
import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# normalised target-stack fragment -> builder
_REGISTRY: Dict[str, Callable[..., List[Dict[str, str]]]] = {}


def register(keys, fn) -> None:
    for key in keys:
        _REGISTRY[key] = fn


def normalize_stack(target_stack: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (target_stack or "").lower())


def builder_for(target_stack: str) -> Optional[Callable[..., List[Dict[str, str]]]]:
    """First registered key that appears in the normalised stack string wins.

    Substring rather than equality because `target_stack` is free text a human typed:
    '.NET 8 / ASP.NET Core Web API', 'dotnet 8', 'ASP.NET Core 8 (C#)' all have to reach the
    same builder.
    """
    normalized = normalize_stack(target_stack)
    if not normalized:
        return None
    for key in sorted(_REGISTRY, key=len, reverse=True):
        if key in normalized:
            return _REGISTRY[key]
    return None


def build(target_stack: str, charter: Dict[str, Any], projects: List[Dict[str, Any]],
          config_keys: List[str]) -> List[Dict[str, str]]:
    """-> [{path, content, purpose}], or [] when no template matches the target stack."""
    fn = builder_for(target_stack)
    if fn is None:
        logger.info("No scaffold template for target stack %r", target_stack)
        return []
    try:
        return fn(charter=charter, projects=projects, config_keys=config_keys)
    except Exception:
        logger.exception("Scaffold generation failed for %r", target_stack)
        return []


from backend.services.modernization.scaffold import dotnet  # noqa: E402,F401
