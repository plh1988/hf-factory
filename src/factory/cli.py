"""Command line.

  python -m factory doctor                          show the active profile and adapter health
  python -m factory generate --blueprint B [--export DIR]   run data engine s3-s7 (+ optional Parquet export)
  python -m factory verify --blueprint B            repro-check: regenerate with different shard counts, compare checksums
  python -m factory up --blueprint B                data engine s3-s7, API engine s8, delivery manifest, then serve the API

The backend is chosen by FACTORY_PROFILE (memory | memory-offline | gcp), nothing else.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from factory.adapters import build_stores
from factory.config import Settings, load_profile
from factory.contracts import Blueprint, S2Output

DEFAULT_BLUEPRINT = "blueprints/s1_dev.json"


def _stores(args):
    s2 = S2Output.load(args.s2) if args.s2 else None
    profile = load_profile(s2=s2)
    return profile, build_stores(profile)


def _print_run(run) -> None:
    for r in run.results:
        rows = sum(e.rows for e in r.entities)
        print(f"  {r.stage}  {r.status:<9} {r.duration_s:>6.2f}s  {rows:>9,} rows")
    rep = run.report
    print(f"  rules: {rep.get('passed')}/{rep.get('rules_checked')} passed, referential integrity {rep.get('referential_integrity')}, "
          f"privacy scan {run.privacy.get('status')}")
    for x in rep.get("results", []):
        if x["status"] == "FAIL":
            print(f"    FAIL {x['rule']}: {x['description']} ({x['violations']} rows)")


def cmd_doctor(args) -> int:
    profile, stores = _stores(args)
    print(f"FACTORY_PROFILE = {profile.name}")
    for port in ["tables", "blobs", "vectors", "events", "text_cache", "llm", "deployer"]:
        adapter = getattr(stores, port)
        try:
            status = adapter.health()
        except Exception as exc:  # report, don't crash: doctor is for diagnosis
            status = f"ERROR {exc!r}"
        print(f"  {port:<11} {type(adapter).__name__:<22} {status}")
    return 0


def cmd_generate(args) -> int:
    from factory.data_engine.runner import run_data_engine

    profile, stores = _stores(args)
    bp = Blueprint.load(args.blueprint)
    print(f"profile={profile.name} blueprint={bp.blueprint_id} members={bp.data_spec.volumes.members:,} shards={args.shards}")
    run = run_data_engine(bp, stores, shards=args.shards)
    _print_run(run)
    if args.export:
        import pyarrow.parquet as pq

        out = Path(args.export)
        (out / "tables").mkdir(parents=True, exist_ok=True)
        for entity in run.manifest.entity_counts:
            pq.write_table(stores.tables.read(entity), out / "tables" / f"{entity}.parquet")
        for key in stores.blobs.list():
            if key.startswith("secrets/"):
                continue
            path = out / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(stores.blobs.get(key))
        print(f"  exported tables and documents to {out.resolve()}")
    return 0 if run.passed else 1


def cmd_verify(args) -> int:
    from factory.data_engine.runner import run_data_engine

    bp = Blueprint.load(args.blueprint)
    results = {}
    for shards in (1, args.shards):
        _, stores = _stores(args)
        results[shards] = run_data_engine(bp, stores, shards=shards).manifest.entity_checksums
    a, b = results[1], results[args.shards]
    diff = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    for k in sorted(a):
        print(f"  {k:<28} {a[k]}  {'MATCH' if k not in diff else 'DIFF ' + str(b.get(k))}")
    print("repro-check:", "MATCH on all entities" if not diff else f"DIFF on {diff}")
    return 0 if not diff else 1


def cmd_up(args) -> int:
    from factory.api_engine.stage import run_api_engine
    from factory.data_engine.runner import run_data_engine

    profile, stores = _stores(args)
    bp = Blueprint.load(args.blueprint)
    t0 = time.perf_counter()
    print(f"profile={profile.name} blueprint={bp.blueprint_id} members={bp.data_spec.volumes.members:,}")
    print("data engine (s3-s7)")
    run = run_data_engine(bp, stores, shards=args.shards)
    _print_run(run)
    if not run.passed:
        print("data_quality_gate FAILED: the API engine does not start on red data")
        return 1
    print("api engine (s8)")
    api = run_api_engine(bp, stores, run.manifest_uri)
    r = api.result
    print(f"  s8  {r.status} {r.duration_s:.2f}s  endpoints={r.endpoint_count}  contract tests {r.contract_tests['passed']}/{r.contract_tests['total']}")
    for t in api.contract_tests:
        if not t["passed"]:
            print(f"    FAIL {t['endpoint']}: {t['test']} (HTTP {t['status']})")
    if r.status != "DEPLOYED":
        return 1

    delivery = {
        "status": "READY", "tenant": bp.tenant.id, "total_duration_s": round(time.perf_counter() - t0, 2),
        "endpoints": {"api_base_url": r.gateway_url, "openapi": r.openapi_uri, "tool_manifest": r.tool_manifest_uri},
        "credentials": {"type": "OAuth2 client credentials", "token_url": r.gateway_url.rsplit("/v1", 1)[0] + "/oauth/token",
                        "secret_ref": r.credentials_secret_ref},
        "data": {"total_rows": sum(run.manifest.entity_counts.values()), "manifest": run.manifest_uri,
                 "rules_passed": f"{run.report['passed']}/{run.report['rules_checked']}", "privacy_scan": run.privacy["status"]},
        "reproducibility": {"blueprint_hash": run.manifest.blueprint_hash, "data_spec_hash": run.manifest.data_spec_hash,
                            "root_seed": run.manifest.root_seed, "as_of_date": str(run.manifest.as_of_date)},
    }
    stores.blobs.put("delivery/manifest.json", json.dumps(delivery, indent=2).encode(), "application/json")
    print("delivery manifest (s11)")
    print(json.dumps(delivery, indent=2))

    if profile.deployer.kind != "local":
        return 0
    creds = json.loads(stores.blobs.get("secrets/api-client.json"))
    print("\nLocal test credentials (stand-in for Secret Manager):")
    print(f"  client_id={creds['client_id']}  client_secret={creds['client_secret']}")
    print(f"  docs: http://{stores.deployer.host}:{stores.deployer.port}/docs")
    if args.no_serve:
        return 0
    import uvicorn

    uvicorn.run(api.app, host=stores.deployer.host, port=stores.deployer.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="factory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--s2", help="s2 output JSON (data store locations); needed for the gcp profile")
    p.add_argument("-v", "--verbose", action="store_true", help="log progress events")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    g = sub.add_parser("generate")
    g.add_argument("--blueprint", default=DEFAULT_BLUEPRINT)
    g.add_argument("--shards", type=int, default=4)
    g.add_argument("--export", help="write tables (Parquet) and documents to this folder")
    v = sub.add_parser("verify")
    v.add_argument("--blueprint", default=DEFAULT_BLUEPRINT)
    v.add_argument("--shards", type=int, default=6)
    u = sub.add_parser("up")
    u.add_argument("--blueprint", default=DEFAULT_BLUEPRINT)
    u.add_argument("--shards", type=int, default=4)
    u.add_argument("--no-serve", action="store_true", help="stop after s8 instead of serving the API")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    _ = Settings()  # validates FACTORY_PROFILE early
    return {"doctor": cmd_doctor, "generate": cmd_generate, "verify": cmd_verify, "up": cmd_up}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
