"""Regression snapshot: run the spec scenarios offline and record everything that must not change by accident.

Output (JSON): per scenario the HTTP status of s3..s8, per-entity row counts and checksums, the rule summary and
every contract test result. Two snapshots compare equal exactly when the generated data and the API behaviour
are unchanged.

    python scripts/regression_snapshot.py out/snap-new.json              # this checkout
    python scripts/regression_snapshot.py out/snap-old.json --repo ../old # another checkout (e.g. a git worktree)
    python scripts/regression_snapshot.py --compare out/snap-old.json out/snap-new.json

Scenarios: S1 (spec payloads as is), S1 with member scenarios (s5 scenario_mix also sent to s4, so PEV recipes run),
and S2-S4 scaled as in tests/test_scenarios.py. Uses the memory-offline profile (no network).
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

STAGES = ["s3", "s4", "s5", "s6", "s7"]


def snapshot(repo: Path) -> dict:
    warnings.filterwarnings("ignore")
    sys.path[:0] = [str(repo / "src"), str(repo / "tests")]
    from fastapi.testclient import TestClient
    from test_scenarios import scenario_payloads

    from factory.config import load_profile
    from factory.control.registry import Registry
    from factory.control.server import create_control_app

    fix = repo / "tests" / "fixtures"

    def s1(member_scenarios: bool) -> dict:
        p = {st: json.loads((fix / "s1_jobs" / f"{st}.json").read_text(encoding="utf-8")) for st in [*STAGES, "s8"]}
        if member_scenarios:
            p["s4"]["scenario_mix"] = p["s5"]["scenario_mix"]
        return p

    cases = {"s1": s1(False), "s1+members": s1(True), **{sc: scenario_payloads(sc) for sc in ["s2", "s3", "s4"]}}
    out = {}
    for name, payloads in cases.items():
        reg = Registry(load_profile("memory-offline"))
        client = TestClient(create_control_app(reg))
        status = {st: client.post(f"/factory/data-engine/{st}", json=payloads[st]).status_code for st in STAGES}
        r8 = client.post("/factory/api-engine/s8", json=payloads["s8"])
        status["s8"] = r8.status_code
        run = reg.runs[r8.json().get("tenant") or reg.active]
        manifest = run.data.manifest
        out[name] = {
            "status": status,
            "counts": dict(sorted(manifest.entity_counts.items())) if manifest else {},
            "checksums": dict(sorted(manifest.entity_checksums.items())) if manifest else {},
            "rules": {k: run.data.report.get(k) for k in ("checked", "passed", "failed", "skipped")},
            "contract_tests": [[t["endpoint"], t["test"], t["status"], t["passed"]] for t in (run.api.contract_tests if run.api else [])],
        }
        ok = sum(t[3] for t in out[name]["contract_tests"])
        print(f"{name:<11} {status}  rows={sum(out[name]['counts'].values()):,}  contract={ok}/{len(out[name]['contract_tests'])}",
              file=sys.stderr)
    return out


def compare(old: dict, new: dict) -> int:
    diffs = 0
    for sc in sorted(set(old) | set(new)):
        a, b = old.get(sc, {}), new.get(sc, {})
        for part in ("status", "rules", "contract_tests"):
            if a.get(part) != b.get(part):
                diffs += 1
                print(f"{sc}: {part} differs")
        for part in ("counts", "checksums"):
            ea, eb = a.get(part, {}), b.get(part, {})
            for e in sorted(set(ea) | set(eb)):
                if ea.get(e) != eb.get(e):
                    diffs += 1
                    print(f"{sc}: {part}[{e}] {ea.get(e)} -> {eb.get(e)}")
    print("IDENTICAL" if diffs == 0 else f"{diffs} differences")
    return 1 if diffs else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?", help="snapshot file to write")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]), help="checkout to snapshot")
    ap.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"), help="compare two snapshot files")
    args = ap.parse_args()
    if args.compare:
        old, new = (json.loads(Path(p).read_text(encoding="utf-8")) for p in args.compare)
        return compare(old, new)
    if not args.out:
        ap.error("give a snapshot file to write, or --compare OLD NEW")
    snap = snapshot(Path(args.repo).resolve())
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
