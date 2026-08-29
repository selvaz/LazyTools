"""GLEIF's Global LEI Index as a bounded LLM tool surface."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lazytools.connectors.gleif.client import GLEIFClient, GLEIFError, LEIRecord

#: Hard ceiling on records returned by one tool call, keeping large result
#: sets from consuming an agent's context in a single step.
MAX_ROWS = 100

_SOURCE = "GLEIF Global LEI Index (api.gleif.org), public read endpoint"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _record_dict(record: LEIRecord) -> dict[str, Any]:
    return {
        "lei": record.lei,
        "legal_name": record.legal_name,
        "status": record.status,
        "registration_status": record.registration_status,
        "legal_form": record.legal_form,
        "jurisdiction": record.jurisdiction,
        "headquarters_country": record.headquarters_country,
        "bic_codes": record.bic_codes,
        "next_renewal_date": record.next_renewal_date,
    }


class GLEIFTools:
    """A LazyBridge ``ToolProvider`` over GLEIF's public, read-only LEI API.

    Args:
        max_calls: budget for this provider's whole life (``None`` to remove).
        timeout: seconds per request.
        client: an injected :class:`GLEIFClient`, mostly for tests.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        max_calls: int | None = 200,
        timeout: float = 15.0,
        client: GLEIFClient | None = None,
    ) -> None:
        self._client = client or GLEIFClient(max_calls=max_calls, timeout=timeout)

    def _envelope(self, **extra: Any) -> dict:
        out = {"as_of": _now(), "source": _SOURCE, "calls_made": self._client.calls_made}
        out.update(extra)
        return out

    def gleif_search(
        self,
        name: str,
        exact_lei: bool = False,
        country: str = "",
        limit: int = 20,
    ) -> dict:
        """Search the Global LEI Index by legal name, or by exact LEI when ``exact_lei=True``; use an empty ``country`` for no country filter, and results are capped at 100.

        Args:
            name: legal name to search for, or a complete LEI code when exact_lei is true.
            exact_lei: false searches legal names; true treats name as an exact LEI code.
            country: ISO country code used to filter legal addresses, or an empty string for no filter.
            limit: maximum records to return, capped at 100.
        """
        if not name or not name.strip():
            raise ValueError("name is required")
        rows_wanted = max(1, min(int(limit), MAX_ROWS))
        records = self._client.search(
            name.strip(),
            exact_lei=exact_lei,
            country=country.strip() or None,
            limit=rows_wanted,
        )
        return self._envelope(
            query=name,
            matched=len(records),
            records=[_record_dict(record) for record in records],
        )

    def gleif_get_record(self, lei: str) -> dict:
        """Get one Global LEI Index record by exact LEI; an unknown or invalid LEI returns ``found=False`` instead of raising a not-found error.

        Args:
            lei: exact 20-character Legal Entity Identifier to look up.
        """
        if not lei or not lei.strip():
            raise ValueError("lei is required")
        record = self._client.get_record(lei.strip())
        if record is None:
            return self._envelope(lei=lei, found=False)
        return self._envelope(lei=lei, found=True, record=_record_dict(record))

    def gleif_parents(self, lei: str, ultimate: bool = False) -> dict:
        """Get an entity's reported direct or ultimate parent; most entities report no parent at all, and ``has_parent=False`` with ``parent=None`` is a normal correct result (including for top-level companies such as Apple Inc.), not an error or missing-data problem.

        Args:
            lei: exact 20-character Legal Entity Identifier whose parent relationship is requested.
            ultimate: false requests the direct parent; true requests the ultimate parent.
        """
        if not lei or not lei.strip():
            raise ValueError("lei is required")
        record = (
            self._client.ultimate_parent(lei.strip())
            if ultimate
            else self._client.direct_parent(lei.strip())
        )
        return self._envelope(
            lei=lei,
            ultimate=ultimate,
            has_parent=record is not None,
            parent=_record_dict(record) if record is not None else None,
        )

    def gleif_children(self, lei: str, ultimate: bool = False, limit: int = 50) -> dict:
        """List an entity's reported direct children, or all descendants reported under it when ``ultimate=True``; results are capped at 100.

        Args:
            lei: exact 20-character Legal Entity Identifier whose child relationships are requested.
            ultimate: false requests direct children; true requests ultimate children or descendants.
            limit: maximum children to return, capped at 100.
        """
        if not lei or not lei.strip():
            raise ValueError("lei is required")
        rows_wanted = max(1, min(int(limit), MAX_ROWS))
        records = (
            self._client.ultimate_children(lei.strip(), limit=rows_wanted)
            if ultimate
            else self._client.direct_children(lei.strip(), limit=rows_wanted)
        )
        return self._envelope(
            lei=lei,
            ultimate=ultimate,
            count=len(records),
            children=[_record_dict(record) for record in records],
        )

    def gleif_fuzzy_search(self, query: str, limit: int = 10) -> dict:
        """Find legal-name and LEI candidates from a partial or misspelled name before making a precise ``gleif_search`` call; suggestions are capped at 100.

        Args:
            query: partial or possibly misspelled legal entity name to complete.
            limit: maximum suggestions to return, capped at 100.
        """
        if not query or not query.strip():
            raise ValueError("query is required")
        rows_wanted = max(1, min(int(limit), MAX_ROWS))
        suggestions = self._client.fuzzy_search(query.strip(), limit=rows_wanted)
        return self._envelope(query=query, suggestions=suggestions)

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(self.gleif_search, name="gleif_search"),
            Tool.wrap(self.gleif_get_record, name="gleif_get_record"),
            Tool.wrap(self.gleif_parents, name="gleif_parents"),
            Tool.wrap(self.gleif_children, name="gleif_children"),
            Tool.wrap(self.gleif_fuzzy_search, name="gleif_fuzzy_search"),
        ]


__all__ = ["GLEIFTools", "GLEIFError", "MAX_ROWS"]
