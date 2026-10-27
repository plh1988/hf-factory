"""Publish capability packs (YAML or JSON files) to a running control plane, in the order given.

    python scripts/publish_pack.py examples/capability-packs/mbp/premium_invoice.yaml examples/capability-packs/mbp/uc_mbp.yaml
    python scripts/publish_pack.py --url http://localhost:8080 --no-gates my_pack.yaml

Prints each pack's gates. Exits 1 at the first pack that is rejected.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
import yaml


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--url", default="http://127.0.0.1:8080", help="control plane base URL")
    ap.add_argument("--no-gates", action="store_true", help="static checks only (skip the sandbox gates)")
    args = ap.parse_args()
    with httpx.Client(base_url=args.url, timeout=600) as client:
        for path in args.files:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            r = client.post("/factory/capabilities/packs", json=doc, params={"gates": str(not args.no_gates).lower()})
            body = r.json()
            ok = r.status_code == 201
            print(f"{'ACTIVE  ' if ok else 'REJECTED'} {path.name}: {body.get('ref') or body.get('message')}")
            for g in body.get("gates") or body.get("details") or []:
                if isinstance(g, dict) and "gate" in g:
                    print(f"    {'pass' if g['passed'] else 'FAIL'}  {g['gate']}  {g.get('detail', '')}")
            if not ok:
                print(json.dumps(body, indent=2)[:2000])
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
