"""Capability agents (phase 2) with a scripted LLM: planner -> authors -> repair loop -> gates -> READY.

The scripted LLM answers by role (planner / entity author / API author) from queues, so a test can make an
author fail first (invalid YAML, a failing gate) and check the repair loop. Live runs against a real model are in
scripts/agent_build_demo.py.
"""

import json
import time
from pathlib import Path

import pytest
import yaml
from conftest import small_payloads
from fastapi.testclient import TestClient

from factory.capability import Skill, reset_packs
from factory.capability.agents import ENTITY_REFS, PLANNER_REFS, USE_CASE_REFS, _extract
from factory.config import SKILL_DIR, load_profile
from factory.control.registry import Registry
from factory.control.server import create_control_app
from factory.data_engine import packs

PACKS = Path(__file__).parent.parent / "examples" / "capability-packs" / "mbp"
ENTITY_YAML = (PACKS / "premium_invoice.yaml").read_text(encoding="utf-8")
UC_YAML = (PACKS / "uc_mbp.yaml").read_text(encoding="utf-8")
PLAN = {"confidence": 0.9,
        "entities": [{"name": "premium_invoice", "purpose": "monthly premium bills", "rows": "many: one per month",
                      "assumptions": ["Medicaid members pay no premium"]}],
        "use_cases": [{"code": "MBP", "name": "Billing and premiums", "purpose": "premiums and balance",
                       "needs": ["premium_invoice"], "caregiver_scope": "BILLING",
                       "endpoints": [{"path": "/members/{member_id}/premiums", "entity": "premium_invoice", "kind": "list"},
                                     {"path": "/members/{member_id}/premiums/{invoice_number}", "entity": "premium_invoice", "kind": "item"}]}]}


class ScriptedLLM:
    """Answers by role, from queues; the last answer of a queue repeats."""

    model, embedding_model = "scripted", "none"

    def __init__(self, **queues: list[str]) -> None:
        self.queues = queues
        self.calls: list[tuple[str, str, str]] = []

    def generate(self, prompt: str, system: str = "") -> str:
        head = system.split(". ", 1)[0]  # the role sentence; the skill text below mentions every role
        role = "planner" if "planning agent" in head else "entity" if "entity author" in head else "use_case"
        self.calls.append((role, system, prompt))
        q = self.queues[role]
        return q.pop(0) if len(q) > 1 else q[0]


def _yaml(text: str) -> str:
    return f"Here is the pack.\n```yaml\n{text}\n```\n"


@pytest.fixture(autouse=True)
def clean_packs():
    reset_packs()
    yield
    reset_packs()


def _client(llm) -> TestClient:
    reg = Registry(load_profile("memory-offline"))
    return TestClient(create_control_app(reg, capability_layer=True, builder="agents", llm=llm)), reg


def _wait(client: TestClient, build_id: str, timeout: float = 120) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        body = client.get(f"/factory/capabilities/builds/{build_id}").json()
        if body["status"] != "BUILDING":
            return body
        time.sleep(0.2)
    raise AssertionError("build still BUILDING")


def _payloads(tenant: str) -> dict:
    p = small_payloads(members=300, tenant=tenant)
    p["s4"]["entities"] = p["s4"]["entities"] + ["premium_invoice"]
    p["s8"]["use_cases"] = p["s8"]["use_cases"] + ["MBP"]
    p["s8"]["endpoints"] = p["s8"]["endpoints"] + [
        {"method": "GET", "path": "/v1/members/{member_id}/premiums", "use_case": "MBP"},
        {"method": "GET", "path": "/v1/members/{member_id}/premiums/{invoice_number}", "use_case": "MBP"}]
    return p


