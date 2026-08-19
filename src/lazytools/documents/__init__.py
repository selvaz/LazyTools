"""Document-reading tools.

Read ``.txt``, ``.md``, ``.pdf``, ``.docx``, ``.html`` files from a folder or a
single file and return their text ready for LLM consumption.

Optional dependencies (PDF/DOCX/HTML parsing)::

    pip install "lazytoolkit[docs] @ git+https://github.com/selvaz/LazyTools.git"
"""

from __future__ import annotations

from lazytools.documents.read_docs import read_docs_tools, read_folder_docs

__all__ = ["read_docs_tools", "read_folder_docs"]
