from __future__ import annotations

from lazytools.mcp_server import __main__ as entrypoint


def test_preimport_scientific_modules_for_enabled_provider(monkeypatch) -> None:
    imported: list[str] = []
    monkeypatch.setattr(entrypoint.importlib, "import_module", imported.append)

    entrypoint._preimport_scientific_modules(["datahub"])

    assert imported == ["numpy", "pandas", "market_data_hub.agent_tools"]


def test_preimport_scientific_modules_tolerates_missing_optional_dependency(monkeypatch) -> None:
    def missing(_module: str) -> None:
        raise ImportError("optional dependency is absent")

    monkeypatch.setattr(entrypoint.importlib, "import_module", missing)

    entrypoint._preimport_scientific_modules(["statistical"])


def test_preimport_scientific_modules_skips_unrelated_provider(monkeypatch) -> None:
    imported: list[str] = []
    monkeypatch.setattr(entrypoint.importlib, "import_module", imported.append)

    entrypoint._preimport_scientific_modules(["studio"])

    assert imported == []


def test_preimport_tolerates_non_import_errors(monkeypatch):
    import importlib

    from lazytools.mcp_server import __main__ as entry

    real_import = importlib.import_module

    def broken(name, *args, **kwargs):
        if name == "pandas":
            raise ValueError("binary incompatibility")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(entry.importlib, "import_module", broken)
    entry._preimport_scientific_modules(None)  # must not raise
