"""The documented coding-connector timeouts must be the ones the code uses.

The defaults moved from five and fifteen minutes to one hour, and the public
configuration guide, the per-connector reference and the provider docstrings
all kept quoting the old numbers. A reader sizing their host timeout from the
docs would have cancelled calls the connector was still willing to wait for.
Compared against the constants themselves so it cannot drift again.
"""

from __future__ import annotations

import re
from pathlib import Path

from lazytools.connectors.code_support import _claude_code, _codex, _review, _writer

ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_the_config_guide_states_the_real_review_timeout() -> None:
    row = re.search(r"\| `LAZYTOOLS_CODE_REVIEW_TIMEOUT` \| `(\d+)` \|", _text("docs/mcp-server.md"))

    assert row is not None
    assert float(row.group(1)) == _review.DEFAULT_REVIEW_TIMEOUT


def test_the_connector_references_state_the_real_defaults() -> None:
    for page, constant in (
        ("docs/code-support/claude-code.md", _claude_code.DEFAULT_TIMEOUT),
        ("docs/code-support/codex.md", _codex.DEFAULT_TIMEOUT),
    ):
        text = _text(page)
        signature = re.search(r"^\s+timeout: float = ([\d.]+),", text, re.M)
        table = re.search(r"\| `timeout` \| `float` \| `([\d.]+)` \|", text)

        assert signature is not None and table is not None, page
        assert float(signature.group(1)) == float(table.group(1)) == constant, page


def test_the_reviewer_signature_states_the_real_default() -> None:
    signature = re.search(r"timeout: float = ([\d.]+),\s+# seconds per review", _text("docs/code-support/codex.md"))

    assert signature is not None
    assert float(signature.group(1)) == _review.DEFAULT_REVIEW_TIMEOUT


def test_the_provider_docstrings_state_the_real_defaults() -> None:
    providers = _text("src/lazytools/mcp_server/providers.py")

    review = re.search(r"``DEFAULT_REVIEW_TIMEOUT``, (\d+)\)", providers)
    write = re.search(r"``LAZYTOOLS_CODE_WRITE_TIMEOUT`` — seconds per call \(default: (\d+)\)", providers)

    assert review is not None and write is not None
    assert float(review.group(1)) == _review.DEFAULT_REVIEW_TIMEOUT
    assert float(write.group(1)) == _writer.DEFAULT_TIMEOUT
