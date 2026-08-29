"""Caching a mapping, because a filed document never changes.

The keys are the point. A mapping answers "which of THESE elements is which
line", so a mapping made against a different element registry answers a
different question and must not be reused -- it would silently omit whatever the
registry gained since.
"""

from __future__ import annotations

from lazytools.connectors.edgar.mapping import Absence, LineRef, Mapping
from lazytools.connectors.edgar.mapping_store import MappingStore

MAPPING = Mapping(
    refs=(LineRef("revenue", "Operations", "Total revenue"),),
    absences=(Absence("depreciation", "not broken out"),),
)


def test_a_stored_mapping_comes_back_intact() -> None:
    store = MappingStore()
    store.put("0000858877-24-000017", 0, MAPPING, model="test-model")
    cached = store.get("0000858877-24-000017", 0)
    assert cached is not None
    assert cached.mapping == MAPPING
    assert cached.model == "test-model" and cached.created_at


def test_an_unknown_filing_is_simply_absent() -> None:
    assert MappingStore().get("nope", 0) is None


def test_a_different_column_is_a_different_mapping() -> None:
    store = MappingStore()
    store.put("x", 0, MAPPING, model="m")
    assert store.get("x", 1) is None


def test_a_mapping_from_another_registry_version_is_not_reused() -> None:
    # It answered "which of THOSE elements is which line". Reusing it would omit
    # every element added since, and nothing would say so.
    import lazytools.connectors.edgar.mapping_store as module

    store = MappingStore()
    store.put("x", 0, MAPPING, model="m")
    original = module.SCHEMA_VERSION
    try:
        module.SCHEMA_VERSION = original + 1
        assert store.get("x", 0) is None
    finally:
        module.SCHEMA_VERSION = original
    assert store.get("x", 0) is not None


def test_remapping_the_same_filing_replaces_rather_than_duplicates() -> None:
    store = MappingStore()
    store.put("x", 0, MAPPING, model="first")
    store.put("x", 0, MAPPING, model="second")
    assert len(store) == 1
    assert store.get("x", 0).model == "second"


def test_the_cache_can_be_cleared_when_a_model_mapped_badly() -> None:
    # Rows from a bad model look exactly like good ones; the model that made
    # each row travels with it, so which to distrust is answerable.
    store = MappingStore()
    store.put("a", 0, MAPPING, model="good")
    store.put("b", 0, MAPPING, model="bad")
    assert store.forget() == 2
    assert store.get("a", 0) is None


def test_the_cache_holds_no_figures_at_all() -> None:
    # A stale cache may cost a re-read; it may never supply a wrong number.
    store = MappingStore()
    store.put("x", 0, MAPPING, model="m")
    ref = store.get("x", 0).mapping.refs[0]
    assert not hasattr(ref, "value")
    assert set(vars(ref)) == {"element_id", "statement", "label"}


def test_a_file_backed_store_survives_being_reopened(tmp_path) -> None:
    path = tmp_path / "nested" / "mappings.sqlite"
    MappingStore(path).put("x", 0, MAPPING, model="m")
    assert MappingStore(path).get("x", 0).mapping == MAPPING
