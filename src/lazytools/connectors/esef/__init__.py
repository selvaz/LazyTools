"""ESEF — the EU's machine-readable annual reports, via filings.xbrl.org.

Incomplete: the client is here and works, but nothing yet turns a filing's
xBRL-JSON into :class:`~lazytools.financials.facts.Fact` objects, and no
evidence layer uses it.
"""

from lazytools.connectors.esef.client import ESEFClient, ESEFNotFound, ESEFService

__all__ = ["ESEFClient", "ESEFNotFound", "ESEFService"]