def test_agents_build_mbp_from_an_empty_registry_with_repairs():
    failing_rule = yaml.safe_load(ENTITY_YAML)
    failing_rule["rules"].append({"id": "F-PREMIUM-99", "description": "wrong", "sql": "SELECT count(*) FROM premium_invoice"})
    llm = ScriptedLLM(
        planner=["I think... {not json", json.dumps(PLAN)],                       # 1 invalid answer, then the plan
        entity=["```yaml\nid: entity.premium_invoice\nversion: 1\n```",           # invalid (G1)
                _yaml(yaml.safe_dump(failing_rule, sort_keys=False)),             # valid but fails gate G2
                _yaml(ENTITY_YAML)],                                              # fixed
        use_case=[_yaml(UC_YAML)])
    client, reg = _client(llm)
    p = _payloads("agents")
    pre = client.post("/factory/capabilities/preflight", json={"payloads": p}).json()
    assert pre["status"] == "BUILDING"
    done = _wait(client, pre["build_id"])
    assert done["status"] == "READY", done
    assert done["published"] == ["entity.premium_invoice@1.0.0", "uc.MBP@1.0.0"]
    outcomes = [(s["agent"], s["attempt"], s["outcome"]) for s in done["steps"]]
    assert outcomes == [("planner", 1, "invalid"), ("planner", 2, "ok"), ("entity:premium_invoice", 1, "invalid"),
                        ("entity:premium_invoice", 2, "gates_failed"), ("entity:premium_invoice", 3, "ok"),
                        ("use_case:MBP", 1, "ok")]
    assert "premium_invoice: Medicaid members pay no premium" in done["assumptions"]
    # the repair prompt carried the gate failure back to the author
    third_entity_prompt = [c[2] for c in llm.calls if c[0] == "entity"][2]
    assert "F-PREMIUM-99" in third_entity_prompt and "G2 sandbox run" in third_entity_prompt
    # names only: the planner saw payload names and paths, never a description
    planner_prompt = llm.calls[0][2]
    assert "premium_invoice" in planner_prompt and "/v1/members/{member_id}/premiums" in planner_prompt
    log = client.get(f"/factory/capabilities/builds/{pre['build_id']}/log").json()
    assert len(log["steps"]) == 6 and log["steps"][0]["response"].startswith("I think")

    # the payloads run end to end on the agent-built packs
    for st in ["s3", "s4", "s5", "s6", "s7"]:
        r = client.post(f"/factory/data-engine/{st}", json=p[st])
        assert r.status_code == 200, (st, r.json())
    s8 = client.post("/factory/api-engine/s8", json=p["s8"]).json()
    assert s8["status"] == "DEPLOYED", s8.get("failed_contract_tests")


def test_a_stage_payload_with_gaps_starts_an_agent_build():
    entity_only = {**PLAN, "use_cases": []}  # the s4 payload alone names only the entity
    client, _ = _client(ScriptedLLM(planner=[json.dumps(entity_only)], entity=[_yaml(ENTITY_YAML)], use_case=["-"]))
    p = _payloads("fallback")
    assert client.post("/factory/data-engine/s3", json=p["s3"]).status_code == 200
    r = client.post("/factory/data-engine/s4", json=p["s4"])
    assert r.status_code == 202 and r.json()["status"] == "BUILDING"
    done = _wait(client, r.json()["build_id"])
    assert done["status"] == "READY" and done["published"] == ["entity.premium_invoice@1.0.0"]
    assert client.post("/factory/data-engine/s4", json=p["s4"]).status_code == 200


def test_unreadable_names_make_the_build_underspecified():
    plan = {"confidence": 0.2, "questions": ["What is a 'zqx_flux' record about?"]}
    client, _ = _client(ScriptedLLM(planner=[json.dumps(plan)], entity=["-"], use_case=["-"]))
    pre = client.post("/factory/capabilities/preflight", json={"payloads": {"s4": {"stage": "s4", "entities": ["zqx_flux"]}}}).json()
    done = _wait(client, pre["build_id"])
    assert done["status"] == "UNDERSPECIFIED" and done["questions"] == plan["questions"]
    assert "zqx_flux" not in packs.active()[0].id


def test_a_build_fails_after_the_attempt_budget_and_can_be_retried():
    llm = ScriptedLLM(planner=[json.dumps(PLAN)], entity=["not yaml: [", "still: {bad"], use_case=[_yaml(UC_YAML)])
    client, _ = _client(llm)
    pre = client.post("/factory/capabilities/preflight", json={"payloads": _payloads("fail")}).json()
    done = _wait(client, pre["build_id"])
    assert done["status"] == "FAILED" and "premium_invoice" in done["error"]
    assert sum(1 for s in done["steps"] if s["agent"].startswith("entity")) == 3
    assert packs.entity_pack("premium_invoice") is None
    llm.queues["entity"] = [_yaml(ENTITY_YAML)]
    assert client.post(f"/factory/capabilities/builds/{pre['build_id']}/retry").json()["status"] == "BUILDING"
    assert _wait(client, pre["build_id"])["status"] == "READY"


def test_the_skill_gives_each_agent_its_references():
    skill = Skill(SKILL_DIR)
    assert skill.version.startswith("0.3")
    for refs in (PLANNER_REFS, ENTITY_REFS, USE_CASE_REFS):
        text = skill.context(refs)
        assert text.startswith("---\nname: healthcare-capability-builder") and all(f"=== {r} ===" in text for r in refs)
    assert len(skill.context(ENTITY_REFS)) < 60_000  # fits a 32K-token context with room for the answer
    assert _extract("```yaml\nid: entity.x\nversion: 1.0.0\n", "yaml") == "id: entity.x\nversion: 1.0.0"  # unclosed fence
    assert _extract("text\n```yaml\na: 1\n```\nmore\n```json\n{\"b\": 2}\n```", "yaml") == '{"b": 2}'
