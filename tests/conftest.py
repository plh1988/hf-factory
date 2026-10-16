"""Test helpers: everything runs through the REST control plane with job payloads (no blueprint)."""

import copy
import json
import warnings
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory.config import load_profile
from factory.control.registry import Registry
from factory.control.server import create_control_app
from factory.data_engine.runner import stage_seed

warnings.filterwarnings("ignore", category=DeprecationWarning)

FIX = Path(__file__).parent / "fixtures" / "s1_jobs"
DATA_STAGES = ["s3", "s4", "s5", "s6", "s7"]


def spec_payload(stage: str) -> dict:
    """The spec's S1 payload for a stage, unchanged."""
    return json.loads((FIX / f"{stage}.json").read_text(encoding="utf-8"))


def new_client() -> tuple[TestClient, Registry]:
    reg = Registry(load_profile("memory-offline"))
    return TestClient(create_control_app(reg)), reg


def small_payloads(members: int = 600, tenant: str = "test-small", seed: int | None = None,
                   shards: int = 3, member_scenarios: bool = True) -> dict[str, dict]:
    """Spec payloads scaled down for fast tests. member_scenarios adds the s5 scenario_mix to s4 (PEV-xx)."""
    out = {}
    for st in [*DATA_STAGES, "s8"]:
        p = copy.deepcopy(spec_payload(st))
        if "alloydb" in p.get("consumes", {}):
            p["consumes"]["alloydb"] = f"alloy-{tenant}"
        if seed is not None and "root_seed" in p:
            p["root_seed"], p["stage_seed"] = seed, stage_seed(seed, st)
        if "shards" in p:
            p["shards"] = shards
        out[st] = p
    out["s3"]["volumes"] = {"providers": 30, "plans": 4}
    out["s4"]["volumes"] = {"members": members}
    out["s5"]["volumes"] = {"claims": members, "authorizations": members // 20}
    if member_scenarios:
        out["s4"]["scenario_mix"] = out["s5"]["scenario_mix"]
    return out


def run_all(client: TestClient, payloads: dict[str, dict], api: bool = True) -> dict:
    resp = {st: client.post(f"/factory/data-engine/{st}", json=payloads[st]) for st in DATA_STAGES}
    if api:
        resp["s8"] = client.post("/factory/api-engine/s8", json=payloads["s8"])
    return resp


def checksums(client: TestClient, tenant: str) -> dict[str, str]:
    return client.get(f"/factory/runs/{tenant}/manifest").json()["entity_checksums"]


@pytest.fixture(scope="session")
def generated():
    """One small S1 run through s3..s8, shared by read-only tests."""
    client, reg = new_client()
    payloads = small_payloads()
    resp = run_all(client, payloads)
    for st, r in resp.items():
        assert r.status_code == 200, (st, r.json())
    return client, reg.runs["test-small"], resp
