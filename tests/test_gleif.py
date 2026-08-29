"""Contract tests for the GLEIF connector, driven by a stub.

Nothing here reaches the network. The stub records every GET a tool issues
so the request shape is what gets asserted, not just the reply shape. A live
spot-check (querying api.gleif.org for real) is what confirms the vendor
still answers this way; that is a release check, not something a unit test
can assert.
"""

from __future__ import annotations

import pytest

from lazytools.connectors.gleif import (
    GLEIFBudgetExceeded,
    GLEIFClient,
    GLEIFError,
    GLEIFTools,
)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Stub:
    """Stands in for an ``httpx.Client``, recording every GET.

    ``routes`` maps a path suffix (e.g. ``"/lei-records"``) to the payload
    returned for it; a path not in the map answers 404, the same as the real
    endpoint does for an unknown resource.
    """

    def __init__(self, *, routes: dict[str, object] | None = None, status: int = 200) -> None:
        self.routes = routes or {}
        self.status = status
        self.gets: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.gets.append((url, params or {}))
        for path, payload in self.routes.items():
            if url.endswith(path):
                return _Response(payload, self.status)
        return _Response(None, 404)

    def close(self):  # pragma: no cover - the client only closes what it made
        raise AssertionError("an injected transport must not be closed by the client")


GLEIF_TOOL_NAMES = {
    "gleif_search",
    "gleif_get_record",
    "gleif_parents",
    "gleif_children",
    "gleif_fuzzy_search",
}


def _lei_row(lei="5493001KJTIIGC8Y1R12", legal_name="Apple Inc."):
    return {
        "id": lei,
        "attributes": {
            "lei": lei,
            "entity": {
                "legalName": {"name": legal_name},
                "status": "ACTIVE",
                "legalForm": {"id": "8888", "other": None},
                "legalAddress": {"country": "US"},
                "headquartersAddress": {"country": "US"},
            },
            "registration": {
                "status": "ISSUED",
                "nextRenewalDate": "2027-01-01T00:00:00Z",
            },
            "bic": ["APPLUS33"],
        },
    }


def _tools(stub, **kw):
    return GLEIFTools(client=GLEIFClient(transport=stub, min_interval=0), **kw)


# --------------------------------------------------------------------------- #
# The mounted surface
# --------------------------------------------------------------------------- #
def test_tool_surface_is_exactly_expected() -> None:
    provider = _tools(_Stub())
    assert {t.name for t in provider.as_tools()} == GLEIF_TOOL_NAMES


# --------------------------------------------------------------------------- #
# gleif_search
# --------------------------------------------------------------------------- #
def test_search_returns_full_lei_record_fields() -> None:
    stub = _Stub(routes={"/lei-records": {"data": [_lei_row()]}})
    tools = _tools(stub)
    out = tools.gleif_search("Apple Inc.")

    assert out["matched"] == 1
    record = out["records"][0]
    assert record["lei"] == "5493001KJTIIGC8Y1R12"
    assert record["legal_name"] == "Apple Inc."
    assert record["status"] == "ACTIVE"
    assert record["registration_status"] == "ISSUED"
    assert record["legal_form"] == "8888"
    assert record["jurisdiction"] == "US"
    assert record["headquarters_country"] == "US"
    assert record["bic_codes"] == ["APPLUS33"]
    assert record["next_renewal_date"] == "2027-01-01T00:00:00Z"


def test_search_sends_legal_name_filter_by_default() -> None:
    stub = _Stub(routes={"/lei-records": {"data": []}})
    tools = _tools(stub)
    tools.gleif_search("Apple Inc.")
    _, params = stub.gets[0]
    assert params["filter[entity.legalName]"] == "Apple Inc."
    assert "filter[lei]" not in params


def test_search_exact_lei_switches_filter_field() -> None:
    stub = _Stub(routes={"/lei-records": {"data": []}})
    tools = _tools(stub)
    tools.gleif_search("5493001KJTIIGC8Y1R12", exact_lei=True)
    _, params = stub.gets[0]
    assert params["filter[lei]"] == "5493001KJTIIGC8Y1R12"
    assert "filter[entity.legalName]" not in params


def test_search_empty_country_is_not_sent_as_a_filter() -> None:
    # The default / "no filter" sentinel — matching the econ_calendar.py-style
    # convention already used elsewhere in this codebase (empty string means
    # "no filter", never a literal filter value sent to the vendor).
    stub = _Stub(routes={"/lei-records": {"data": []}})
    _tools(stub).gleif_search("Apple Inc.", country="")
    _, params = stub.gets[0]
    assert "filter[entity.legalAddress.country]" not in params

    # A real country code does get sent through.
    stub2 = _Stub(routes={"/lei-records": {"data": []}})
    _tools(stub2).gleif_search("Apple Inc.", country="US")
    _, params2 = stub2.gets[0]
    assert params2["filter[entity.legalAddress.country]"] == "US"


