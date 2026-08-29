"""Source-agnostic vocabulary for reported financial data.

Reading a period phrase, and choosing one reported fact out of the several that
answer a naive query, are the same problems whether the filing came from SEC
EDGAR or from an ESEF repository. They live here rather than inside either
connector so that neither has to import from the other.
"""
