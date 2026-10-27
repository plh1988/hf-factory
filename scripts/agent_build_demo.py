"""Live phase 2 acceptance: capability agents build packs from payload names alone, with a real LLM.

    python scripts/agent_build_demo.py                     # MBP, MPA, MRX with the memory-local LLM (LM Studio)
    python scripts/agent_build_demo.py --only MRX --llm-profile memory

For each use case: start from a registry without its packs, preflight stage payloads that only *name* the new
entity / use case / endpoints, wait for the agent build, then run s3..s8 on the agent-built packs and call the new
endpoints. Data runs use the memory-offline profile (fast); only the agents call the LLM.
Results, with every prompt and response, go to out/agent-demo/<UC>.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]
warnings.filterwarnings("ignore")

CASES = {
    "MBP": {"s4": ["premium_invoice"], "paths": ["/v1/members/{member_id}/premiums", "/v1/members/{member_id}/premiums/{invoice_number}"]},
    "MPA": {"s4": [], "paths": ["/v1/members/{member_id}/authorizations", "/v1/members/{member_id}/authorizations/{auth_number}"]},
    "MRX": {"s4": ["prescription"], "paths": ["/v1/members/{member_id}/prescriptions", "/v1/members/{member_id}/prescriptions/{rx_number}"]},
}


def payloads(code: str) -> dict:
    from conftest import small_payloads

    case = CASES[code]
    p = small_payloads(members=300, tenant=f"demo-{code.lower()}")
    p["s4"]["entities"] = p["s4"]["entities"] + case["s4"]
    p["s8"]["use_cases"] = p["s8"]["use_cases"] + [code]
    p["s8"]["endpoints"] = p["s8"]["endpoints"] + [{"method": "GET", "path": x, "use_case": code} for x in case["paths"]]
    return p


def run_case(code: str, client, reg, out_dir: Path, timeout: float) -> dict:
    p = payloads(code)
    t0 = time.time()
    pre = client.post("/factory/capabilities/preflight", json={"payloads": p}).json()
    print(f"\n=== {code}: preflight {pre['status']} gaps={[g['name'] for g in pre.get('remaining', [])]}", flush=True)
    status = pre
    while status["status"] == "BUILDING" and time.time() - t0 < timeout:
        time.sleep(5)
        status = client.get(f"/factory/capabilities/builds/{pre['build_id']}").json()
        last = status.get("steps", [])[-1:] or [{}]
        print(f"  {time.time() - t0:6.0f}s {status['status']} {last[0].get('agent', '')} {last[0].get('outcome', '')}", flush=True)
    log = client.get(f"/factory/capabilities/builds/{pre['build_id']}/log").json() if pre.get("build_id") else {}
    result = {"use_case": code, "build": status, "build_seconds": round(time.time() - t0), "log": log}
    print(f"  build {status['status']} in {result['build_seconds']}s; published {status.get('published')}; error {status.get('error')}")
    for s in status.get("steps", []):
        print(f"    {s['agent']:<28} attempt {s['attempt']} {s['outcome']:<13} {s['seconds']:6.1f}s  {s['detail'][:150]}")
    if status["status"] == "READY":
        codes = {}
        for st in ["s3", "s4", "s5", "s6", "s7"]:
            codes[st] = client.post(f"/factory/data-engine/{st}", json=p[st]).status_code
        s8 = client.post("/factory/api-engine/s8", json=p["s8"]).json()
        tenant = f"demo-{code.lower()}"
        result["run"] = {"stages": codes, "s8": s8.get("status"), "contract_tests": s8.get("contract_tests"),
                         "failed_contract_tests": s8.get("failed_contract_tests")}
        print(f"  run: {codes} s8 {s8.get('status')} {s8.get('contract_tests')}")
        if s8.get("status") == "DEPLOYED":
            creds = client.get(f"/factory/runs/{tenant}/credentials").json()
            tok = client.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": creds["client_id"],
                                                    "client_secret": creds["client_secret"]}).json()["access_token"]
            h = {"Authorization": f"Bearer {tok}", "x-fault-injection": "off"}
            from factory.data_engine import packs

            uc = packs.use_case_pack(code).use_case
            ep = next(e for e in uc.endpoints if e.item is None)
            q = reg.runs[tenant].stores.tables.query
            mid = q(f"SELECT m.member_number FROM {ep.entity} x JOIN member_demographics m ON m.member_id = x.{ep.member_column} "
                    "ORDER BY 1 LIMIT 1").to_pylist()[0]["member_number"]
            sample = client.get(ep.path.replace("{member_id}", mid).join(["/v1", ""]), headers=h).json()
            result["sample_response"] = sample
            print(f"  GET /v1{ep.path.replace('{member_id}', mid)} ->\n{json.dumps(sample, indent=1, default=str)[:1500]}")
        result["packs"] = {pk.id: pk.model_dump(mode="json", by_alias=True) for pk in __import__("factory.data_engine.packs", fromlist=["x"]).active()
                           if pk.id.endswith(code) or any(pk.id == f"entity.{e}" for e in CASES[code]["s4"])}
    (out_dir / f"{code}.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", default=list(CASES))
    ap.add_argument("--llm-profile", default="memory-local", help="profile whose llm the agents use")
    ap.add_argument("--timeout", type=float, default=7200, help="seconds to wait per build")
    args = ap.parse_args()

    from fastapi.testclient import TestClient

    from factory.adapters import build_llm
    from factory.capability import reset_packs
    from factory.config import load_profile
    from factory.control.registry import Registry
    from factory.control.server import create_control_app

    llm = build_llm(load_profile(args.llm_profile))
    print(f"agents use {type(llm).__name__} {getattr(llm, 'model', '')} at {getattr(llm, 'base_url', '')}")
    out_dir = REPO / "out" / "agent-demo"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {}
    for code in args.only:
        reset_packs()  # start each case from a registry without its packs
        reg = Registry(load_profile("memory-offline"))
        client = TestClient(create_control_app(reg, capability_layer=True, builder="agents", llm=llm))
        r = run_case(code, client, reg, out_dir, args.timeout)
        summary[code] = {"build": r["build"]["status"], "seconds": r["build_seconds"], "run": r.get("run", {}).get("s8")}
    print("\nSUMMARY", json.dumps(summary, indent=1))
    return 0 if all(v["build"] == "READY" and v["run"] == "DEPLOYED" for v in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
