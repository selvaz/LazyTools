"""Shared task-run and artifact catalog for the LazyTools ecosystem.

The catalog stores operational metadata in SQLite and content-addressed
artifacts under a separate directory. Domain repositories keep ownership of
their specialist databases; they publish run IDs and artifact references here.
"""

from lazytools.operations.catalog import ArtifactRecord, OperationsCatalog, RunRecord

__all__ = ["ArtifactRecord", "OperationsCatalog", "RunRecord"]
