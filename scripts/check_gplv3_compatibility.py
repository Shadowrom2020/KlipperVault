#!/usr/bin/env python3
"""Check that runtime Python dependencies are compatible with GPLv3.

The checker reads `requirements.txt`, resolves installed transitive dependencies,
and validates each package's detected license against a GPLv3-compatible allowlist.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement


# Metadata quality varies across packages. These overrides are used only when a
# package does not expose a usable license in `License` or classifier fields.
LICENSE_OVERRIDES: dict[str, str] = {
    "nicegui": "mit",
}


ALLOWED_LICENSE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmit\b", re.IGNORECASE),
    re.compile(r"\bbsd\b", re.IGNORECASE),
    re.compile(r"apache(?:\s+license)?\s*(?:-|)?\s*2(?:\.0)?", re.IGNORECASE),
    re.compile(r"apache software license", re.IGNORECASE),
    re.compile(r"\bmpl\b\s*(?:-|)?\s*2(?:\.0)?", re.IGNORECASE),
    re.compile(r"mozilla public license\s*2", re.IGNORECASE),
    re.compile(r"python software foundation", re.IGNORECASE),
    re.compile(r"\bpsf\b", re.IGNORECASE),
    re.compile(r"public domain", re.IGNORECASE),
)


REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")


def parse_requirement_names(requirements_file: Path) -> list[str]:
    return _parse_requirement_names(requirements_file, visited=set())


def _parse_requirement_names(requirements_file: Path, visited: set[Path]) -> list[str]:
    resolved_requirements_file = requirements_file.resolve()
    if resolved_requirements_file in visited:
        return []
    visited.add(resolved_requirements_file)

    names: list[str] = []
    for line in requirements_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        include_target = _parse_requirements_include(stripped)
        if include_target is not None:
            include_path = (requirements_file.parent / include_target).resolve()
            if include_path.exists() and include_path.is_file():
                names.extend(_parse_requirement_names(include_path, visited=visited))
            continue
        match = REQ_NAME_RE.match(stripped)
        if not match:
            continue
        names.append(match.group(1))
    return names


def _parse_requirements_include(line: str) -> str | None:
    parts = line.split(maxsplit=1)
    if len(parts) != 2:
        return None
    if parts[0] not in {"-r", "--requirement"}:
        return None
    return parts[1].strip()


def normalize(name: str) -> str:
    return name.lower().replace("_", "-")


def parse_dependency_requirement(requirement_line: str) -> str | None:
    try:
        requirement = Requirement(requirement_line)
    except Exception:
        return None

    env = default_environment()
    env["extra"] = ""
    if requirement.marker and not requirement.marker.evaluate(env):
        return None
    return requirement.name


def resolve_dependency_closure(initial_requirements: Iterable[str]) -> list[metadata.Distribution]:
    seen: set[str] = set()
    queue = list(initial_requirements)
    resolved: list[metadata.Distribution] = []

    while queue:
        name = queue.pop(0)
        key = normalize(name)
        if key in seen:
            continue
        seen.add(key)

        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"Package '{name}' is listed/resolved but not installed in the current environment."
            ) from exc

        resolved.append(dist)

        for requirement_line in dist.requires or []:
            child = parse_dependency_requirement(requirement_line)
            if child and normalize(child) not in seen:
                queue.append(child)

    resolved.sort(key=lambda d: normalize(d.metadata.get("Name", "")))
    return resolved


def collect_license_candidates(dist: metadata.Distribution) -> list[str]:
    md = dist.metadata
    name = md.get("Name", "")
    candidates: list[str] = []

    license_field = (md.get("License", "") or "").strip()
    if license_field:
        candidates.append(license_field)

    for classifier in md.get_all("Classifier", []) or []:
        if classifier.startswith("License ::"):
            candidates.append(classifier)

    override = LICENSE_OVERRIDES.get(normalize(name))
    if override:
        candidates.append(override)

    # Fall back to scanning embedded license files in dist-info metadata.
    files = list(dist.files or [])
    for rel in files:
        rel_text = str(rel).lower()
        if "license" not in rel_text and not rel_text.endswith("copying"):
            continue
        try:
            path = dist.locate_file(rel)
            if not path.is_file():
                continue
            blob = path.read_text(encoding="utf-8", errors="ignore")[:6000].lower()
        except OSError:
            continue

        if "mit license" in blob:
            candidates.append("MIT")
            break
        if "apache license" in blob and "version 2.0" in blob:
            candidates.append("Apache-2.0")
            break
        if "python software foundation license" in blob:
            candidates.append("PSF-2.0")
            break
        if "mozilla public license" in blob and "2.0" in blob:
            candidates.append("MPL-2.0")
            break
        if "redistribution and use in source and binary forms" in blob:
            candidates.append("BSD")
            break

    return candidates


def is_compatible(candidates: list[str]) -> bool:
    if not candidates:
        return False
    blob = " | ".join(candidates)
    return any(pattern.search(blob) for pattern in ALLOWED_LICENSE_PATTERNS)


def main() -> int:
    requirements_file = Path("requirements.txt")
    if not requirements_file.exists():
        print("ERROR: requirements.txt not found")
        return 2

    initial = parse_requirement_names(requirements_file)
    if not initial:
        print("No requirements found.")
        return 0

    try:
        distributions = resolve_dependency_closure(initial)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2

    failures: list[tuple[str, str, list[str]]] = []
    print("GPLv3 compatibility report")
    print("=" * 40)
    for dist in distributions:
        name = dist.metadata.get("Name", "<unknown>")
        version = dist.metadata.get("Version", "?")
        candidates = collect_license_candidates(dist)
        compatible = is_compatible(candidates)
        status = "OK" if compatible else "FAIL"
        display_license = "; ".join(candidates) if candidates else "UNKNOWN"
        print(f"[{status}] {name}=={version} :: {display_license}")
        if not compatible:
            failures.append((name, version, candidates))

    if failures:
        print("\nIncompatible or unknown licenses detected:")
        for name, version, candidates in failures:
            display = "; ".join(candidates) if candidates else "UNKNOWN"
            print(f"- {name}=={version}: {display}")
        return 1

    print("\nAll resolved dependencies are GPLv3-compatible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
