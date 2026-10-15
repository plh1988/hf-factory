"""Stage s7: rule engine over every entity, privacy scan, rule report and manifest."""

from __future__ import annotations

import json
import re

from factory import GENERATOR_VERSION
from factory.contracts import Manifest
from factory.data_engine import rules as R
from factory.data_engine.context import GenContext
from factory.data_engine.rng import fmt_checksum

SSN_LIKE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[a-z]{2,}", re.I)
PHONE = re.compile(r"\(?\b\d{3}\)?[ -]?(\d{3})-\d{4}\b")


def luhn_ok(npi: str) -> bool:
    digits = [int(c) for c in "80840" + npi]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return total % 10 == 0


def run_rules(ctx: GenContext) -> dict:
    tables = ctx.stores.tables
    present = {e for e in ctx.counts if tables.exists(e)}
    rule_list = R.fk_rules() + R.domain_rules(str(ctx.as_of), ctx.tenant_id) + [R.p01_rule(e, ctx.tenant_id) for e in sorted(present)]
    results = []
    for rule in rule_list:
        if not set(rule.needs) <= present:
            results.append({"rule": rule.id, "status": "SKIPPED", "violations": None, "description": rule.description})
            continue
        n = int(tables.query(rule.sql).column(0)[0].as_py() or 0)
        results.append({"rule": rule.id, "status": "PASS" if n == 0 else "FAIL", "violations": n, "description": rule.description})
    checked = [r for r in results if r["status"] != "SKIPPED"]
    fk = [r for r in checked if r["rule"].startswith("R-01")]
    return {
        "rules_checked": len(checked),
        "passed": sum(r["status"] == "PASS" for r in checked),
        "failed": sum(r["status"] == "FAIL" for r in checked),
        "skipped": len(results) - len(checked),
        "referential_integrity": "100%" if all(r["status"] == "PASS" for r in fk) else
        f"{100 * sum(r['status'] == 'PASS' for r in fk) / max(len(fk), 1):.1f}%",
        "results": results,
    }


def privacy_scan(ctx: GenContext) -> dict:
    tables = ctx.stores.tables
    findings: list[str] = []
    if tables.exists("provider"):
        bad = [n for n in tables.query("SELECT npi FROM provider").column(0).to_pylist() if not luhn_ok(n)]
        if bad:
            findings.append(f"{len(bad)} NPIs fail the Luhn check")
    texts: list[str] = []
    if tables.exists("asset_chunk"):
        texts += tables.query("SELECT text FROM asset_chunk").column(0).to_pylist()
    if tables.exists("faq_entry"):
        texts += tables.query("SELECT answer FROM faq_entry").column(0).to_pylist()
    for t in texts:
        if SSN_LIKE.search(t):
            findings.append("SSN-like number in generated text")
        for m in EMAIL.finditer(t):
            if not m.group(0).lower().endswith("@example.test"):
                findings.append(f"non-reserved email in generated text: {m.group(0)}")
        for m in PHONE.finditer(t):
            if m.group(1) != "555":
                findings.append("non-reserved phone number in generated text")
    dupes = len(texts) - len(set(texts))
    return {
        "status": "PASS" if not findings else "FAIL",
        "checks": ["NPI Luhn (registry match not checked: needs a pinned NPPES snapshot)",
                   "SSN-like patterns in LLM text", "reserved email/phone formats in LLM text",
                   "duplicate generated texts (informational)"],
        "findings": sorted(set(findings)),
        "duplicate_texts": dupes,
    }


def run(ctx: GenContext) -> dict:
    report = run_rules(ctx)
    privacy = privacy_scan(ctx)
    r = ctx.bp.repeatability
    manifest = Manifest(
        blueprint_hash=ctx.bp.blueprint_hash,
        data_spec_hash=ctx.bp.data_spec_hash,
        root_seed=r.root_seed,
        as_of_date=r.as_of_date,
        catalog_version=r.catalog_version,
        generator_version=GENERATOR_VERSION,
        entity_counts=dict(sorted(ctx.counts.items())),
        entity_checksums={k: fmt_checksum(v) for k, v in sorted(ctx.checksums.items())},
        rule_report={k: v for k, v in report.items() if k != "results"},
        privacy_scan=privacy["status"],
        llm_model=ctx.stores.llm.model,
        embedding_model=ctx.stores.llm.embedding_model,
    )
    report_uri = ctx.stores.blobs.put("reports/rule-report.json", json.dumps({**report, "privacy": privacy}, indent=2, default=str).encode(), "application/json")
    manifest_uri = ctx.stores.blobs.put(f"manifest/{r.root_seed}.json", manifest.model_dump_json(indent=2).encode(), "application/json")
    return {"report": report, "privacy": privacy, "manifest": manifest, "report_uri": report_uri, "manifest_uri": manifest_uri}
