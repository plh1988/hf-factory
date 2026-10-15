"""Stage s6: care management from inpatient discharges (R-10) and knowledge assets (LLM text + chunks + index).

v1 runs this after s5 (design doc gap 1): TOC records need facility claims, asset links need denial codes.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pyarrow as pa

from factory.data_engine import catalog
from factory.data_engine.context import GenContext, dates, stamps
from factory.data_engine.knowledge_catalog import USE_CASE_ORDER, AssetSpec, asset_specs

DAY = np.timedelta64(1, "D")
PROMPT_VERSION = "kb-doc-v1"
SYSTEM = (
    "You write governed health-plan documents for a synthetic test environment. Use only the facts given. "
    "Do not invent names, phone numbers, dollar amounts or codes. Plain language, short paragraphs, no markdown tables."
)


# ------------------------------------------------------------------ care management
def gen_care(ctx: GenContext) -> None:
    t = ctx.stores.tables.query(
        """SELECT claim_id, claim_number, member_id, facility_npi, service_from_dt, service_to_dt
           FROM claim_header WHERE bill_type = '0111' ORDER BY claim_number"""
    )
    rows = t.to_pylist()
    keys = np.array([(int(r["claim_number"][4:11]) - 1) * 64 + int(r["claim_number"][12:]) for r in rows], dtype=np.int64)
    rr = ctx.rr("transition_of_care", keys)
    admit = np.array([np.datetime64(r["service_from_dt"], "D") for r in rows], dtype="datetime64[D]")
    discharge = np.array([np.datetime64(r["service_to_dt"], "D") for r in rows], dtype="datetime64[D]")
    members = np.array([r["member_id"] for r in rows], dtype=object)

    # readmit_30d is derived: another admission for the same member within 30 days of discharge
    readmit = np.zeros(len(rows), dtype=bool)
    by_member: dict[str, list[int]] = {}
    for i, mid in enumerate(members):
        by_member.setdefault(mid, []).append(i)
    for idxs in by_member.values():
        for i in idxs:
            readmit[i] = any(admit[j] > discharge[i] and (admit[j] - discharge[i]) <= 30 * DAY for j in idxs if j != i)

    call_due = discharge.astype("datetime64[s]") + np.timedelta64(48, "h")
    past = call_due < ctx.as_of.astype("datetime64[s]")
    toc_status = np.where(past, rr.choice("toc", ["CALL_DONE", "VISIT_DONE", "MISSED"], [60, 25, 15]), "PENDING")
    case_ids = ctx.ids("care_case", keys)
    if ctx.wants("care_case"):
        closed = discharge < ctx.as_of - 30 * DAY
        ctx.write("care_case", {
            "case_id": pa.array(case_ids),
            "member_id": pa.array(members.tolist()),
            "case_type": pa.array(["TOC"] * len(rows)),
            "status": pa.array(np.where(closed, "CLOSED", rr.choice("cs", ["OPEN", "ENGAGED"])).tolist()),
            "priority": pa.array(rr.choice("prio", ["LOW", "MEDIUM", "HIGH"], [20, 50, 30]).tolist()),
            "opened_dt": dates(discharge),
            "closed_dt": dates(np.where(closed, discharge + 30 * DAY, np.datetime64("NaT", "D"))),
            "assigned_role": pa.array(["RN"] * len(rows)),
            "source": pa.array(["ADT"] * len(rows)),
        })
    if ctx.wants("transition_of_care"):
        ctx.write("transition_of_care", {
            "toc_id": pa.array(ctx.ids("transition_of_care", keys)),
            "member_id": pa.array(members.tolist()),
            "facility_npi": pa.array([r["facility_npi"] for r in rows], pa.string()),
            "case_id": pa.array(case_ids),
            "admit_dt": dates(admit),
            "discharge_dt": dates(discharge),
            "disposition": pa.array(rr.choice("disp", ["HOME", "SNF", "HOME_HEALTH", "REHAB", "AMA"], [50, 20, 20, 7, 3]).tolist()),
            "toc_call_due_dt": stamps(call_due),
            "follow_up_due_dt": dates(discharge + 7 * DAY),
            "med_reconciliation_done": pa.array(past & rr.bernoulli("medrec", 0.7)),
            "toc_status": pa.array(toc_status.tolist()),
            "readmit_30d": pa.array(readmit),
        })


# ------------------------------------------------------------------ knowledge
def cache_key(model: str, prompt: str, seed: int) -> str:
    return hashlib.sha256(f"{model}|{PROMPT_VERSION}|{prompt}|{seed}".encode()).hexdigest()


def generate_text(ctx: GenContext, title: str, kind: str, audience: str, facts: list[str], stats: dict) -> tuple[str, str]:
    prompt = (
        f"TITLE: {title}\nDOCUMENT TYPE: {kind}\nAUDIENCE: {audience}\n"
        "Write the document using only these facts:\n" + "\n".join(f"- {f}" for f in facts)
    )
    llm = ctx.stores.llm
    key = cache_key(llm.model, prompt, ctx.seed)
    text = ctx.stores.text_cache.get(key)
    if text is None:
        text = llm.generate(prompt, system=SYSTEM)
        ctx.stores.text_cache.put(key, text)
        stats["generated"] += 1
    else:
        stats["served_from_cache"] += 1
    return text, key


def chunk_text(text: str, max_words: int = 350) -> list[str]:
    chunks, cur = [], []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if cur and sum(len(x.split()) for x in cur) + len(para.split()) > max_words:
            chunks.append("\n\n".join(cur))
            cur = []
        cur.append(para)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def gen_knowledge(ctx: GenContext) -> dict:
    plans = ctx.stores.tables.query(
        "SELECT plan_id, plan_code, plan_name, lob, network_type, plan_year, deductible, oop_max, copay_pcp, "
        "copay_specialist, coinsurance_pct FROM plan ORDER BY plan_code"
    ).to_pylist()
    specs = asset_specs(ctx, plans)
    stats = {"generated": 0, "served_from_cache": 0}
    year = str(ctx.as_of)[:4]
    a_ids, texts, keys_ = [], [], []
    for s in specs:
        text, key = generate_text(ctx, s.title, s.asset_type, s.audience, s.facts, stats)
        aid = ctx.ids("knowledge_asset", [s.key])[0]
        ctx.stores.blobs.put(f"docs/{s.asset_type.lower()}/{aid}.md", text.encode(), "text/markdown")
        a_ids.append(aid)
        texts.append(text)
        keys_.append(key)

    n = len(specs)
    ctx.write("knowledge_asset", {
        "asset_id": pa.array(a_ids),
        "asset_type": pa.array([s.asset_type for s in specs]),
        "title": pa.array([s.title for s in specs]),
        "version": pa.array(["1.0.0"] * n),
        "effective_dt": dates(np.full(n, np.datetime64(f"{year}-01-01"))),
        "expiry_dt": dates(np.full(n, np.datetime64(f"{year}-12-31"))),
        "audience": pa.array([s.audience for s in specs]),
        "lob": pa.array([[s.lob] if s.lob else [] for s in specs], pa.list_(pa.string())),
        "plan_id": pa.array([s.plan_id for s in specs], pa.string()),
        "use_case_code": pa.array([s.use_case for s in specs], pa.string()),
        "status": pa.array(["ACTIVE"] * n),
        "supersedes_asset_id": pa.array([None] * n, pa.string()),
        "language": pa.array(["EN"] * n),
        "reading_grade": pa.array([7 if s.audience == "MEMBER" else 10 for s in specs], pa.int32()),
        "license_class": pa.array(["OWNED"] * n),
        "approved_by": pa.array(["compliance-synthetic"] * n),
        "approval_dt": dates(np.full(n, np.datetime64(f"{int(year) - 1}-12-15"))),
        "text_cache_key": pa.array(keys_),
        "llm_model": pa.array([ctx.stores.llm.model] * n),
    })

    # chunks + vector index (only ACTIVE, approved assets are indexed)
    c_ids, c_asset, c_text, c_key, c_meta, c_spec = [], [], [], [], [], []
    for s, aid, text in zip(specs, a_ids, texts):
        for j, chunk in enumerate(chunk_text(text)):
            k = s.key * 100 + j
            c_key.append(k)
            c_asset.append(aid)
            c_text.append(chunk)
            c_spec.append(s)
            c_meta.append({"audience": s.audience, "asset_type": s.asset_type, "lob": s.lob, "use_case": s.use_case,
                           "effective_dt": f"{year}-01-01", "language": "EN"})
    c_ids = ctx.ids("asset_chunk", np.array(c_key))
    vectors = ctx.stores.llm.embed(c_text)
    ctx.stores.vectors.upsert(c_ids, vectors, [
        {"chunk_id": cid, "asset_id": aid, "title": s.title, "text": t, **meta}
        for cid, aid, s, t, meta in zip(c_ids, c_asset, c_spec, c_text, c_meta)
    ])
    ctx.write("asset_chunk", {
        "chunk_id": pa.array(c_ids),
        "asset_id": pa.array(c_asset),
        "section_path": pa.array([f"{s.title} > Part {j + 1}" for s, j in zip(c_spec, [k % 100 for k in c_key])]),
        "text": pa.array(c_text),
        "embedding_ref": pa.array(c_ids),
        "metadata": pa.array([json.dumps(m, sort_keys=True) for m in c_meta]),
    })
    chunks_by_asset: dict[str, list[str]] = {}
    for cid, aid in zip(c_ids, c_asset):
        chunks_by_asset.setdefault(aid, []).append(cid)

    write_faq_sop_links_golden(ctx, specs, a_ids, chunks_by_asset, stats)
    return {"llm_text": {**stats, "cache_key": "sha256(model, prompt_version, prompt, seed)"},
            "vector_index": {"loaded_chunks": len(c_ids)}}


def write_faq_sop_links_golden(ctx: GenContext, specs: list[AssetSpec], a_ids: list[str],
                               chunks_by_asset: dict[str, list[str]], stats: dict) -> None:
    by_key = {s.key: (s, aid) for s, aid in zip(specs, a_ids)}
    manual = next(s for s in specs if s.asset_type == "PROVIDER_MANUAL")

    faq_rows, sop_rows, gold_rows = [], [], []
    for uc in ctx.bp.scope.use_cases:
        if uc not in catalog.USE_CASES:
            continue
        info, n = catalog.USE_CASES[uc], USE_CASE_ORDER.index(uc)
        faq_spec, faq_aid = by_key[5000 + n]
        _, sop_aid = by_key[6000 + n]
        for qi, (variants, can) in enumerate(zip(info["faqs"], info["can"])):
            answer, _ = generate_text(ctx, f"Answer: {variants[0]}", "FAQ_ANSWER", faq_spec.audience,
                                      [can] + manual.facts[:3], stats)
            faq_rows.append({"key": (5000 + n) * 100 + qi, "asset_id": faq_aid, "question_variants": variants,
                             "answer": answer, "persona_tags": info["personas"], "use_case_code": uc})
            gold_rows.append({"key": n * 1000 + qi, "use_case_code": uc, "persona": info["personas"][qi % len(info["personas"])],
                              "question": variants[-1], "expected_chunk_ids": chunks_by_asset.get(faq_aid, []),
                              "expected_behavior": "ANSWER", "difficulty": "EASY"})
        for ei, esc in enumerate(info["escalate"]):
            gold_rows.append({"key": n * 1000 + 100 + ei, "use_case_code": uc, "persona": info["personas"][0],
                              "question": f"I need help with {esc.lower()}.", "expected_chunk_ids": chunks_by_asset.get(sop_aid, []),
                              "expected_behavior": "ESCALATE", "difficulty": "MULTI_HOP"})
        for ni, never in enumerate(info["never"]):
            gold_rows.append({"key": n * 1000 + 200 + ni, "use_case_code": uc, "persona": info["personas"][-1],
                              "question": f"Can you {never[0].lower() + never[1:]}?", "expected_chunk_ids": [],
                              "expected_behavior": "ABSTAIN", "difficulty": "CONFLICT"})
        steps = [{"step": 1, "action": "verify_identity", "tool": "POST /v1/identity/verify", "factors": info["verification"]},
                 {"step": 2, "action": "look_up", "tool": f"{uc} endpoint"},
                 {"step": 3, "action": "answer_with_citation", "tool": "GET /v1/knowledge/search"},
                 {"step": 4, "action": "escalate_if_triggered", "queue": info["handoff_queue"]}]
        sop_rows.append({"key": (6000 + n) * 100, "asset_id": sop_aid, "use_case_code": uc, "steps": json.dumps(steps),
                         "verification_required": info["verification"], "escalation_triggers": info["escalate"],
                         "handoff_queue": info["handoff_queue"], "forbidden_actions": info["never"]})

    def col(rows, k, typ=None):
        return pa.array([r[k] for r in rows], typ) if typ else pa.array([r[k] for r in rows])

    ls = pa.list_(pa.string())
    ctx.write("faq_entry", {
        "faq_id": pa.array(ctx.ids("faq_entry", [r["key"] for r in faq_rows]), pa.string()),
        "asset_id": col(faq_rows, "asset_id", pa.string()), "question_variants": col(faq_rows, "question_variants", ls),
        "answer": col(faq_rows, "answer", pa.string()), "persona_tags": col(faq_rows, "persona_tags", ls),
        "use_case_code": col(faq_rows, "use_case_code", pa.string()),
    })
    ctx.write("agent_sop", {
        "sop_id": pa.array(ctx.ids("agent_sop", [r["key"] for r in sop_rows]), pa.string()),
        "asset_id": col(sop_rows, "asset_id", pa.string()), "use_case_code": col(sop_rows, "use_case_code", pa.string()),
        "steps": col(sop_rows, "steps", pa.string()), "verification_required": col(sop_rows, "verification_required", ls),
        "escalation_triggers": col(sop_rows, "escalation_triggers", ls), "handoff_queue": col(sop_rows, "handoff_queue", pa.string()),
        "forbidden_actions": col(sop_rows, "forbidden_actions", ls),
    })
    link_rows = [{"key": s.key * 10 + j, "asset_id": aid, "linked_entity": e, "linked_key": k}
                 for s, aid in zip(specs, a_ids) for j, (e, k) in enumerate(s.links)]
    ctx.write("asset_link", {
        "link_id": pa.array(ctx.ids("asset_link", [r["key"] for r in link_rows]), pa.string()),
        "asset_id": col(link_rows, "asset_id", pa.string()), "linked_entity": col(link_rows, "linked_entity", pa.string()),
        "linked_key": col(link_rows, "linked_key", pa.string()),
    })
    ctx.write("qa_golden", {
        "qa_id": pa.array(ctx.ids("qa_golden", [r["key"] for r in gold_rows]), pa.string()),
        "use_case_code": col(gold_rows, "use_case_code", pa.string()), "persona": col(gold_rows, "persona", pa.string()),
        "question": col(gold_rows, "question", pa.string()), "expected_chunk_ids": col(gold_rows, "expected_chunk_ids", ls),
        "expected_behavior": col(gold_rows, "expected_behavior", pa.string()), "difficulty": col(gold_rows, "difficulty", pa.string()),
    })


def run(ctx: GenContext) -> tuple[list[str], dict]:
    if ctx.wants("care_case") or ctx.wants("transition_of_care"):
        gen_care(ctx)
    extra = gen_knowledge(ctx) if ctx.wants("knowledge_asset") else {}
    return [e for e in catalog.STAGE_ENTITIES["s6"] if ctx.wants(e)], extra
