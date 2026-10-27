"""Command line.

  python -m factory serve     REST control plane: trigger s3..s7 and s8 one call at a time with job payloads
  python -m factory doctor    show the active profile and adapter health
  factory task --stage s4 --tenant <id>   one data-engine shard (Cloud Run Job task; index from CLOUD_RUN_TASK_INDEX)
  factory api --tenant <id>               the tenant business API (Cloud Run service deployed by stage s8)

The backend is chosen by FACTORY_PROFILE (memory | memory-offline | gcp), nothing else.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from factory.adapters import build_stores
from factory.config import Settings, load_profile
from factory.contracts import S2Output


def _profile(args):
    """Active profile and s2 output: from --s2 <file>, else from FACTORY_S2_JSON (how Cloud Run containers get it)."""
    if args.s2:
        s2 = S2Output.load(args.s2)
    else:
        inline = Settings().s2_json
        s2 = S2Output.model_validate_json(inline) if inline else None
    return load_profile(s2=s2), s2


def cmd_doctor(args) -> int:
    """Print the active profile and every adapter's health check; errors are reported, not raised."""
    profile, _ = _profile(args)
    stores = build_stores(profile)
    print(f"FACTORY_PROFILE = {profile.name}")
    for port in ["tables", "analytics", "blobs", "vectors", "events", "text_cache", "llm", "executor", "deployer"]:
        adapter = getattr(stores, port)
        try:
            status = adapter.health()
        except Exception as exc:  # report, don't crash: doctor is for diagnosis
            status = f"ERROR {exc!r}"
        print(f"  {port:<11} {type(adapter).__name__:<22} {status}")
    return 0


def cmd_serve(args) -> int:
    """Run the REST control plane (and, after s8, the tenant API) with uvicorn."""
    import uvicorn

    from factory.control.registry import Registry
    from factory.control.server import create_control_app

    profile, s2 = _profile(args)
    # Cloud Run sets PORT; containers must bind 0.0.0.0 (pass --host 0.0.0.0). Defaults come from the profile.
    host = args.host or profile.deployer.get("host", "127.0.0.1")
    port = int(args.port or os.environ.get("PORT") or profile.deployer.get("port", 8080))
    print(f"profile={profile.name}")
    print(f"control plane: http://{host}:{port}/factory/docs")
    print("  POST /factory/data-engine/s3 .. s7, then POST /factory/api-engine/s8")
    print(f"  tenant API (after s8): http://{host}:{port}/docs")
    layer = Settings().capability_layer
    print(f"  capability layer: {'on (gaps start builds, 202)' if layer else 'off'}; packs: {profile.packs.kind}")
    uvicorn.run(create_control_app(Registry(profile, s2), capability_layer=layer), host=host, port=port, log_level="info")
    return 0


def cmd_task(args) -> int:
    """One Cloud Run Job task: load the run spec from the BlobStore, generate shard CLOUD_RUN_TASK_INDEX, write its result."""
    import json

    from factory.adapters.jobs import task_key
    from factory.contracts import RunSpec
    from factory.data_engine.context import GenContext
    from factory.data_engine.runner import run_task_remote

    profile, _ = _profile(args)
    stores = build_stores(profile, namespace=args.tenant)
    index = args.task_index if args.task_index is not None else int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    spec = RunSpec.model_validate_json(stores.blobs.get("runs/spec.json"))
    shards = json.loads(stores.blobs.get(f"runs/{args.stage}/job.json"))["shards"]
    ctx = GenContext(spec, stores, shards=shards)
    result = run_task_remote(ctx, args.stage, index)
    stores.blobs.put(task_key(args.stage, index), json.dumps(result).encode(), "application/json")
    print(f"{args.stage} task {index}: {sum(result['counts'].values()):,} rows")
    return 0


def cmd_api(args) -> int:
    """The tenant API as its own service: built from the s8 job and the s7 manifest stored in the BlobStore."""
    import uvicorn

    from factory.api_engine.app import create_app
    from factory.contracts import ApiJob, Manifest

    profile, _ = _profile(args)
    stores = build_stores(profile, namespace=args.tenant)
    job = ApiJob.model_validate_json(stores.blobs.get("jobs/s8.json"))
    manifests = stores.blobs.list("manifest/")
    if not manifests:
        print("no s7 manifest found for this tenant", file=sys.stderr)
        return 1
    manifest = Manifest.model_validate_json(stores.blobs.get(manifests[-1]))
    app = create_app(job, stores, tenant_id=args.tenant, seed=manifest.root_seed, as_of=manifest.as_of_date)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of the `factory` command."""
    p = argparse.ArgumentParser(prog="factory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--s2", help="s2 output JSON (data store locations); needed for the gcp profile")
    p.add_argument("-v", "--verbose", action="store_true", help="log progress events")
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", help="bind address (default from the profile; 0.0.0.0 in containers)")
    sv.add_argument("--port", type=int, help="port (default: $PORT, then the profile)")
    sub.add_parser("doctor")
    tk = sub.add_parser("task", help="run one data-engine shard (Cloud Run Job task)")
    tk.add_argument("--stage", required=True, choices=["s4", "s5"])
    tk.add_argument("--tenant", required=True)
    tk.add_argument("--task-index", type=int, help="default: $CLOUD_RUN_TASK_INDEX")
    ap = sub.add_parser("api", help="serve one tenant's business API (Cloud Run service)")
    ap.add_argument("--tenant", required=True)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    _ = Settings()  # validates FACTORY_PROFILE early
    return {"serve": cmd_serve, "doctor": cmd_doctor, "task": cmd_task, "api": cmd_api}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
