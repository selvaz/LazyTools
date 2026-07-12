"""Artifact resolution for report figures — stdlib only.

A :class:`~lazytools.report.models.FigureBlock` names its image with a
canonical ``"scheme:key"`` ref — the same string shape as
``market_data_hub.lazydatacore.ArtifactRef``, the ecosystem's shared artifact
identity. It is parsed here with the stdlib so the report core keeps its
zero-dependency guarantee; contract compliance is the string format itself.

:class:`ArtifactResolvers` maps a scheme to a resolver callable returning
``(bytes, mime)``. The core registers only the two schemes it can satisfy
without any dependency:

* ``file:<path>``   — read a local file (only the first ``:`` separates, so
  Windows drive letters survive). Optionally sandboxed to a base directory.
* ``bytes:<base64>`` — inline payload, MIME sniffed from magic bytes.

Source-specific schemes (``regimes:`` — LazyStats depot plots, ``crawler:`` —
LazyCrawler artifacts, ``chart:`` — on-demand datahub charts) are registered
by their connectors via :meth:`ArtifactResolvers.register`; an unregistered
scheme fails loudly at resolve time.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
from collections.abc import Callable
from pathlib import Path

#: A resolver takes the ref's key and returns ``(payload bytes, mime type)``.
Resolver = Callable[[str], tuple[bytes, str]]

#: Magic-byte prefixes for the image formats a report may embed.
_IMAGE_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # RIFF....WEBP, checked further below
    (b"<svg", "image/svg+xml"),
    (b"<?xml", "image/svg+xml"),
]


def split_ref(ref: str) -> tuple[str, str]:
    """Split a canonical ``"scheme:key"`` ref; only the first ``:`` separates."""
    scheme, sep, key = ref.partition(":")
    if not sep or not scheme or not key:
        raise ValueError(
            f"not a namespaced artifact ref: {ref!r} "
            "(expected 'scheme:key', e.g. 'regimes:plot_ab12cd')"
        )
    return scheme, key


def sniff_image_mime(data: bytes, *, fallback: str = "application/octet-stream") -> str:
    """Best-effort image MIME from magic bytes (deterministic, stdlib only)."""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return fallback


class ArtifactResolvers:
    """A per-scheme registry turning artifact refs into ``(bytes, mime)``.

    ``file_base_dir`` optionally sandboxes the ``file:`` scheme: when set,
    refs resolving outside that directory are refused — pass it when the
    refs come from an untrusted composer (an LLM agent); leave it ``None``
    for trusted in-process callers.
    """

    def __init__(self, *, file_base_dir: str | None = None) -> None:
        self._file_base = Path(file_base_dir).resolve() if file_base_dir else None
        self._resolvers: dict[str, Resolver] = {
            "file": self._resolve_file,
            "bytes": self._resolve_bytes,
        }

    def register(self, scheme: str, resolver: Resolver) -> None:
        """Register (or replace) the resolver for a scheme."""
        self._resolvers[scheme] = resolver

    def schemes(self) -> list[str]:
        """The schemes currently resolvable, sorted."""
        return sorted(self._resolvers)

    def resolve(self, ref: str) -> tuple[bytes, str]:
        """Resolve a canonical ref to ``(payload bytes, mime type)``."""
        scheme, key = split_ref(ref)
        resolver = self._resolvers.get(scheme)
        if resolver is None:
            raise ValueError(
                f"no resolver registered for artifact scheme {scheme!r} "
                f"(ref {ref!r}); available: {', '.join(self.schemes())}"
            )
        return resolver(key)

    # ------------------------------------------------------------------ #
    # Core resolvers
    # ------------------------------------------------------------------ #
    def _resolve_file(self, key: str) -> tuple[bytes, str]:
        path = Path(key)
        if self._file_base is not None:
            resolved = (self._file_base / path).resolve() if not path.is_absolute() else path.resolve()
            if not resolved.is_relative_to(self._file_base):
                raise ValueError(
                    f"file artifact {key!r} is outside the sandbox {str(self._file_base)!r}"
                )
            path = resolved
        data = path.read_bytes()
        guessed, _ = mimetypes.guess_type(path.name)
        return data, guessed or sniff_image_mime(data)

    def _resolve_bytes(self, key: str) -> tuple[bytes, str]:
        try:
            data = base64.b64decode(key, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"bytes artifact is not valid base64: {exc}") from exc
        return data, sniff_image_mime(data, fallback="image/png")
