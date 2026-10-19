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
from factory.data_engine.keys import claim_key_from_number
from factory.data_engine.knowledge_catalog import USE_CASE_ORDER, AssetSpec, asset_specs

DAY = np.timedelta64(1, "D")
PROMPT_VERSION = "kb-doc-v1"
SYSTEM = (
    "You write governed health-plan documents for a synthetic test environment. Use only the facts given. "
    "Do not invent names, phone numbers, dollar amounts or codes. Plain language, short paragraphs, no markdown tables."
)


# ------------------------------------------------------------------ care management
def gen_care(ctx: GenContext) -> None:
    """Transition-of-care records and TOC care cases, one per inpatient facility claim (bill type 0111) - rule R-10.

    The call is due 48 hours after discharge and the follow-up visit within 7 days (T-06); readmit_30d is derived
    from the member's other admissions, never drawn.
    """
    t = ctx.stores.tables.query(
        """SELECT claim_id, claim_number, member_id, facility_npi, service_from_dt, service_to_dt
           FROM claim_header WHERE bill_type = '0111' ORDER BY claim_number"""
    )
    rows = t.to_pylist()
    keys = np.array([claim_key_from_number(r["claim_number"]) for r in rows], dtype=np.int64)  # same key as the claim
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
    """Content-addressed cache key: the same model, prompt version, prompt and seed always reuse the same text."""
    return hashlib.sha256(f"{model}|{PROMPT_VERSION}|{prompt}|{seed}".encode()).hexdigest()


def generate_text(ctx: GenContext, title: str, kind: str, audience: str, facts: list[str], stats: dict) -> tuple[str, str]:
    """Text for one document from the LLM, through the cache. The prompt contains only the given facts.

    Returns (text, cache key); stats counts generated vs cached texts for the stage result.
    """
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
    """Split a document into retrieval chunks of whole paragraphs, at most max_words words each."""
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
    """Knowledge assets for the run: documents (LLM text), chunks, embeddings in the vector index, then
    FAQs, SOPs, links and golden QA. Asset ids are deterministic (see knowledge_catalog), so s5 can already cite
    PA policies. Returns stage-result extras: LLM cache statistics and the number of indexed chunks.
    """
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
    vectors = ctx.stores.llm.embed(c_text, task="document")
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
    """Derived knowledge rows: FAQ entries (answers through the LLM), agent SOPs from the use-case catalog,
    asset links (plan, CPT, denial code, use case; rule R-11) and the golden QA set (answer, escalate, abstain).
    """
    by_key = {s.key: (s, aid) for s, aid in zip(specs, a_ids)}
    manual = next(s for s in specs if s.asset_type == "PROVIDER_MANUAL")

    faq_rows, sop_rows, gold_rows = [], [], []
    for uc in ctx.spec.use_cases:
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


# ------------------------------------------------------------------ care management programs
COHORTS = [("High-risk CHF", "HIGH", "WEEKLY"), ("Rising-risk diabetes", "RISING", "MONTHLY"), ("High-risk COPD", "HIGH", "WEEKLY"),
           ("Frequent ED users", "HIGH", "DAILY"), ("Post-discharge 30 days", "HIGH", "DAILY"), ("Rising-risk hypertension", "RISING", "MONTHLY"),
           ("Maternity", "RISING", "WEEKLY"), ("Behavioral health engaged", "RISING", "MONTHLY"), ("CKD stage 3-4", "HIGH", "MONTHLY"),
           ("Polypharmacy 10+", "RISING", "MONTHLY"), ("Gaps in care", "LOW", "MONTHLY"), ("Healthy adults", "LOW", "MONTHLY")]
CAMPAIGN_PURPOSES = ["GAP_CLOSURE", "TOC", "WELLNESS", "RENEWAL", "ADHERENCE"]
PROGRAMS = ["DIABETES", "CHF", "COPD", "ASTHMA", "HYPERTENSION", "CKD"]


