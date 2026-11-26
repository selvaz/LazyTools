"""Install a tagged LazyTools release into idle InvestmentCommittee job environments."""

import argparse
import json
import ntpath
import re
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(r"C:\ProgramData\InvestmentCommittee")
DEFAULT_ENVS = ("marketdata", "etf", "anomaly", "lazycrawler", "portfolio-ray")


def normalize_path(path):
    return ntpath.normcase(ntpath.normpath(path.replace("/", "\\")))


def in_use(python, executable_paths):
    """True when the env's python runs, or when a Python process has an unreadable path.

    An unreadable path cannot prove the environment is idle, so it counts as in use.
    """
    # Any interpreter of the env counts (python.exe, pythonw.exe), not only the one we install with.
    scripts = ntpath.dirname(normalize_path(str(python)))
    return any(not path or ntpath.dirname(normalize_path(path)) == scripts for path in executable_paths)


def running_executables():
    """Executable paths of running Python processes; None where the path is unreadable."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "@(Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" -ErrorAction Stop | "
         "ForEach-Object { $_.ExecutablePath }) | ConvertTo-Json -Compress"],
        check=True, capture_output=True, text=True,
    )
    paths = json.loads(result.stdout or "[]")
    return [paths] if isinstance(paths, str) or paths is None else paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--envs", default=",".join(DEFAULT_ENVS))
    args = parser.parse_args(argv)
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.ref):
        parser.error("--ref must be a release tag such as v1.2.3")
    envs = args.envs.split(",")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", env) for env in envs):
        parser.error("--envs must be a comma-separated list of environment names")
    skipped = failed = False
    for env in envs:
        python = ROOT / "envs" / env / "Scripts" / "python.exe"
        try:
            if in_use(python, running_executables()):
                print(f"{env}: skipped (its Python is running, or a Python process path is unreadable)")
                skipped = True
                continue
            backup = ROOT / "backups"
            backup.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            freeze = backup / f"{env}-{stamp}.txt"
            with freeze.open("x", encoding="utf-8") as output:
                subprocess.run([str(python), "-m", "pip", "freeze"], check=True, stdout=output)
            for command in (
                ["-m", "pip", "install", f"lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@{args.ref}"],
                ["-m", "pip", "check"],
                ["-c", "import lazytools"],
            ):
                subprocess.run([str(python), *command], check=True, capture_output=True, text=True)
            print(f"{env}: installed {args.ref} (freeze: {freeze})")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            print(f"{env}: failed ({exc})")
            failed = True
    return 2 if skipped else 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

