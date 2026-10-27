"""Stage s7: rule engine over every entity, privacy scan, rule report and manifest."""

from __future__ import annotations

import json
import re

from factory import GENERATOR_VERSION
from factory.contracts import Manifest, canonical_hash
from factory.data_engine import packs
from factory.data_engine import rules as R
from factory.data_engine.context import GenContext
from factory.data_engine.rng import fmt_checksum

SSN_LIKE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[a-z]{2,}", re.I)
PHONE = re.compile(r"\(?\b\d{3}\)?[ -]?(\d{3})-\d{4}\b")


def luhn_ok(npi: str) -> bool:
    """Luhn check of an NPI including its check digit, over the 80840 prefix (the NPI standard)."""
    digits = [int(c) for c in "80840" + npi]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return total % 10 == 0


RULE_BASE = re.compile(r"^[A-Z]-\d+")


def _base(rule_id: str) -> str:
    """R-04b -> R-04; capability-pack rule ids (F-PREMIUM-01) are their own base."""
    m = RULE_BASE.match(rule_id)
    return m.group(0) if m else rule_id


def all_rules(ctx: GenContext, present: set[str]) -> list[R.Rule]:
    """Every rule that applies to this run: foreign keys, domain rules, rules of active capability packs, and a P-01
    stamp check per generated entity."""
    return (R.fk_rules() + R.domain_rules(str(ctx.as_of), ctx.tenant_id) + R.pack_rules(str(ctx.as_of))
            + [R.p01_rule(e, ctx.tenant_id) for e in sorted(present)])


def _evaluate(ctx: GenContext, rule: R.Rule, present: set[str]) -> dict:
    """Run one rule's SQL (a count of violating rows); SKIPPED when a table it needs was not generated."""
    if not set(rule.needs) <= present:
        return {"rule": rule.id, "status": "SKIPPED", "violations": None, "description": rule.description,
                "waiting_for": sorted(set(rule.needs) - present)}
    n = int(ctx.stores.tables.query(rule.sql).column(0)[0].as_py() or 0)
    return {"rule": rule.id, "status": "PASS" if n == 0 else "FAIL", "violations": n, "description": rule.description}


def check_rules(ctx: GenContext, requested: list[str]) -> tuple[dict[str, str], list[dict]]:
    """Stage-level check of the rule ids a job payload names (e.g. ["R-04", "F-04", "P-02"]).

    A rule id covers all its sub-rules (R-04 -> R-04a, R-04b). Sub-rules whose tables do not exist yet
    are deferred to s7. Summary per id: PASS, FAIL, PASS_PARTIAL (rest deferred), DEFERRED_TO_S7, NOT_CHECKED."""
    present = {e for e in ctx.counts if ctx.stores.tables.exists(e)}
    rules = [r for r in all_rules(ctx, present) if not r.id.startswith("R-01:") and not r.id.startswith("P-01:")]
    summary, details = {}, []
    for rid in requested:
        subs = [_evaluate(ctx, r, present) for r in rules if _base(r.id) == rid]
        details += subs
        ran = [s for s in subs if s["status"] != "SKIPPED"]
        if not subs:
            summary[rid] = "NOT_CHECKED"
        elif any(s["status"] == "FAIL" for s in ran):
            summary[rid] = "FAIL"
        elif not ran:
            summary[rid] = "DEFERRED_TO_S7"
        else:
            summary[rid] = "PASS" if len(ran) == len(subs) else "PASS_PARTIAL"
    return summary, details


def run_rules(ctx: GenContext, families: list[str] | None = None) -> dict:
    """Run all rules (optionally only some families) and summarise: checked, passed, failed, skipped, referential integrity."""
    present = {e for e in ctx.counts if ctx.stores.tables.exists(e)}
    rule_list = [r for r in all_rules(ctx, present) if families is None or r.id[0] in families]
    results = [_evaluate(ctx, rule, present) for rule in rule_list]
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


def privacy_scan(ctx: GenContext, options: dict | None = None) -> dict:
    """Privacy checks on the generated data: NPI validity, SSN-like numbers and non-reserved emails or phones in
    LLM text, and duplicate texts (informational). 'rare_combinations' does not apply: no real records are used.
    """
    options = options or {"near_duplicates": True, "rare_combinations": True, "reserved_formats": True}
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
        "requested": {
            "reserved_formats": "CHECKED" if options.get("reserved_formats") else "NOT_REQUESTED",
            "near_duplicates": f"CHECKED ({dupes} exact duplicate texts)" if options.get("near_duplicates") else "NOT_REQUESTED",
            "rare_combinations": "NOT_APPLICABLE (no real seed records are used; rows are drawn from pinned distributions)"
            if options.get("rare_combinations") else "NOT_REQUESTED",
        },
    }


def run(ctx: GenContext, rule_sets: list[str] | None = None, privacy_options: dict | None = None) -> dict:
    """Stage s7: rules, privacy scan, rule report and manifest (written to the BlobStore).

    The manifest's per-entity checksums are what replays and repro-checks compare against.
    """
    report = run_rules(ctx, rule_sets)
    privacy = privacy_scan(ctx, privacy_options)
    r = ctx.spec
    pack_pins = packs.pins(entities=set(ctx.counts))
    manifest = Manifest(
        tenant_id=r.tenant_id,
        dataset_id=r.dataset_id,
        # payload hashes plus the capability packs the data came from: a replay must use exactly these packs
        data_spec_hash=canonical_hash({"payloads": r.data_spec_hash(), "capability_packs": pack_pins}) if pack_pins
        else r.data_spec_hash(),
        capability_packs=pack_pins,
        payload_hashes=dict(sorted(r.payload_hashes.items())),
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