def test_search_requires_a_name() -> None:
    stub = _Stub()
    tools = _tools(stub)
    with pytest.raises(ValueError):
        tools.gleif_search("")
    assert stub.gets == []


# --------------------------------------------------------------------------- #
# gleif_get_record
# --------------------------------------------------------------------------- #
def test_get_record_found() -> None:
    stub = _Stub(routes={"/lei-records/5493001KJTIIGC8Y1R12": {"data": _lei_row()}})
    tools = _tools(stub)
    out = tools.gleif_get_record("5493001KJTIIGC8Y1R12")
    assert out["found"] is True
    assert out["record"]["lei"] == "5493001KJTIIGC8Y1R12"


def test_get_record_not_found_without_raising() -> None:
    # client.py's get_record() -> _single_record() calls _get(..., allow_404=True),
    # which returns None on an HTTP 404 -- the stub reproduces that by simply
    # having no matching route, so the default 404 response fires.
    stub = _Stub(routes={})
    tools = _tools(stub)
    out = tools.gleif_get_record("00000000000000000000")
    assert out["found"] is False
    assert "record" not in out


def test_get_record_requires_a_lei() -> None:
    stub = _Stub()
    tools = _tools(stub)
    with pytest.raises(ValueError):
        tools.gleif_get_record("")
    assert stub.gets == []


# --------------------------------------------------------------------------- #
# gleif_parents -- the 404-means-no-parent behavior is the load-bearing case
# --------------------------------------------------------------------------- #
def test_direct_parent_found() -> None:
    parent = _lei_row(lei="PARENT00000000000001", legal_name="Apple Parent Co.")
    stub = _Stub(routes={"/lei-records/5493001KJTIIGC8Y1R12/direct-parent": {"data": parent}})
    tools = _tools(stub)
    out = tools.gleif_parents("5493001KJTIIGC8Y1R12")
    assert out["has_parent"] is True
    assert out["parent"]["lei"] == "PARENT00000000000001"
    assert out["ultimate"] is False


def test_direct_parent_missing_reports_has_parent_false_without_raising() -> None:
    # Most entities (e.g. Apple Inc.) report NO direct parent at all. GLEIF's
    # vendor API answers that with HTTP 404 carrying its OWN JSON:API error
    # body (verified live) -- that specific shape is what client.py now
    # requires (strict_404) before trusting a 404 as "confirmed no parent",
    # so the stub must reproduce it exactly, not just any 404.
    stub = _Stub(status=404, routes={
        "/lei-records/5493001KJTIIGC8Y1R12/direct-parent":
            {"errors": [{"status": "404", "title": "Resource not found"}]},
    })
    tools = _tools(stub)
    out = tools.gleif_parents("5493001KJTIIGC8Y1R12")
    assert out["has_parent"] is False
    assert out["parent"] is None


def test_ultimate_parent_missing_reports_has_parent_false_without_raising() -> None:
    stub = _Stub(status=404, routes={
        "/lei-records/5493001KJTIIGC8Y1R12/ultimate-parent":
            {"errors": [{"status": "404", "title": "Resource not found"}]},
    })
    tools = _tools(stub)
    out = tools.gleif_parents("5493001KJTIIGC8Y1R12", ultimate=True)
    assert out["has_parent"] is False
    assert out["parent"] is None
    assert out["ultimate"] is True
    # confirm the ultimate-parent path, not the direct-parent one, was hit
    url, _ = stub.gets[0]
    assert url.endswith("/lei-records/5493001KJTIIGC8Y1R12/ultimate-parent")


def test_parent_lookup_on_a_nonexistent_lei_raises_instead_of_lying() -> None:
    # The regression this guards: verified live that a genuinely nonexistent
    # LEI ALSO 404s on /direct-parent, but with an HTML gateway page, not
    # GLEIF's JSON error body -- indistinguishable from "no parent" unless
    # checked. A bogus LEI silently reporting has_parent=False would be a
    # confidently wrong answer, so this must raise instead.
    stub = _Stub()  # default route -> _Response(None, 404), body is not a JSON error
    tools = _tools(stub)
    with pytest.raises(GLEIFError):
        tools.gleif_parents("00000000000000000000")


def test_parents_requires_a_lei() -> None:
    stub = _Stub()
    tools = _tools(stub)
    with pytest.raises(ValueError):
        tools.gleif_parents("")
    assert stub.gets == []


# --------------------------------------------------------------------------- #
# gleif_children
# --------------------------------------------------------------------------- #
def test_direct_children() -> None:
    child = _lei_row(lei="CHILD0000000000000001", legal_name="Apple Retail")
    stub = _Stub(
        routes={"/lei-records/5493001KJTIIGC8Y1R12/direct-children": {"data": [child]}}
    )
    tools = _tools(stub)
    out = tools.gleif_children("5493001KJTIIGC8Y1R12")
    assert out["ultimate"] is False
    assert out["count"] == 1
    assert out["children"][0]["lei"] == "CHILD0000000000000001"


