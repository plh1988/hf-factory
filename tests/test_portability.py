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
    s2 = S2Output.load("config/s2_output_gcp.example.json")
    stores = build_stores(load_profile(s2=s2), namespace="ma-demo-01")  # builds without credentials: clients connect lazily
    assert isinstance(stores.tables, AlloyDBTableStore)
    assert stores.tables.uri.endswith("/factory/t_ma_demo_01")
    assert stores.blobs.prefix == "ma_demo_01/"
    assert type(stores.executor).__name__ == "CloudRunJobExecutor"
    assert stores.executor.job == "projects/hf-ma-demo-01/locations/us-central1/jobs/data-loader"
    assert S2Output.model_validate_json(stores.executor.s2_json).project_id == "hf-ma-demo-01"
    assert type(stores.deployer).__name__ == "CloudRunDeployer"


def test_sql_translation_for_alloydb():
    from factory.adapters.gcp import to_postgres_sql

    sql = "SELECT 1 FROM s WHERE list_contains(blackout_dates, CAST(CAST(d AS DATE) AS VARCHAR)) AND a = ? AND p LIKE '%5'"
    out = to_postgres_sql(sql, True)
    assert "(CAST(CAST(d AS DATE) AS VARCHAR) = ANY(blackout_dates))" in out and "a = %s" in out and "'%%5'" in out


def test_openapi_converts_to_swagger2():
    from factory.adapters.deploy import openapi3_to_swagger2
    from factory.api_engine.app import create_app
    from factory.contracts import ApiJob
    from datetime import date

    job = ApiJob(job="api_generate_and_deploy", stage="s8", use_cases=["PCS", "MPC"])
    app = create_app(job, build_stores(load_profile("memory-offline")), tenant_id="t", seed=1, as_of=date(2026, 10, 12))
    sw = openapi3_to_swagger2(app.openapi(), "https://api-t.run.app", "t")
    assert sw["swagger"] == "2.0" and sw["x-google-backend"]["disable_auth"] is True
    put = sw["paths"]["/v1/members/{member_id}/pcp"]["put"]
    assert any(p["in"] == "body" for p in put["parameters"]) and any(p["in"] == "path" for p in put["parameters"])
    assert "#/components/" not in str(sw) and "#/definitions/PcpChangeRequest" in str(sw["paths"])
    assert "PcpChangeRequest" in sw["definitions"]


def test_gcp_profile_needs_s2_output(monkeypatch):
    monkeypatch.setenv("FACTORY_PROFILE", "gcp")
    with pytest.raises(ValueError):
        load_profile()
