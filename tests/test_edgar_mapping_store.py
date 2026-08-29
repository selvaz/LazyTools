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
    store.put("0000858877-24-000017", MAPPING, model="test-model")
    cached = store.get("0000858877-24-000017")
    assert cached is not None
    assert cached.mapping == MAPPING
    assert cached.model == "test-model" and cached.created_at


def test_an_unknown_filing_is_simply_absent() -> None:
    assert MappingStore().get("nope") is None


def test_one_mapping_serves_the_filing_whatever_period_is_asked_for() -> None:
    # This test used to assert the opposite: that a different column was a
    # different mapping. It was wrong, and the cost showed up in the series --
    # a mapping names LABELS, and a label is the same line in every column, so
    # keying by column made one document map twice whenever two callers wanted
    # different years out of it. One filing, one model call.
    store = MappingStore()
    store.put("x", MAPPING, model="m")
    cached = store.get("x")
    assert cached is not None and cached.mapping == MAPPING
    assert len(store) == 1


def test_a_mapping_from_another_registry_version_is_not_reused() -> None:
    # It answered "which of THOSE elements is which line". Reusing it would omit
    # every element added since, and nothing would say so.
    import lazytools.connectors.edgar.mapping_store as module

    store = MappingStore()
    store.put("x", MAPPING, model="m")
    original = module.SCHEMA_VERSION
    try:
        module.SCHEMA_VERSION = original + 1
        assert store.get("x") is None
    finally:
        module.SCHEMA_VERSION = original
    assert store.get("x") is not None


def test_remapping_the_same_filing_replaces_rather_than_duplicates() -> None:
    store = MappingStore()
    store.put("x", MAPPING, model="first")
    store.put("x", MAPPING, model="second")
    assert len(store) == 1
    assert store.get("x").model == "second"


def test_the_cache_can_be_cleared_when_a_model_mapped_badly() -> None:
    # Rows from a bad model look exactly like good ones; the model that made
    # each row travels with it, so which to distrust is answerable.
    store = MappingStore()
    store.put("a", MAPPING, model="good")
    store.put("b", MAPPING, model="bad")
    assert store.forget() == 2
    assert store.get("a") is None


def test_the_cache_holds_no_figures_at_all() -> None:
    # A stale cache may cost a re-read; it may never supply a wrong number.
    store = MappingStore()
    store.put("x", MAPPING, model="m")
    ref = store.get("x").mapping.refs[0]
    assert not hasattr(ref, "value")
    assert set(vars(ref)) == {"element_id", "statement", "label"}


def test_a_file_backed_store_survives_being_reopened(tmp_path) -> None:
    path = tmp_path / "nested" / "mappings.sqlite"
    MappingStore(path).put("x", MAPPING, model="m")
    assert MappingStore(path).get("x").mapping == MAPPING
