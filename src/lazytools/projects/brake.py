"""Per-project on/off switch for the engine quota brake (``admission.py``).

New with this package. Stored at its own key, one per project, for the same
CAS-safety reason ``owner.py`` is: never a field inside ``ProjectRecord``,
so a concurrent LazyCEO write through its own (unaware) model can never
drop it.

On by default for every project -- a project with no record at all reads as
enabled. Disabling it is a deliberate per-project opt-out, e.g. for a small
project the operator has decided is worth running past the brake's ordinary
limits regardless of fleet-wide quota pressure; it does not touch the
engine-level ceiling/boundary numbers (``EngineBudget``), only whether THIS
project's delegated work is subject to them at all. See
``admission.project_admit`` and docs/projects.md's "brake rule".
"""

from __future__ import annotations

from lazybridge import Store

from lazytools.projects.keys import PROJECT_BRAKE_PREFIX


def _brake_key(project_id: str, *, prefix: str = PROJECT_BRAKE_PREFIX) -> str:
    return f"{prefix}{project_id}"


def get_project_brake_enabled(store: Store, project_id: str, *, prefix: str = PROJECT_BRAKE_PREFIX) -> bool:
    """Whether the quota brake applies to ``project_id``'s delegated work. Default True."""
    raw = store.read(_brake_key(project_id, prefix=prefix))
    if isinstance(raw, dict) and isinstance(raw.get("enabled"), bool):
        return raw["enabled"]
    return True


def set_project_brake_enabled(
    store: Store, project_id: str, enabled: bool, *, prefix: str = PROJECT_BRAKE_PREFIX
) -> None:
    """Turn the brake on or off for ``project_id``. One write, no data copy."""
    store.write(_brake_key(project_id, prefix=prefix), {"project_id": project_id, "enabled": bool(enabled)})