def gen_cohorts_and_campaigns(ctx: GenContext) -> tuple[list[str], list[str]]:
    cohort_ids = ctx.ids("risk_cohort", np.arange(len(COHORTS)))
    if ctx.wants("risk_cohort"):
        ctx.write("risk_cohort", {
            "cohort_id": pa.array(cohort_ids),
            "cohort_name": pa.array([c[0] for c in COHORTS]),
            "criteria": pa.array([json.dumps({"risk_tier": c[1]}) for c in COHORTS]),
            "refresh_frequency": pa.array([c[2] for c in COHORTS]),
        })
    n = 20
    keys = np.arange(n)
    rr = ctx.rr("outreach_campaign", keys)
    campaign_ids = ctx.ids("outreach_campaign", keys)
    purpose = [CAMPAIGN_PURPOSES[i % len(CAMPAIGN_PURPOSES)] for i in keys]
    start = ctx.as_of - rr.integers("start", 30, 300).astype("timedelta64[D]")
    if ctx.wants("outreach_campaign"):
        ctx.write("outreach_campaign", {
            "campaign_id": pa.array(campaign_ids),
            "name": pa.array([f"{pu.replace('_', ' ').title()} wave {i // len(CAMPAIGN_PURPOSES) + 1}" for i, pu in zip(keys, purpose)]),
            "purpose": pa.array(purpose),
            "channel": pa.array(rr.choice("channel", ["VOICE", "CHAT", "SMS"], [40, 20, 40]).tolist()),
            "target_cohort_id": pa.array([cohort_ids[i % len(cohort_ids)] for i in keys]),
            "start_dt": dates(start),
            "end_dt": dates(start + 90 * DAY),
            "script_asset_id": pa.array([None] * n, pa.string()),
        })
    return cohort_ids, campaign_ids


def gen_programs_and_outreach(ctx: GenContext, campaign_ids: list[str]) -> None:
    """Disease-program enrollments (12% of members) and outreach contact history.

    Outreach without a consent basis is SUPPRESSED (C-07) and every AI contact discloses that it is AI (A-04).
    """
    members = ctx.stores.tables.query("SELECT member_id, member_number FROM member_demographics ORDER BY member_number")
    mids = np.array(members["member_id"].to_pylist(), dtype=object)
    midx = np.array([int(x[4:]) - 1 for x in members["member_number"].to_pylist()], dtype=np.int64)
    rr = ctx.rr("care_programs:member", midx)
    if ctx.wants("disease_program_enrollment"):
        sel = np.where(rr.bernoulli("program", 0.12))[0]
        prr = ctx.rr("disease_program_enrollment", midx[sel])
        status = prr.choice("status", ["IDENTIFIED", "ENROLLED", "ENGAGED", "DECLINED", "GRADUATED"], [15, 30, 35, 10, 10])
        enrolled = ctx.as_of - prr.integers("enrolled", 30, 700).astype("timedelta64[D]")
        ctx.write("disease_program_enrollment", {
            "enrollment_id": pa.array(ctx.ids("disease_program_enrollment", midx[sel])),
            "member_id": pa.array(mids[sel].tolist()),
            "program": pa.array(prr.choice("prog", PROGRAMS).tolist()),
            "status": pa.array(status.tolist()),
            "enrolled_dt": dates(np.where(status == "IDENTIFIED", np.datetime64("NaT", "D"), enrolled)),
            "graduated_dt": dates(np.where(status == "GRADUATED", enrolled + 180 * DAY, np.datetime64("NaT", "D"))),
            "engagement_level": pa.array(prr.choice("engagement", ["LOW", "MEDIUM", "HIGH"]).tolist()),
        })
    if ctx.wants("outreach_history"):
        k = np.minimum(rr.poisson("contacts", 0.25), 5)
        oi = np.repeat(np.arange(len(midx)), k)
        oj = np.concatenate([np.arange(x) for x in k]) if len(oi) else np.array([], np.int64)
        keys = midx[oi] * 8 + oj
        orr = ctx.rr("outreach_history", keys)
        consent = orr.choice("consent", ["TREATMENT_EXEMPT", "EXPRESS_CONSENT", "NONE"], [55, 40, 5])
        outcome = np.where(consent == "NONE", "SUPPRESSED",  # C-07: no consent, no AI contact
                           orr.choice("outcome", ["REACHED", "VOICEMAIL", "NO_ANSWER", "REFUSED", "WRONG_NUMBER"], [40, 25, 25, 7, 3]))
        ctx.write("outreach_history", {
            "outreach_id": pa.array(ctx.ids("outreach_history", keys)),
            "campaign_id": pa.array([campaign_ids[i] for i in orr.integers("campaign", 0, len(campaign_ids))]),
            "member_id": pa.array(mids[oi].tolist()),
            "channel": pa.array(orr.choice("channel", ["VOICE", "CHAT", "SMS"]).tolist()),
            "attempt_dt": stamps(ctx.as_of.astype("datetime64[s]") - orr.integers("ago", 3600, 200 * 86400).astype("timedelta64[s]")),
            "attempt_no": pa.array((oj + 1).astype(np.int32)),
            "outcome": pa.array(outcome.tolist()),
            "consent_basis": pa.array(consent.tolist()),
            "ai_disclosed": pa.array(np.ones(len(keys), bool)),  # A-04: AI disclosure on every AI contact
            "follow_up_required": pa.array(orr.bernoulli("follow_up", 0.2) & (outcome == "REACHED")),
        })


