"""Deciding whether a package the converter asked for is real, and at what version.

The conversion stage is not allowed to write project files. That rule is right and it stays:
`runner._place_artifacts` refuses them, and the reason is on the record — the first two live
runs had components inventing `Microsoft.AspNetCore.Authentication.Cookies` and
`Microsoft.AspNetCore.Rewrite` at 8.0.0, neither of which has existed since those assemblies
were absorbed into the shared framework in 3.0. A package id that cannot restore does not fail
like a bad line of code: `NU1102` stops the restore before compilation, so there is no file and
no line to route back to anything, and the run reads as a conversion that will not converge.

What was wrong was throwing the refused file *away*. Run 8a9612e0 spent three build rounds and
three conversion calls on two `CS0103: The name 'BCrypt' does not exist` errors, while the
converter had twice submitted a corrected project file naming `BCrypt.Net-Next` 4.0.3 — a real
package, at a real version, and exactly the right answer. The catalogue in `wireup` is 23
regexes; anything outside it repeated that loop.

So the answer is not to trust the converter and not to ignore it, but to *check* it, and the
check has an authority that is neither this codebase nor a language model: nuget.org knows
which package ids and versions exist. That distinction is the whole design —

    "does this package exist?"   -> the registry. Never a model: the model that invented the
                                   id is the last thing that should be asked to confirm it,
                                   and it will agree with itself.
    "which package provides X?"  -> a model is genuinely good at this, and its answer then
                                   goes through the registry like any other.

Only the first half is implemented here; the second is a later piece and would land as another
producer of `Request` rows.

No customer code leaves the host — a package id and a version string, which is the same thing
`dotnet restore` would send from the build machine anyway. D-05 is about what this host may
*execute*, and this executes nothing.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# nuget.org's package-content endpoint. Unauthenticated, cached at the CDN, and the smallest
# thing that answers the question: 200 lists every published version, 404 means no such id.
NUGET_INDEX = "https://api.nuget.org/v3-flatcontainer/{id}/index.json"
NUGET_TIMEOUT_SECONDS = 6.0

# The tag, then its `Version` read out of whatever the tag matched. Two patterns rather than
# one, because `Include` and `Version` appear in either order and a tag may legally carry no
# version at all (central package management, and the shape a model reaches for when it is
# unsure) — an id we skip for want of a version is an id we never check.
#
# Deliberately not `wireup._PACKAGE_REF`, which captures the id alone.
_REFERENCE_ID = re.compile(
    r"<PackageReference\b[^>]*\bInclude\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_VERSION_ATTR = re.compile(r"\bVersion\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def parse_requests(content: str) -> List[Tuple[str, str]]:
    """Every `<PackageReference>` in a project file, as `(id, version)`. Pure.

    Version is `""` when the tag carries none. Order is preserved and duplicates are dropped,
    so the caller's checkpoint text reads the way the file did.
    """
    found: List[Tuple[str, str]] = []
    seen = set()
    for match in _REFERENCE_ID.finditer(content or ""):
        name = (match.group(1) or "").strip()
        if not name:
            continue
        version_match = _VERSION_ATTR.search(match.group(0))
        version = (version_match.group(1) if version_match else "").strip()
        if (name, version) in seen:
            continue
        seen.add((name, version))
        found.append((name, version))
    return found


def _version_key(version: str) -> Tuple[int, ...]:
    """Sortable form of a NuGet version, ignoring any prerelease suffix."""
    core = str(version or "").split("+")[0].split("-")[0]
    parts: List[int] = []
    for piece in core.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple((parts + [0, 0, 0, 0])[:4])


def _stable(versions: Sequence[str]) -> List[str]:
    """Published versions with prereleases removed, oldest first.

    A converted application that will not start is the problem being solved; shipping it
    against a `-preview` package is not the way to solve it.
    """
    return sorted((v for v in versions if "-" not in str(v)), key=_version_key)


def nuget_versions(name: str, timeout: float = NUGET_TIMEOUT_SECONDS) -> Optional[List[str]]:
    """Every published version of `name`, or `None` if nuget.org could not be reached.

    The three-way return is the point, and collapsing it would undo the whole design:
    `[]` means the registry answered and there is no such package, `None` means we do not
    know. The first is evidence and the second is the absence of it, and only the second is
    a question for a human.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a declared dependency
        logger.warning("httpx is unavailable, so package ids cannot be verified")
        return None
    url = NUGET_INDEX.format(id=str(name or "").strip().lower())
    try:
        response = httpx.get(url, timeout=timeout,
                             headers={"Accept": "application/json"})
    except Exception:  # noqa: BLE001 - any transport failure is "we do not know"
        logger.info("Could not reach nuget.org for %s", name, exc_info=True)
        return None
    if response.status_code == 404:
        return []
    if response.status_code != 200:
        return None
    try:
        return [str(v) for v in (response.json() or {}).get("versions") or []]
    except ValueError:
        return None


