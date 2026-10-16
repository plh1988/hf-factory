"""Command line.

  python -m factory serve     REST control plane: trigger s3..s7 and s8 one call at a time with job payloads
  python -m factory doctor    show the active profile and adapter health

The backend is chosen by FACTORY_PROFILE (memory | memory-offline | gcp), nothing else.
"""

from __future__ import annotations

import argparse
import logging
import sys

from factory.adapters import build_stores
from factory.config import Settings, load_profile
from factory.contracts import S2Output


def _profile(args):
    s2 = S2Output.load(args.s2) if args.s2 else None
    return load_profile(s2=s2), s2


def cmd_doctor(args) -> int:
    profile, _ = _profile(args)
    stores = build_stores(profile)
    print(f"FACTORY_PROFILE = {profile.name}")
    for port in ["tables", "blobs", "vectors", "events", "text_cache", "llm", "deployer"]:
        adapter = getattr(stores, port)
        try:
            status = adapter.health()
        except Exception as exc:  # report, don't crash: doctor is for diagnosis
            status = f"ERROR {exc!r}"
        print(f"  {port:<11} {type(adapter).__name__:<22} {status}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from factory.control.registry import Registry
    from factory.control.server import create_control_app

    profile, s2 = _profile(args)
    host, port = profile.deployer.get("host", "127.0.0.1"), int(profile.deployer.get("port", 8080))
    print(f"profile={profile.name}")
    print(f"control plane: http://{host}:{port}/factory/docs")
    print("  POST /factory/data-engine/s3 .. s7, then POST /factory/api-engine/s8")
    print(f"  tenant API (after s8): http://{host}:{port}/docs")
    uvicorn.run(create_control_app(Registry(profile, s2)), host=host, port=port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="factory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--s2", help="s2 output JSON (data store locations); needed for the gcp profile")
    p.add_argument("-v", "--verbose", action="store_true", help="log progress events")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("doctor")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    _ = Settings()  # validates FACTORY_PROFILE early
    return {"serve": cmd_serve, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
