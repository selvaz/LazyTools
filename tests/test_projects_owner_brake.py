"""lazytools.projects.owner / .brake -- separate-key per-project settings."""

from __future__ import annotations

from lazybridge import Store

from lazytools.projects import brake, owner


def _store() -> Store:
    return Store()


def test_owner_defaults_to_ceo_for_unset_project() -> None:
    store = _store()
    assert owner.get_project_owner(store, "never-touched") == "ceo"


def test_set_project_owner_round_trips() -> None:
    store = _store()
    assert owner.set_project_owner(store, "alpha", "claude") is None
    assert owner.get_project_owner(store, "alpha") == "claude"
    assert owner.set_project_owner(store, "alpha", "shared") is None
    assert owner.get_project_owner(store, "alpha") == "shared"


def test_set_project_owner_rejects_invalid_value() -> None:
    store = _store()
    refusal = owner.set_project_owner(store, "alpha", "nobody")
    assert refusal is not None and "REJECTED" in refusal
    # unchanged
    assert owner.get_project_owner(store, "alpha") == "ceo"


def test_list_project_ids_by_owner_only_counts_explicit_records() -> None:
    store = _store()
    owner.set_project_owner(store, "alpha", "claude")
    owner.set_project_owner(store, "beta", "claude")
    owner.set_project_owner(store, "gamma", "ceo")
    assert owner.list_project_ids_by_owner(store, "claude") == {"alpha", "beta"}
    # a legacy project with no record at all is NOT counted as "ceo" here --
    # get_project_owner is what defaults it; this is the raw explicit-record view.
    assert owner.list_project_ids_by_owner(store, "ceo") == {"gamma"}


def test_owner_write_never_touches_a_second_key() -> None:
    """One write, no data copy: setting owner must not write the main project
    record or its board -- those keys simply never existed."""
    store = _store()
    owner.set_project_owner(store, "alpha", "shared")
    assert store.read("ceo:project:alpha") is None
    assert store.read("blackboard:project:alpha") is None


def test_brake_enabled_defaults_true_for_unset_project() -> None:
    store = _store()
    assert brake.get_project_brake_enabled(store, "never-touched") is True


def test_set_project_brake_enabled_round_trips() -> None:
    store = _store()
    brake.set_project_brake_enabled(store, "alpha", False)
    assert brake.get_project_brake_enabled(store, "alpha") is False
    brake.set_project_brake_enabled(store, "alpha", True)
    assert brake.get_project_brake_enabled(store, "alpha") is True


def test_brake_write_never_touches_the_main_record() -> None:
    store = _store()
    brake.set_project_brake_enabled(store, "alpha", False)
    assert store.read("ceo:project:alpha") is None
