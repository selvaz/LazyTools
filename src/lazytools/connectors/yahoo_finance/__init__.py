"""Direct, in-memory Yahoo Finance fundamentals reader.

This is a normal Python connector, deliberately not a ToolProvider and not an
MCP provider.  It fetches a caller-selected ticker's reported statements only
when called and stores nothing.  See :mod:`.fundamentals` for the comparison
contract.
"""

from lazytools.connectors.yahoo_finance.fundamentals import (
    StatementSnapshot,
    VendorComparison,
    YahooFundamentals,
    YahooFundamentalsClient,
    YahooFundamentalsError,
    compare_with_base,
)

__all__ = [
    "StatementSnapshot",
    "VendorComparison",
    "YahooFundamentals",
    "YahooFundamentalsClient",
    "YahooFundamentalsError",
    "compare_with_base",
]