def test_ultimate_children() -> None:
    child = _lei_row(lei="CHILD0000000000000002", legal_name="Apple Descendant")
    stub = _Stub(
        routes={"/lei-records/5493001KJTIIGC8Y1R12/ultimate-children": {"data": [child]}}
    )
    tools = _tools(stub)
    out = tools.gleif_children("5493001KJTIIGC8Y1R12", ultimate=True)
    assert out["ultimate"] is True
    assert out["count"] == 1
    url, _ = stub.gets[0]
    assert url.endswith("/lei-records/5493001KJTIIGC8Y1R12/ultimate-children")


def test_children_empty_list_case() -> None:
    # An entity with no reported children -- unlike parents, client.py notes
    # children normally arrive as an empty list, which _children() also
    # accepts defensively via allow_404.
    stub = _Stub(
        routes={"/lei-records/5493001KJTIIGC8Y1R12/direct-children": {"data": []}}
    )
    tools = _tools(stub)
    out = tools.gleif_children("5493001KJTIIGC8Y1R12")
    assert out["count"] == 0
    assert out["children"] == []


def test_children_lookup_on_a_nonexistent_lei_raises_instead_of_lying() -> None:
    # Same regression as the parent lookups (Codex PR review finding): a
    # real entity with no reported children normally answers 200 with an
    # empty list, not 404, so a bare 404 here most likely means the LEI
    # itself was never found -- must raise, not silently report count=0.
    stub = _Stub()  # default route -> 404, body is not GLEIF's JSON error shape
    tools = _tools(stub)
    with pytest.raises(GLEIFError):
        tools.gleif_children("00000000000000000000")


def test_children_requires_a_lei() -> None:
    stub = _Stub()
    tools = _tools(stub)
    with pytest.raises(ValueError):
        tools.gleif_children("")
    assert stub.gets == []


# --------------------------------------------------------------------------- #
# gleif_fuzzy_search
# --------------------------------------------------------------------------- #
def test_fuzzy_search_happy_path() -> None:
    stub = _Stub(
        routes={
            "/fuzzycompletions": {
                "data": [
                    {
                        "attributes": {"value": "Apple Inc."},
                        "relationships": {
                            "lei-records": {"data": {"id": "5493001KJTIIGC8Y1R12"}}
                        },
                    }
                ]
            }
        }
    )
    tools = _tools(stub)
    out = tools.gleif_fuzzy_search("appl")
    assert out["suggestions"] == [
        {"suggestion": "Apple Inc.", "lei": "5493001KJTIIGC8Y1R12"}
    ]
    _, params = stub.gets[0]
    assert params["q"] == "appl"


def test_fuzzy_search_keeps_completions_with_no_resolved_lei() -> None:
    # Verified live: a generic/ambiguous name (e.g. "John GmbH" from a real
    # "John Sm" query) has no `relationships` key at all -- no single entity
    # to point at. The suggestion text is still useful and must not be
    # silently dropped (Codex PR review finding).
    stub = _Stub(routes={"/fuzzycompletions": {"data": [{"attributes": {"value": "John GmbH"}}]}})
    tools = _tools(stub)
    out = tools.gleif_fuzzy_search("John Sm")
    assert out["suggestions"] == [{"suggestion": "John GmbH", "lei": None}]


def test_fuzzy_search_forwards_the_limit_upstream() -> None:
    # Without page[size], the vendor's own default page cannot be widened,
    # so a limit above that default was silently unreachable (Codex PR
    # review finding).
    stub = _Stub(routes={"/fuzzycompletions": {"data": []}})
    tools = _tools(stub)
    tools.gleif_fuzzy_search("appl", limit=50)
    _, params = stub.gets[0]
    assert params["page[size]"] == 50


def test_fuzzy_search_requires_a_query() -> None:
    stub = _Stub()
    tools = _tools(stub)
    with pytest.raises(ValueError):
        tools.gleif_fuzzy_search("")
    assert stub.gets == []


# --------------------------------------------------------------------------- #
# Errors and budget
# --------------------------------------------------------------------------- #
def test_search_raises_when_endpoint_answers_something_broken() -> None:
    # search() has no allow_404 path, unlike get_record/parents/children --
    # any non-200 (404 included) is a real GLEIFError here, not "not found".
    stub = _Stub(status=404)
    tools = _tools(stub)
    with pytest.raises(GLEIFError):
        tools.gleif_search("Apple Inc.")


def test_call_budget_stops_a_runaway_loop() -> None:
    stub = _Stub(routes={"/lei-records/5493001KJTIIGC8Y1R12": {"data": _lei_row()}})
    client = GLEIFClient(transport=stub, min_interval=0, max_calls=2)
    client.get_record("5493001KJTIIGC8Y1R12")
    client.get_record("5493001KJTIIGC8Y1R12")
    with pytest.raises(GLEIFBudgetExceeded):
        client.get_record("5493001KJTIIGC8Y1R12")
