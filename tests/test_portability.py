"""Local -> cloud must be one environment variable. These guards keep it that way."""

import ast
import pathlib

import pytest

from factory.adapters import build_stores
from factory.adapters.gcp import AlloyDBTableStore
from factory.config import load_profile
from factory.contracts import S2Output

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "factory"
BACKEND_MODULES = ("duckdb", "google", "psycopg", "faiss")
ALLOWED = {
    "adapters",                     # the only place backends live
    "data_engine/rng.py",           # DuckDB as a pure compute library for checksums, no storage
}


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_engine_code_never_imports_a_backend():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.split("/")[0] in ALLOWED or rel in ALLOWED:
            continue
        bad = _imports(path) & set(BACKEND_MODULES)
        if bad:
            offenders.append((rel, bad))
    assert offenders == []


def test_only_config_reads_the_environment():
    markers = ("os.environ", "getenv(", "Settings(")
    readers = [p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
               if any(m in p.read_text(encoding="utf-8") for m in markers)]
    assert set(readers) <= {"config.py", "cli.py"}


def test_profile_variable_switches_backends(monkeypatch):
    monkeypatch.setenv("FACTORY_PROFILE", "memory-offline")
    assert type(build_stores(load_profile()).tables).__name__ == "DuckDBTableStore"

    monkeypatch.setenv("FACTORY_PROFILE", "gcp")
    s2 = S2Output.load("blueprints/s2_output_gcp.example.json")
    stores = build_stores(load_profile(s2=s2))
    assert isinstance(stores.tables, AlloyDBTableStore)
    assert stores.tables.uri.endswith("instances/primary")
    assert "not implemented" in stores.tables.health()


def test_gcp_profile_needs_s2_output(monkeypatch):
    monkeypatch.setenv("FACTORY_PROFILE", "gcp")
    with pytest.raises(ValueError):
        load_profile()
