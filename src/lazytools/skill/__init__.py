"""Bundled LazyTools assistant skill."""

from importlib.resources import files
from pathlib import Path


def skill_path() -> Path:
    """Return the directory containing the skill and its references."""
    return Path(str(files(__name__)))
