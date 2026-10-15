"""Check that a repository pins ecosystem siblings exactly as the compatibility manifest says.

Run from any ecosystem repo's CI (the manifest is fetched from LazyTools main by default):

    python check_pins.py --repo .                       # manifest from GitHub
    python check_pins.py --repo . --manifest path.toml  # manifest from a file

Scanned: ``pyproject.toml``, ``requirements*.txt`` and ``.github/workflows/*.yml`` (the files
that decide what gets installed, in production and in CI). Rules:

* every ``git+https://github.com/selvaz/<Repo>.git@<ref>`` reference must name a repo in the
  manifest and use exactly its ref;
* an exact ``lazybridge==X`` pin must equal the verified LazyBridge version, and any other
  ``lazybridge`` range must admit it.

A line that deliberately installs a moving ref opts out with a trailing ``# pin-check: ignore``;
a whole file (e.g. a source-head job that tests every ``main``) with ``pin-check: ignore-file``.

Exit 0 when aligned, 1 with one line per mismatch otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

MANIFEST_URL = "https://raw.githubusercontent.com/selvaz/LazyTools/main/ecosystem/compatibility.toml"
GIT_REF_RE = re.compile(r"git\+https://github\.com/selvaz/([A-Za-z0-9_.-]+?)(?:\.git)?@([A-Za-z0-9._-]+)")
# A selvaz Git URL with no ``@ref`` installs whatever the default branch is that day.
UNPINNED_RE = re.compile(r"git\+https://github\.com/selvaz/([A-Za-z0-9_.-]+?)(?:\.git)?(?![A-Za-z0-9_.@-])")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "site-packages", ".tox", "build", "dist"}
REQ_STRING_RE = re.compile(r"""["']([^"'\n]*lazy[-_.]?bridge[^"'\n]*)["']""", re.IGNORECASE)


def load_manifest(source: str | None) -> dict:
    if source:
        return tomllib.loads(Path(source).read_text(encoding="utf-8"))
    with urllib.request.urlopen(MANIFEST_URL, timeout=30) as response:
        return tomllib.loads(response.read().decode("utf-8"))


def scanned_files(repo: Path) -> list[Path]:
    files = [repo / "pyproject.toml"] if (repo / "pyproject.toml").exists() else []
    # Requirement files anywhere (deploy bundles, docker images...), outside virtualenvs/builds.
    files += sorted(
        path
        for path in repo.rglob("requirements*.txt")
        if not SKIP_DIRS.intersection(path.relative_to(repo).parts[:-1])
    )
    files += sorted(path for path in repo.glob("requirements/*.txt") if not path.name.startswith("requirements"))
    # CI installs count too: a workflow that installs a sibling "@main" tests another version.
    files += sorted(repo.glob(".github/workflows/*.yml")) + sorted(repo.glob(".github/workflows/*.yaml"))
    return files


def check(repo: Path, manifest: dict) -> list[str]:
    refs = {entry["repo"].rsplit("/", 1)[-1].lower(): entry["ref"] for entry in manifest["verified"]["github"].values()}
    bridge = Version(manifest["verified"]["lazybridge"])
    errors: list[str] = []
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
                    errors.append(f"{rel}:{lineno}: {name} is not in the ecosystem manifest")
                elif ref != want:
                    errors.append(f"{rel}:{lineno}: {name} pinned to {ref!r}, manifest says {want!r}")
            for name in UNPINNED_RE.findall(line):
                want = refs.get(name.lower())
                hint = f"use @{want}" if want else "not in the manifest (LazyBridge: lazybridge==X from PyPI)"
                errors.append(f"{rel}:{lineno}: {name} has no ref, so it installs a moving branch; {hint}")
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
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    errors = check(Path(args.repo).resolve(), manifest)
    if errors:
        print("ecosystem pins differ from the manifest:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("ecosystem pins match the manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
