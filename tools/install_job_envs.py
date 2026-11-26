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
    target = normalize_path(str(python))
    return any(path and normalize_path(path) == target for path in executable_paths)


def running_executables():
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return [p.exe() for p in psutil.process_iter()]
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            # An incomplete list cannot establish that an environment is idle.
            pass
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Get-CimInstance Win32_Process -ErrorAction Stop | "
         "Select-Object -ExpandProperty ExecutablePath | ConvertTo-Json -Compress"],
        check=True, capture_output=True, text=True,
    )
    paths = json.loads(result.stdout or "[]")
    return [paths] if isinstance(paths, str) else paths or []


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
                print(f"{env}: skipped (Python process running)")
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

