"""Known-vulnerable dependencies, found by a database rather than asked of a model.

This is the other half of the deterministic pre-pass. The first half already shipped:
hardcoded credentials come from the ingest redaction scan, so the secret is found by a regex
that either matched or did not (`source_bundle.secret_findings`). Dependencies are the same
shape of problem and had no equivalent — the Code Review prompt lists "pinned to known-
vulnerable versions" under what to detect, which asks a language model to recall a CVE
database from training data. It will be confidently wrong about versions, cannot know
anything published after its cutoff, and costs tokens to be wrong.

`osv.dev` answers the same question exactly. It is free, needs no key, aggregates GHSA,
PYSEC, RUSTSEC, the Go vulnerability database and CVE, and takes a batch of
`(ecosystem, name, version)` in one request. So the platform's own rule — D-04, never ask a
model for what a parser answers exactly — applies here as directly as it did to
modernization's dependency graph.

Three deliberate limits, each stated rather than hidden:

**Only concrete versions are queried.** `^1.2.3` in a `package.json` is a *range*, not a
version, and OSV cannot answer a range. So a lockfile is used where one exists, and a
manifest carrying only ranges is reported as *unresolved* rather than guessed at — a guessed
version produces both false alarms and false silence.

**It fails open.** No network, a timeout, or an OSV outage yields no findings and a recorded
reason. A dependency check that turns a working review into an error is a check that gets
switched off.

**It reports the manifest line, not the installed tree.** Transitive dependencies appear only
where a lockfile lists them. A `requirements.txt` naming five packages is five queries, not
the fifty that actually get installed — which is honest, and is why the finding says which
file and line it came from.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns"

# OSV answers a batch in one request. Kept well under any documented limit, and batched
# rather than looped because a 200-package lockfile would otherwise be 200 round trips.
OSV_BATCH_SIZE = 100
OSV_TIMEOUT_SECONDS = 20.0

# How many vulnerability records to expand for detail. The batch call returns ids only;
# turning each into a title and a CVSS vector is one request per id, so the worst offenders
# get expanded and the rest are counted. A finding naming 40 CVEs is not more actionable than
# one naming the four worst and saying there are 36 more.
MAX_DETAIL_LOOKUPS = 25

# Severity words OSV puts in `database_specific.severity`, mapped to this platform's 1-5.
_SEVERITY_BY_WORD = {
    "CRITICAL": 5,
    "HIGH": 4,
    "MODERATE": 3,
    "MEDIUM": 3,
    "LOW": 2,
}
_RISK_LEVEL = {5: "Critical", 4: "High", 3: "Moderate", 2: "Minor", 1: "Low"}

# A CVSS base score is preferred over the word when a vector is present, because the word is
# the publishing database's own grading and varies between them for the same CVE.
_CVSS_BANDS = ((9.0, 5), (7.0, 4), (4.0, 3), (0.1, 2))


# ── Manifest parsing ──────────────────────────────────────────────────────
#
# One parser per file shape, each returning `(name, version, line_number)` triples and
# nothing else. A range, a URL, a local path or a variable reference yields no version, and
# the caller records it as unresolved.

_PY_REQ_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*==\s*(?P<version>[A-Za-z0-9._+-]+)")
_PY_LOOSE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(?P<op>[<>~!^]=?|===)\s*(?P<version>\S+)")
_GO_MOD_RE = re.compile(r"^\s*(?P<name>[^\s]+/[^\s]+)\s+v(?P<version>[0-9][^\s]*)")
_CSPROJ_RE = re.compile(
    r'<PackageReference\s+Include="(?P<name>[^"]+)"\s+Version="(?P<version>[^"]+)"', re.I)
_GEMLOCK_RE = re.compile(r"^\s{4}(?P<name>[A-Za-z0-9._-]+) \((?P<version>[0-9][^)]*)\)")


def _parse_requirements(text: str) -> Tuple[List[tuple], List[tuple]]:
    """`requirements.txt` and friends. Only `==` pins are a version."""
    resolved, unresolved = [], []
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-", "git+", "http")):
            continue
        pin = _PY_REQ_RE.match(line)
        if pin:
            resolved.append((pin.group("name"), pin.group("version"), i))
            continue
        loose = _PY_LOOSE_RE.match(line)
        if loose:
            unresolved.append((loose.group("name"),
                               f"{loose.group('op')}{loose.group('version')}", i))
    return resolved, unresolved


def _parse_package_lock(text: str) -> Tuple[List[tuple], List[tuple]]:
    """`package-lock.json` — the best npm signal, because it names exact versions including
    transitive ones. Both lockfile layouts are handled: v1's nested `dependencies` and v2/v3's
    flat `packages` keyed by install path."""
    try:
        data = json.loads(text)
    except ValueError:
        return [], []
    resolved: List[tuple] = []

    for path, entry in (data.get("packages") or {}).items():
        if not path or not isinstance(entry, dict):
            continue                       # "" is the project itself
        version = entry.get("version")
        name = entry.get("name") or path.split("node_modules/")[-1]
        if version and name:
            resolved.append((name, version, None))

    def walk(deps):
        for name, entry in (deps or {}).items():
            if isinstance(entry, dict):
                if entry.get("version"):
                    resolved.append((name, entry["version"], None))
                walk(entry.get("dependencies"))

    walk(data.get("dependencies"))
    # De-duplicate: a lockfile lists the same package at the same version many times.
    seen, unique = set(), []
    for name, version, line in resolved:
        if (name, version) not in seen:
            seen.add((name, version))
            unique.append((name, version, line))
    return unique, []


def _parse_package_json(text: str) -> Tuple[List[tuple], List[tuple]]:
    """`package.json` — almost entirely ranges, so almost entirely unresolved. Parsed anyway
    so a project with no lockfile is *told* its dependencies were not checked, rather than
    quietly getting a clean report."""
    try:
        data = json.loads(text)
    except ValueError:
        return [], []
    resolved, unresolved = [], []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(section) or {}).items():
            if not isinstance(spec, str):
                continue
            if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?", spec.strip()):
                resolved.append((name, spec.strip(), None))
            else:
                unresolved.append((name, spec, None))
    return resolved, unresolved


def _parse_go_mod(text: str) -> Tuple[List[tuple], List[tuple]]:
    resolved = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("//") or " => " in line:
            continue
        m = _GO_MOD_RE.match(line.replace("require ", "\t"))
        if m:
            resolved.append((m.group("name"), m.group("version"), i))
    return resolved, []


def _parse_pom(text: str) -> Tuple[List[tuple], List[tuple]]:
    """Maven, without an XML parser: `<dependency>` blocks whose version is a literal.
    A `${property}` version is unresolved — resolving it needs the whole POM hierarchy."""
    resolved, unresolved = [], []
    for block in re.finditer(r"<dependency>(.*?)</dependency>", text, re.S):
        body = block.group(1)
        gid = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", body)
        aid = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", body)
        ver = re.search(r"<version>\s*([^<]+?)\s*</version>", body)
        if not (gid and aid):
            continue
        name = f"{gid.group(1)}:{aid.group(1)}"
        line = text[:block.start()].count("\n") + 1
        if ver and "${" not in ver.group(1):
            resolved.append((name, ver.group(1), line))
        else:
            unresolved.append((name, ver.group(1) if ver else "(inherited)", line))
    return resolved, unresolved


def _parse_csproj(text: str) -> Tuple[List[tuple], List[tuple]]:
    resolved, unresolved = [], []
    for m in _CSPROJ_RE.finditer(text):
        line = text[:m.start()].count("\n") + 1
        version = m.group("version")
        (unresolved if "$(" in version else resolved).append(
            (m.group("name"), version, line))
    return resolved, unresolved


def _parse_gemfile_lock(text: str) -> Tuple[List[tuple], List[tuple]]:
    resolved = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = _GEMLOCK_RE.match(line)
        if m:
            resolved.append((m.group("name"), m.group("version"), i))
    return resolved, []


def _parse_composer_lock(text: str) -> Tuple[List[tuple], List[tuple]]:
    try:
        data = json.loads(text)
    except ValueError:
        return [], []
    resolved = []
    for section in ("packages", "packages-dev"):
        for entry in data.get(section) or []:
            name, version = entry.get("name"), (entry.get("version") or "").lstrip("v")
            if name and version:
                resolved.append((name, version, None))
    return resolved, []


# Basename (lowercased) -> (OSV ecosystem, parser). A lockfile is listed before its manifest
# so `_pick_manifests` can prefer it.
MANIFESTS: Dict[str, Tuple[str, Any]] = {
    "requirements.txt": ("PyPI", _parse_requirements),
    "requirements-dev.txt": ("PyPI", _parse_requirements),
    "package-lock.json": ("npm", _parse_package_lock),
    "package.json": ("npm", _parse_package_json),
    "go.mod": ("Go", _parse_go_mod),
    "pom.xml": ("Maven", _parse_pom),
    "gemfile.lock": ("RubyGems", _parse_gemfile_lock),
    "composer.lock": ("Packagist", _parse_composer_lock),
}
_CSPROJ_ECOSYSTEM = "NuGet"

# When both are present in the same directory, the lockfile wins: it carries exact versions
# and the transitive tree, where the manifest carries ranges and only direct dependencies.
_SUPERSEDED_BY = {"package.json": "package-lock.json"}


def _pick_manifests(files: List[Dict[str, Any]]) -> List[Tuple[str, str, Any, str]]:
    """-> [(path, ecosystem, parser, content)], with a manifest dropped where its lockfile
    sits beside it."""
    by_dir: Dict[str, Dict[str, Dict[str, Any]]] = {}
    extras: List[Tuple[str, str, Any, str]] = []
    for f in files or []:
        path = f.get("path") or ""
        base = path.rsplit("/", 1)[-1].lower()
        if base.endswith(".csproj") or base.endswith(".vbproj"):
            extras.append((path, _CSPROJ_ECOSYSTEM, _parse_csproj, f.get("content") or ""))
            continue
        if base in MANIFESTS:
            directory = path.rsplit("/", 1)[0] if "/" in path else ""
            by_dir.setdefault(directory, {})[base] = f

    picked: List[Tuple[str, str, Any, str]] = []
    for directory, found in by_dir.items():
        for base, f in found.items():
            superseding = _SUPERSEDED_BY.get(base)
            if superseding and superseding in found:
                logger.info("Skipping %s — %s beside it has exact versions",
                            f.get("path"), superseding)
                continue
            ecosystem, parser = MANIFESTS[base]
            picked.append((f.get("path") or "", ecosystem, parser, f.get("content") or ""))
    return picked + extras


# ── OSV ───────────────────────────────────────────────────────────────────


def _osv_batch(queries: List[dict], timeout: float = OSV_TIMEOUT_SECONDS) -> List[dict]:
    """One batch request. Raises on transport failure; the caller fails open."""
    import httpx

    results: List[dict] = []
    with httpx.Client(timeout=timeout) as client:
        for start in range(0, len(queries), OSV_BATCH_SIZE):
            chunk = queries[start:start + OSV_BATCH_SIZE]
            resp = client.post(OSV_BATCH_URL, json={"queries": chunk})
            resp.raise_for_status()
            results.extend(resp.json().get("results") or [])
    return results


def _osv_detail(vuln_ids: List[str], timeout: float = OSV_TIMEOUT_SECONDS) -> Dict[str, dict]:
    """Titles, aliases and severities for a bounded number of ids."""
    import httpx

    out: Dict[str, dict] = {}
    with httpx.Client(timeout=timeout) as client:
        for vid in vuln_ids[:MAX_DETAIL_LOOKUPS]:
            try:
                resp = client.get(f"{OSV_VULN_URL}/{vid}")
                if resp.is_success:
                    out[vid] = resp.json()
            except Exception:
                logger.info("Could not expand %s — counting it without detail", vid)
    return out


def _severity_of(record: dict) -> int:
    """1-5 from an OSV record. A CVSS base score beats the publishing database's own word,
    because that word differs between databases for the same CVE."""
    for entry in record.get("severity") or []:
        vector = entry.get("score") or ""
        # A v3/v4 vector carries no base score, so the score is derived from the word below
        # unless the record supplies a numeric one.
        if isinstance(vector, (int, float)):
            for threshold, sev in _CVSS_BANDS:
                if float(vector) >= threshold:
                    return sev
    word = ((record.get("database_specific") or {}).get("severity") or "").upper()
    return _SEVERITY_BY_WORD.get(word, 3)


def _cvss_vector(record: dict) -> str:
    for entry in record.get("severity") or []:
        score = entry.get("score")
        if isinstance(score, str) and score.startswith("CVSS:"):
            return score
    return ""


def _cve_of(record: dict) -> str:
    for alias in record.get("aliases") or []:
        if str(alias).startswith("CVE-"):
            return alias
    return record.get("id", "")


# ── The public entry point ────────────────────────────────────────────────


def scan(files: List[Dict[str, Any]], *, batch=None, detail=None) -> Dict[str, Any]:
    """Findings for known-vulnerable dependencies, plus what could not be checked.

    `batch` / `detail` are injection points for the offline suite — the real functions hit
    the network, and a test that needs the network is a test that fails on a train.

    Returns `{findings, checked, unresolved, manifests, error}`. `error` being set means the
    scan could not run; `findings` is then empty and the caller reports it rather than
    treating an outage as a clean bill of health.
    """
    manifests = _pick_manifests(files)
    if not manifests:
        return {"findings": [], "checked": 0, "unresolved": [], "manifests": [], "error": ""}

    queries: List[dict] = []
    origins: List[dict] = []
    unresolved: List[dict] = []

    for path, ecosystem, parser, content in manifests:
        try:
            resolved, ranges = parser(content)
        except Exception:
            logger.exception("Could not parse %s", path)
            continue
        for name, version, line in resolved:
            queries.append({"package": {"name": name, "ecosystem": ecosystem},
                            "version": version})
            origins.append({"path": path, "name": name, "version": version,
                            "line": line, "ecosystem": ecosystem})
        for name, spec, line in ranges:
            unresolved.append({"path": path, "name": name, "spec": spec,
                               "line": line, "ecosystem": ecosystem,
                               "reason": "version is a range, not a pin — no lockfile to resolve it"})

    if not queries:
        return {"findings": [], "checked": 0, "unresolved": unresolved,
                "manifests": [m[0] for m in manifests], "error": ""}

    try:
        results = (batch or _osv_batch)(queries)
    except Exception as exc:
        logger.warning("Dependency scan skipped — OSV unreachable: %s", exc)
        return {
            "findings": [], "checked": 0, "unresolved": unresolved,
            "manifests": [m[0] for m in manifests],
            "error": f"Could not reach the vulnerability database ({type(exc).__name__}), "
                     f"so {len(queries)} dependency version(s) were not checked. This is not "
                     f"a clean result.",
        }

    # One finding per vulnerable package, carrying every id against it.
    hits: List[Tuple[dict, List[str]]] = []
    all_ids: List[str] = []
    for origin, result in zip(origins, results):
        ids = [v.get("id") for v in (result or {}).get("vulns") or [] if v.get("id")]
        if ids:
            hits.append((origin, ids))
            all_ids.extend(ids)

    records = (detail or _osv_detail)(sorted(set(all_ids))) if all_ids else {}

    findings = []
    for origin, ids in hits:
        expanded = [records[i] for i in ids if i in records]
        severity = max((_severity_of(r) for r in expanded), default=3)
        worst = next((r for r in expanded if _severity_of(r) == severity), None)
        named = [f"{_cve_of(r)} ({(r.get('summary') or '').strip()[:70]})" for r in expanded[:4]]
        where = f"{origin['path']}" + (f":{origin['line']}" if origin.get("line") else "")

        findings.append({
            "control_id": "SEC-007",
            "severity": severity,
            "risk_level": _RISK_LEVEL[severity],
            "title": f"{origin['name']} {origin['version']} has "
                     f"{len(ids)} known vulnerabilit{'y' if len(ids) == 1 else 'ies'}",
            "description": (
                f"{origin['ecosystem']} package `{origin['name']}` is pinned to "
                f"{origin['version']} in {where}, which the OSV vulnerability database lists "
                f"as affected by {len(ids)} advisor{'y' if len(ids) == 1 else 'ies'}"
                + (f": {'; '.join(named)}" if named else "")
                + (f" and {len(ids) - len(named)} more" if len(ids) > len(named) else "")
                + "."
            ),
            "recommendation": (
                f"Upgrade `{origin['name']}` past the affected range. Check the advisories "
                f"for the fixed version rather than assuming the latest release is required."
            ),
            "is_blocking": severity >= 5,
            "file_path": origin["path"],
            "line_start": origin.get("line"),
            # The manifest line is the evidence, so this verifies like any other finding
            # against the file it names. Set only when the parser knew the line — a lockfile
            # entry has no single line worth quoting.
            "evidence": _manifest_line(files, origin) if origin.get("line") else "",
            "confidence": "High",
            "detector": "dependency-database",
            "detector_rule": f"osv:{origin['ecosystem'].lower()}",
            "cve_ids": [_cve_of(records[i]) for i in ids if i in records] or ids[:4],
            "advisory_ids": ids,
            "cvss_vector": _cvss_vector(worst) if worst else "",
        })

    logger.info(
        "Dependency scan: %d version(s) checked across %d manifest(s), %d vulnerable, "
        "%d unresolved",
        len(queries), len(manifests), len(findings), len(unresolved),
    )
    return {
        "findings": findings,
        "checked": len(queries),
        "unresolved": unresolved,
        "manifests": [m[0] for m in manifests],
        "error": "",
    }


def _manifest_line(files: List[Dict[str, Any]], origin: dict) -> str:
    """The manifest line this dependency was declared on, verbatim."""
    for f in files or []:
        if (f.get("path") or "") == origin["path"]:
            lines = (f.get("content") or "").splitlines()
            index = (origin.get("line") or 0) - 1
            if 0 <= index < len(lines):
                return lines[index].strip()[:300]
    return ""
