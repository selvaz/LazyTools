"""Check that a repository pins ecosystem siblings exactly as the compatibility manifest says.

Run from any ecosystem repo's CI (the manifest is fetched from LazyTools main by default):

    python check_pins.py --repo .                       # manifest from GitHub
    python check_pins.py --repo . --manifest path.toml  # manifest from a file

Scanned: every ``pyproject.toml`` and ``requirements*.txt`` below the repository root,
``manifest/*.toml``, and ``.github/workflows/*.yml`` (the files that decide what gets installed,
in production and in CI). Virtual environments, dependency/build output, and files marked
``pin-check: ignore-file`` are skipped. Rules:

* every managed ``git+https://github.com/selvaz/<Repo>.git@<ref>`` reference must use exactly
  the manifest ref; unmanaged repositories produce a warning but do not fail the check;
* an exact ``lazybridge==X`` pin must equal the verified LazyBridge version, and any other
  ``lazybridge`` range must admit it.

During a release train the next set is staged as a *candidate* manifest on the LazyTools branch
``train/next``: a repository passes when its pins match main's manifest, or (all of them) the
candidate. The train ends by merging ``train/next`` into main through the ecosystem-released
gate, after which the candidate branch is deleted. Every release must also regenerate the
lazy-installer manifest and bump LazyStudio's backend pins to the promoted refs.

A line that deliberately installs a moving ref opts out with a trailing ``# pin-check: ignore``;
a whole file (e.g. a source-head job that tests every ``main``) with ``pin-check: ignore-file``.

Exit 0 when aligned, 1 with one line per mismatch otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

MANIFEST_URL = "https://raw.githubusercontent.com/selvaz/LazyTools/main/ecosystem/compatibility.toml"
CANDIDATE_URL = "https://raw.githubusercontent.com/selvaz/LazyTools/train/next/ecosystem/compatibility.toml"
GIT_REF_RE = re.compile(r"git\+https://github\.com/selvaz/([A-Za-z0-9_.-]+?)(?:\.git)?@([A-Za-z0-9._-]+)")
# A selvaz Git URL with no ``@ref`` installs whatever the default branch is that day.
UNPINNED_RE = re.compile(r"git\+https://github\.com/selvaz/([A-Za-z0-9_.-]+?)(?:\.git)?(?![A-Za-z0-9_.@-])")
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    ".virtualenv",
    "virtualenv",
    ".direnv",
    "node_modules",
    "site-packages",
    ".tox",
    "build",
    "dist",
}
REQ_STRING_RE = re.compile(r"""["']([^"'\n]*lazy[-_.]?bridge[^"'\n]*)["']""", re.IGNORECASE)


def load_manifest(source: str | None, url: str = MANIFEST_URL) -> dict:
    if source:
        return tomllib.loads(Path(source).read_text(encoding="utf-8"))
    with urllib.request.urlopen(url, timeout=30) as response:
        return tomllib.loads(response.read().decode("utf-8"))


def load_candidate(source: str | None) -> dict | None:
    """The staged next release set, or ``None`` when no train is running (no branch)."""
    if source:
        return load_manifest(source)
    try:
        return load_manifest(None, CANDIDATE_URL)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def scanned_files(repo: Path) -> list[Path]:
    # Python projects and deploy bundles can be nested (for example LazyStudio/backend).
    files = sorted(
        path
        for pattern in ("pyproject.toml", "requirements*.txt")
        for path in repo.rglob(pattern)
        if not SKIP_DIRS.intersection(path.relative_to(repo).parts[:-1])
    )
    # Installer manifests are another production install surface.
    files += sorted(repo.glob("manifest/*.toml"))
    # CI installs count too: a workflow that installs a sibling "@main" tests another version.
    files += sorted(repo.glob(".github/workflows/*.yml")) + sorted(repo.glob(".github/workflows/*.yaml"))
    return files


def check(repo: Path, manifest: dict, warnings: list[str] | None = None) -> list[str]:
    refs = {entry["repo"].rsplit("/", 1)[-1].lower(): entry["ref"] for entry in manifest["verified"]["github"].values()}
    bridge = Version(manifest["verified"]["lazybridge"])
    errors: list[str] = []
    warnings = warnings if warnings is not None else []
    for path in scanned_files(repo):
        rel = path.relative_to(repo).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if "pin-check: ignore-file" in text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#") or "pin-check: ignore" in line:
                continue
            for name, ref in GIT_REF_RE.findall(line):
                want = refs.get(name.lower())
                if want is None:
                    warnings.append(f"{rel}:{lineno}: {name} is unmanaged (not in the ecosystem manifest)")
                elif ref != want:
                    errors.append(f"{rel}:{lineno}: {name} pinned to {ref!r}, manifest says {want!r}")
            for name in UNPINNED_RE.findall(line):
                want = refs.get(name.lower())
                if name.lower() == "lazybridge":
                    errors.append(
                        f"{rel}:{lineno}: {name} has no ref, so it installs a moving branch; "
                        "use lazybridge==X from PyPI"
                    )
                elif want is None:
                    warnings.append(f"{rel}:{lineno}: {name} is unmanaged and has no ref")
                else:
                    errors.append(f"{rel}:{lineno}: {name} has no ref, so it installs a moving branch; use @{want}")
            # pip requirement files allow inline comments after whitespace.
            plain = re.split(r"\s#", line, maxsplit=1)[0].strip()
            for raw in REQ_STRING_RE.findall(line) or ([plain] if path.suffix == ".txt" and plain else []):
                try:
                    req = Requirement(raw)
                except InvalidRequirement:
                    continue
                if canonicalize_name(req.name) != "lazybridge" or req.url:
                    continue
                # The whole specifier set decides (``==1.8.*``, ``==1.8.1,!=1.8.1`` ...).
                if req.specifier and bridge not in req.specifier:
                    errors.append(f"{rel}:{lineno}: lazybridge {req.specifier} excludes the verified {bridge}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="repository root to scan (default: .)")
    parser.add_argument("--manifest", default=None, help="manifest TOML path (default: LazyTools main on GitHub)")
    parser.add_argument(
        "--candidate",
        default=None,
        help="candidate manifest TOML path (default: LazyTools train/next on GitHub, if that branch exists)",
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    warnings: list[str] = []
    errors = check(repo, load_manifest(args.manifest), warnings)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if errors:
        candidate = load_candidate(args.candidate)
        if candidate is not None and not check(repo, candidate):
            print("ecosystem pins match the candidate release set (LazyTools train/next)")
            return 0
        print("ecosystem pins differ from the manifest:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("ecosystem pins match the manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