# What resolving one request concluded. `verdict` is the only field callers branch on.
#
#   known       - our own catalogue already covers this id. Ours wins; no call is made.
#   framework   - shipped inside the ASP.NET Core shared framework. Adding it is the specific
#                 mistake that produced NU1102 twice, and the registry cannot see that: some of
#                 these ids do exist, at versions that stopped years before the one asked for.
#   confirmed   - the registry has this id at this version. Added automatically.
#   substituted - the registry has the id but not that version, so the newest stable one is
#                 used instead. Added automatically, and said out loud, because picking a
#                 different version than the one asked for is a decision and not a detail.
#   missing     - the registry answered and has no such id.
#   unreachable - nuget.org could not be asked.
#
# `missing` and `unreachable` both become the same question for a human, for different reasons:
# one is a package we can show does not exist, the other is one we could not check. Neither is
# something to guess at, and a private feed makes even `missing` a reasonable thing to approve.
AUTO_ADD = ("confirmed", "substituted")
NEEDS_A_HUMAN = ("missing", "unreachable")


def resolve(requests: Sequence[Tuple[str, str]],
            known_ids: Sequence[str] = (),
            framework_ids: Sequence[str] = (),
            timeout: float = NUGET_TIMEOUT_SECONDS) -> List[Dict[str, Any]]:
    """Decide each `(id, version)` against our catalogue, then against nuget.org.

    One network call per unknown id, and only for ids neither catalogue has an opinion about —
    so a run whose converter asks for nothing unusual makes none at all.
    """
    known = {str(k).lower() for k in known_ids}
    framework = {str(f).lower() for f in framework_ids}
    results: List[Dict[str, Any]] = []
    for name, version in requests:
        row: Dict[str, Any] = {"name": name, "requested": version, "version": version}
        lowered = name.lower()
        if lowered in framework:
            row.update(verdict="framework", note=(
                "shipped inside the ASP.NET Core shared framework, so it must not be "
                "referenced as a package — this is the mistake that produced NU1102 on two "
                "earlier runs"))
        elif lowered in known:
            row.update(verdict="known", note=(
                "already covered by this platform's own package catalogue, which decides its "
                "version"))
        else:
            versions = nuget_versions(name, timeout=timeout)
            if versions is None:
                row.update(verdict="unreachable",
                           note="nuget.org could not be reached, so this is unverified")
            elif not versions:
                row.update(verdict="missing",
                           note="nuget.org has no package with this id")
            elif version and version in versions:
                row.update(verdict="confirmed",
                           note=f"nuget.org has {name} {version}")
            else:
                stable = _stable(versions)
                if not stable:
                    row.update(verdict="missing", note=(
                        f"{name} exists but has no stable release, and a converted "
                        f"application should not be pinned to a prerelease"))
                else:
                    row.update(verdict="substituted", version=stable[-1], note=(
                        f"nuget.org has {name}, but not at "
                        f"{version or 'any version the converter named'}; using the newest "
                        f"stable release, {stable[-1]}"))
        results.append(row)
    return results


def describe(rows: Sequence[Dict[str, Any]]) -> str:
    """The resolution as a human reads it, worst first."""
    order = {"missing": 0, "unreachable": 1, "framework": 2, "substituted": 3,
             "confirmed": 4, "known": 5}
    lines = []
    for row in sorted(rows, key=lambda r: order.get(r.get("verdict") or "", 9)):
        asked = row.get("requested") or "(no version)"
        lines.append(f"  - {row.get('name')} {asked} — {row.get('verdict')}: {row.get('note')}")
    return "\n".join(lines)


def approved(rows: Sequence[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """The `(id, version)` pairs a caller may hand to `wireup`."""
    return [(r["name"], r["version"]) for r in rows
            if r.get("verdict") in AUTO_ADD and r.get("version")]


def unresolved(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The rows that need somebody to say yes or no."""
    return [r for r in rows if r.get("verdict") in NEEDS_A_HUMAN]