def gen_care_plans(ctx: GenContext) -> None:
    """Care plans for 80% of care cases: a goal, a measure and its baseline, target and current value."""
    if not ctx.stores.tables.exists("care_case"):
        return
    cases = ctx.stores.tables.query("SELECT case_id, member_id, opened_dt FROM care_case ORDER BY case_id")
    n = cases.num_rows
    keys = np.arange(n)
    rr = ctx.rr("care_plan", np.array([int(x.replace("-", "")[:12], 16) for x in cases["case_id"].to_pylist()], dtype=np.int64))
    has = rr.bernoulli("has_plan", 0.8)
    measure = rr.choice("measure", ["A1C", "BP", "WEIGHT", "PHQ9"])
    baseline = np.select([measure == "A1C", measure == "BP", measure == "WEIGHT"], [9.2, 152.0, 230.0], 16.0)
    target = np.select([measure == "A1C", measure == "BP", measure == "WEIGHT"], [8.0, 130.0, 210.0], 9.0)
    current = np.round(baseline + (target - baseline) * rr.u("progress"), 1)
    goal = {"A1C": "A1c under 8 in 90 days", "BP": "Systolic BP under 130", "WEIGHT": "Lose 20 lb in 6 months", "PHQ9": "PHQ-9 under 10"}
    opened = np.array([np.datetime64(d, "D") for d in cases["opened_dt"].to_pylist()], dtype="datetime64[D]")
    sel = np.where(has)[0]
    ctx.write("care_plan", {
        "care_plan_id": pa.array(ctx.ids("care_plan", keys[sel])),
        "case_id": pa.array(np.array(cases["case_id"].to_pylist(), dtype=object)[sel].tolist()),
        "member_id": pa.array(np.array(cases["member_id"].to_pylist(), dtype=object)[sel].tolist()),
        "goal": pa.array([goal[m] for m in measure[sel]]),
        "measure": pa.array(measure[sel].tolist()),
        "baseline": pa.array(baseline[sel]),
        "target": pa.array(target[sel]),
        "current": pa.array(current[sel]),
        "interventions": pa.array([["education", "med review"] if m in ("A1C", "BP") else ["referral"] for m in measure[sel]],
                                  pa.list_(pa.string())),
        "next_review_dt": dates(np.maximum(opened[sel] + 30 * DAY, ctx.as_of + 7 * DAY)),
    })


def run(ctx: GenContext) -> tuple[list[str], dict]:
    if ctx.wants("care_case") or ctx.wants("transition_of_care"):
        gen_care(ctx)
    if ctx.wants("care_plan"):
        gen_care_plans(ctx)
    if any(ctx.wants(e) for e in ("risk_cohort", "outreach_campaign", "outreach_history", "disease_program_enrollment")):
        _, campaign_ids = gen_cohorts_and_campaigns(ctx)
        gen_programs_and_outreach(ctx, campaign_ids)
    extra = gen_knowledge(ctx) if ctx.wants("knowledge_asset") else {}
    return [e for e in catalog.STAGE_ENTITIES["s6"] if ctx.wants(e)], extra
