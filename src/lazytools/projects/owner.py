"""Who a project belongs to: ``ceo``, ``claude``, or ``shared``.

New with this package -- no installed LazyCEO writes this key today. Stored
at its OWN key, one per project, deliberately never as a field inside the
``ProjectRecord`` blob: see ``keys.PROJECT_OWNER_PREFIX`` for why (LazyCEO's
own CAS mutations round-trip that blob through a pydantic model that does
not know this field, which would silently drop it on the model's next write).
A separate key cannot be dropped that way, which is also what makes
"changing owner" a single write with no data copy.

A project with no owner record at all -- every project any currently
installed LazyCEO has ever created -- reads as ``"ceo"``: legacy work stays
attributed to the CEO until someone deliberately reassigns it.
"""

from __future__ import annotations

from typing import Literal

from lazybridge import Store

from lazytools.projects.keys import PROJECT_OWNER_PREFIX

ProjectOwner = Literal["ceo", "claude", "shared"]

_VALID_OWNERS: tuple[ProjectOwner, ...] = ("ceo", "claude", "shared")

#: What a project with no owner record reads as. Not "unknown": every such
#: project was created before this package existed, i.e. by LazyCEO.
DEFAULT_OWNER: ProjectOwner = "ceo"


def _owner_key(project_id: str, *, prefix: str = PROJECT_OWNER_PREFIX) -> str:
    return f"{prefix}{project_id}"


def get_project_owner(store: Store, project_id: str, *, prefix: str = PROJECT_OWNER_PREFIX) -> ProjectOwner:
    """The owner of ``project_id``, or :data:`DEFAULT_OWNER` if never set."""
    raw = store.read(_owner_key(project_id, prefix=prefix))
    if isinstance(raw, dict) and raw.get("owner") in _VALID_OWNERS:
        return raw["owner"]
    return DEFAULT_OWNER


def set_project_owner(store: Store, project_id: str, owner: str, *, prefix: str = PROJECT_OWNER_PREFIX) -> str | None:
    """Set ``project_id``'s owner. Returns None on success, a refusal string otherwise.

    One write, no data copy: this never touches the ``ProjectRecord`` blob or
    the project's board. Safe to call whether or not a live CEO process is
    writing the SAME Store at the same time -- it never reads-then-writes the
    main record, so there is nothing for a concurrent CEO write to race.
    Accepts any existing project_id, including one the caller has never
    itself seen, on the theory that reassigning ownership should not require
    reading the whole record first.
    """
    if owner not in _VALID_OWNERS:
        return f"REJECTED: owner must be one of {_VALID_OWNERS} (got {owner!r})"
    store.write(_owner_key(project_id, prefix=prefix), {"project_id": project_id, "owner": owner})
    return None


def list_project_ids_by_owner(store: Store, owner: str, *, prefix: str = PROJECT_OWNER_PREFIX) -> set[str]:
    """Every project_id with an EXPLICIT owner record equal to ``owner``.

    Does not include legacy projects that default to ``"ceo"`` with no
    record at all -- callers filtering a project list by owner should treat
    "no record" as ``"ceo"`` themselves (``get_project_owner`` does this),
    not assume this function already has.
    """
    return {
        raw["project_id"]
        for _key, raw in store.items(prefix=prefix)
        if isinstance(raw, dict) and raw.get("owner") == owner and isinstance(raw.get("project_id"), str)
    }
