"""Appeals, reprocessing, and every claim entity written from a ClaimBatch.

write_claims plans appeals and reprocessing first (both may reset a scenario tag that cannot fit before as_of),
builds the replacement claims, then calls one writer per entity. Reprocessed claims keep their original row
(status ADJUSTED or REPROCESSED) and get a replacement claim (frequency code 7, key = original + 32) with its own
lines, adjustments and payment, plus a reprocessing_history row (R-07, F-06). Only entities listed in the payloads
are written (ctx.wants).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from factory.codes import AppealOutcome, AppealStatus, ClaimStatus, ClaimType, DenialCategory, Lob, NetworkStatus
from factory.data_engine import catalog, keys
from factory.data_engine import scenarios as recipes
from factory.data_engine.context import GenContext, dates, stamps
from factory.data_engine.rng import money
from factory.data_engine.stages.s5.claims import ClaimBatch
from factory.data_engine.stages.s5.inputs import DAY, DRUGS_NO_PA, NAT, Inputs

APPEAL_SCENARIOS = recipes.ids(lambda s: s.appeal)  # claims that must carry a specific appeal


# ------------------------------------------------------------------ planners
@dataclass(eq=False)
class Appeals:
    """Appeals that fit before as_of; `pos` are claim positions in the batch."""

    pos: np.ndarray
    keys: np.ndarray  # claim keys (appeal ids and numbers derive from them)
    filed: np.ndarray
    deadline: np.ndarray
    status: np.ndarray
    outcome: np.ndarray
    decision: np.ndarray


@dataclass(eq=False)
class Reprocessing:
    """Claims re-adjudicated into a replacement claim; `pos` are claim positions in the batch."""

    pos: np.ndarray
    when: np.ndarray
    trigger: np.ndarray  # APPEAL_OVERTURN | PROVIDER_CORRECTION


def plan_appeals(ctx: GenContext, c: ClaimBatch) -> Appeals:
    """Appeals on denied claims: 12% baseline plus the PAP scenarios."""
    status, scen, cat = c.status, c.scen, c.denial_cat
    lob = c.m["lob"][c.ci]
    deadline_days = np.where(lob == Lob.COMMERCIAL, 180, 60)
    appealable = (status == ClaimStatus.DENIED) & (cat != DenialCategory.DUPLICATE) & (cat != DenialCategory.AUTH)
    # claims of other forced recipes, and fresh-appeal claims, never get a baseline appeal
    other_scenario = recipes.tagged(scen, lambda s: ((s.anchor == "claim" and s.forced) or s.fresh_appeal) and not s.appeal)
    pick = appealable & ((c.crr.bernoulli("appeal", 0.12) & ~other_scenario) | np.isin(scen, APPEAL_SCENARIOS))
    ap = np.where(pick)[0]
    arr = ctx.rr("appeal_record", c.ckey[ap])
    adjud = c.adjud[ap]
    deadline = adjud + deadline_days[ap].astype("timedelta64[D]")
    sc = scen[ap]
    filed = adjud + arr.integers("file_lag", 3, 26).astype("timedelta64[D]")
    late = recipes.tagged(sc, lambda s: s.appeal and s.appeal.late_filing)
    filed = np.where(late, deadline + arr.integers("late", 3, 21).astype("timedelta64[D]"), filed)
    st = arr.choice("status", [AppealStatus.OPEN, AppealStatus.IN_REVIEW, AppealStatus.DECIDED], [40, 40, 20]).astype(object)
    outcome = np.where(st == AppealStatus.DECIDED, arr.choice("outcome", [AppealOutcome.UPHELD, AppealOutcome.OVERTURNED], [60, 40]), None).astype(object)
    for s in recipes.having("appeal"):
        m = sc == s.id
        st[m], outcome[m] = s.appeal.status, s.appeal.outcome
    decision = np.where(st == AppealStatus.DECIDED, filed + arr.integers("dec_lag", 5, 30).astype("timedelta64[D]"), NAT)
    ok = filed <= ctx.as_of
    ok &= np.isnat(decision) | (decision <= ctx.as_of)
    # a scenario that cannot fit before the as-of date falls back to a baseline denial
    c.scen[ap[~ok & np.isin(sc, APPEAL_SCENARIOS)]] = recipes.BASE
    keep = np.where(ok)[0]
    return Appeals(pos=ap[keep], keys=c.ckey[ap[keep]], filed=filed[keep], deadline=deadline[keep],
                   status=st[keep], outcome=outcome[keep], decision=decision[keep])


def plan_reprocessing(ctx: GenContext, c: ClaimBatch, appeals: Appeals) -> Reprocessing:
    """Overturned appeals (APPEAL_OVERTURN) and recipe corrections of paid claims (MCI-04: PROVIDER_CORRECTION).
    Appeals whose recipe is awaiting reprocessing (PAP-03) get no replacement yet."""
    scen = c.scen
    over = appeals.outcome == AppealOutcome.OVERTURNED
    a_pos, a_dec = appeals.pos[over], appeals.decision[over]
    awaiting = recipes.tagged(scen[a_pos], lambda s: s.appeal and s.appeal.awaiting_reprocessing)
    a_pos, a_dec = a_pos[~awaiting], a_dec[~awaiting]
    a_dt = a_dec + 2 * DAY
    m_pos = np.where(recipes.tagged(scen, lambda s: s.reprocess == "PROVIDER_CORRECTION") & (c.status == ClaimStatus.PAID))[0]
    m_dt = c.paid_dt[m_pos] + 3 * DAY
    pos = np.concatenate([a_pos, m_pos]).astype(np.int64)
    when = np.concatenate([a_dt, m_dt]).astype("datetime64[D]")
    trigger = np.array(["APPEAL_OVERTURN"] * len(a_pos) + ["PROVIDER_CORRECTION"] * len(m_pos), dtype=object)
    ok = when < ctx.as_of
    c.scen[pos[~ok & (trigger == "PROVIDER_CORRECTION")]] = recipes.BASE
    return Reprocessing(pos=pos[ok], when=when[ok], trigger=trigger[ok])


@dataclass(eq=False)
class Replacements:
    """Replacement claims (one per reprocessed claim) and their lines, plus the ids of originals and replacements.

    Replacement lines copy the original lines; they are fully allowed and carry coinsurance only.
    """

    r: np.ndarray  # positions of the reprocessed originals
    r_lines: np.ndarray  # line positions of those originals
    r_li: np.ndarray  # replacement claim index per replacement line
    r_allowed: np.ndarray
    r_coins: np.ndarray
    r_paid: np.ndarray
    r_ckey: np.ndarray
    r_lkey: np.ndarray
    r_paid_dt: np.ndarray
    claim_ids: np.ndarray  # originals
    line_ids: np.ndarray
    r_claim_ids: np.ndarray
    r_line_ids: np.ndarray

    @property
    def nr(self) -> int:
        return len(self.r)

    def total(self, x: np.ndarray) -> np.ndarray:
        """Per replacement claim: sum of a replacement-line array."""
        return np.bincount(self.r_li, weights=x, minlength=self.nr).astype(np.int64)


def build_replacements(ctx: GenContext, c: ClaimBatch, rp: Reprocessing) -> Replacements:
    r = rp.pos
    r_lines = np.where(np.isin(c.li, r))[0]
    r_map = {int(p): k for k, p in enumerate(r)}
    r_li = np.array([r_map[int(x)] for x in c.li[r_lines]], dtype=np.int64)
    r_allowed = c.allowed_full[r_lines]
    r_coins = (r_allowed * c.coins_pct[c.li[r_lines]]) // 100
    r_ckey = keys.replacement_key(c.ckey[r])
    r_lkey = keys.line_key(r_ckey[r_li], c.ln[r_lines])
    return Replacements(
        r=r, r_lines=r_lines, r_li=r_li, r_allowed=r_allowed, r_coins=r_coins, r_paid=r_allowed - r_coins,
        r_ckey=r_ckey, r_lkey=r_lkey, r_paid_dt=np.minimum(rp.when + DAY, ctx.as_of),
        claim_ids=np.asarray(ctx.ids("claim_header", c.ckey), dtype=object),
        line_ids=np.asarray(ctx.ids("claim_line", c.lkey), dtype=object),
        r_claim_ids=np.asarray(ctx.ids("claim_header", r_ckey), dtype=object),
        r_line_ids=np.asarray(ctx.ids("claim_line", r_lkey), dtype=object),
    )


# ------------------------------------------------------------------ entry point
def write_claims(ctx: GenContext, inp: Inputs, c: ClaimBatch, claim_auth: np.ndarray) -> None:
    """Write every claim entity for a shard, adding appeals, reprocessing and accumulators."""
    appeals = plan_appeals(ctx, c)
    rp = plan_reprocessing(ctx, c, appeals)  # c.scen may now have tags reset to BASE

    status = c.status.copy()  # final status of the originals
    status[rp.pos[rp.trigger == "APPEAL_OVERTURN"]] = ClaimStatus.REPROCESSED
    status[rp.pos[rp.trigger == "PROVIDER_CORRECTION"]] = ClaimStatus.ADJUSTED
    rep = build_replacements(ctx, c, rp)

    if ctx.wants("claim_header"):
        _write_headers(ctx, c, rp, rep, status, claim_auth)
    if ctx.wants("claim_line"):
        _write_lines(ctx, c, rep)
    if ctx.wants("pharmacy_claim_detail"):
        _write_pharmacy(ctx, c, rep)
    if ctx.wants("claim_denial"):
        _write_denials(ctx, c, rep)
    if ctx.wants("claim_pend"):
        _write_pends(ctx, c, rep)
    if ctx.wants("claim_adjustment"):
        _write_adjustments(ctx, c, rp, rep)
    if ctx.wants("claim_payment"):
        _write_payments(ctx, inp, c, rep)
    if ctx.wants("appeal_record"):
        _write_appeals(ctx, c, appeals, rep)
    if ctx.wants("reprocessing_history"):
        _write_reprocessing(ctx, c, rp, rep)
    if ctx.wants("member_accumulator"):
        _write_accumulators(ctx, c, rep, status)


# ------------------------------------------------------------------ one writer per entity
def _both(orig: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Originals followed by the reprocessed originals' values (copied onto their replacements)."""
    return np.concatenate([orig, orig[r]])


def _write_headers(ctx: GenContext, c: ClaimBatch, rp: Reprocessing, rep: Replacements, status: np.ndarray,
                   claim_auth: np.ndarray) -> None:
    r, nr, crr, ctype = rep.r, rep.nr, c.crr, c.ctype
    member_ids = c.m["member_id"][c.ci]
    bill_type = np.where(ctype == ClaimType.FACILITY, np.where(c.inpatient, "0111", "0131"), None)
    drg = np.where(c.inpatient, crr.choice("drg", ["470", "291", "193", "871"]), None)
    dx = crr.choice("dx", catalog.ICD10)
    ctx.write("claim_header", {
        "claim_id": pa.array(np.concatenate([rep.claim_ids, rep.r_claim_ids]).tolist()),
        "claim_number": pa.array(keys.claim_numbers(c.ckey) + keys.claim_numbers(rep.r_ckey)),
        "claim_type": pa.array(_both(ctype, r).tolist()),
        "member_id": pa.array(_both(member_ids, r).tolist()),
        "plan_id": pa.array(_both(c.plan_ids, r).tolist()),
        "billing_npi": pa.array(_both(c.billing, r).tolist()),
        "rendering_npi": pa.array(_both(c.rendering, r).tolist(), pa.string()),
        "facility_npi": pa.array(_both(c.facility, r).tolist(), pa.string()),
        "claim_frequency_code": pa.array(["1"] * len(rep.claim_ids) + ["7"] * nr),
        "original_claim_id": pa.array([None] * len(rep.claim_ids) + rep.claim_ids[r].tolist(), pa.string()),
        "service_from_dt": dates(_both(c.s_from, r)),
        "service_to_dt": dates(_both(c.s_to, r)),
        "received_dt": dates(np.concatenate([c.received, rp.when])),
        "adjudicated_dt": dates(np.concatenate([c.adjud, rp.when])),
        "paid_dt": dates(np.concatenate([c.paid_dt, rep.r_paid_dt])),
        "status": pa.array(np.concatenate([status, np.full(nr, ClaimStatus.PAID, dtype=object)]).tolist()),
        "network_status": pa.array(_both(c.network, r).tolist()),
        "is_emergency": pa.array(_both(c.emergency, r)),
        "auth_id": pa.array(_both(claim_auth, r).tolist(), pa.string()),
        "bill_type": pa.array(_both(bill_type, r).tolist(), pa.string()),
        "drg_code": pa.array(_both(drg, r).tolist(), pa.string()),
        "primary_dx": pa.array(_both(dx, r).tolist()),
        "total_billed": money(_both(c.tot_billed, r)),
        "total_allowed": money(np.concatenate([c.tot_allowed, rep.total(rep.r_allowed)])),
        "total_paid": money(np.concatenate([c.tot_paid, rep.total(rep.r_paid)])),
        "member_resp": money(np.concatenate([c.member_resp, rep.total(rep.r_coins)])),
    }, scenario=_both(c.scen, r))


def _write_lines(ctx: GenContext, c: ClaimBatch, rep: Replacements) -> None:
    li, ln, rl = c.li, c.ln, rep.r_lines
    zeros = np.zeros(len(rl), np.int64)
    ctx.write("claim_line", {
        "line_id": pa.array(np.concatenate([rep.line_ids, rep.r_line_ids]).tolist()),
        "claim_id": pa.array(np.concatenate([rep.claim_ids[li], rep.r_claim_ids[rep.r_li]]).tolist()),
        "line_no": pa.array((_both(ln, rl) + 1).astype(np.int32)),
        "cpt_hcpcs": pa.array(_both(c.cpt, rl).tolist(), pa.string()),
        "revenue_code": pa.array(np.where(_both(c.ctype[li], rl) == ClaimType.FACILITY, "0120", None).tolist(), pa.string()),
        "modifier": pa.array([None] * (len(li) + len(rl)), pa.string()),
        "units": pa.array(_both(c.units, rl)),
        "billed": money(_both(c.billed, rl)),
        "allowed": money(np.concatenate([c.allowed, rep.r_allowed])),
        "paid": money(np.concatenate([c.paid, rep.r_paid])),
        "deductible": money(np.concatenate([c.ded, zeros])),
        "copay": money(np.concatenate([c.copay, zeros])),
        "coinsurance": money(np.concatenate([c.coins, rep.r_coins])),
        "line_status": pa.array(np.concatenate([c.line_status, np.full(len(rl), ClaimStatus.PAID, dtype=object)]).tolist()),
    }, scenario=_both(c.scen[li], rl))


def _write_pharmacy(ctx: GenContext, c: ClaimBatch, rep: Replacements) -> None:
    """R-08: exactly one detail row per pharmacy claim."""
    ph = c.ph
    drugs = [DRUGS_NO_PA[i] for i in c.drug_i[ph]]
    days = c.crr.choice("days_supply", [30, 90], [80, 20])[ph].astype(np.int64)
    ctx.write("pharmacy_claim_detail", {
        "claim_id": pa.array(rep.claim_ids[ph].tolist()),
        "ndc": pa.array([d[0] for d in drugs]),
        "drug_name": pa.array([d[1] for d in drugs]),
        "quantity": pa.array(days),
        "days_supply": pa.array(days),
        "pharmacy_npi": pa.array(c.billing[ph].tolist()),
        "fill_dt": dates(c.s_from[ph]),
        "formulary_tier": pa.array(c.tier[ph].astype(np.int32)),
        "pa_required": pa.array([d[3] for d in drugs], pa.bool_()),
        "copay_applied": money(np.array([catalog.TIER_COPAY_USD[t] * 100 for t in c.tier[ph]], dtype=np.int64)),
        "reject_code": pa.array([None] * len(ph), pa.string()),
    }, scenario=c.scen[ph])


def _write_denials(ctx: GenContext, c: ClaimBatch, rep: Replacements) -> None:
    """Claim-level denials anchored on line 1; they stay on reprocessed originals as history."""
    dn = np.where(c.status == ClaimStatus.DENIED)[0]
    first_line = {int(ci): lid for ci, lid, l in zip(c.li, rep.line_ids, c.ln) if l == 0}
    cat = c.denial_cat[dn]
    lob = c.m["lob"][c.ci[dn]]
    appealable = (cat != DenialCategory.DUPLICATE) & (cat != DenialCategory.AUTH)
    deadline = c.adjud[dn] + np.where(lob == Lob.COMMERCIAL, 180, 60).astype("timedelta64[D]")
    ctx.write("claim_denial", {
        "denial_id": pa.array(ctx.ids("claim_denial", c.ckey[dn])),
        "claim_id": pa.array(rep.claim_ids[dn].tolist()),
        "line_id": pa.array([first_line[int(i)] for i in dn], pa.string()),
        "carc": pa.array([f"CO-{catalog.DENIAL_CATEGORIES[x][0]}" for x in cat], pa.string()),
        "rarc": pa.array([None] * len(dn), pa.string()),
        "denial_category": pa.array(cat.tolist(), pa.string()),
        "reason_text": pa.array([catalog.DENIAL_CATEGORIES[x][1] for x in cat], pa.string()),
        "liable_party": pa.array(["PROVIDER"] * len(dn), pa.string()),
        "denial_dt": dates(c.adjud[dn]),
        "appealable": pa.array(appealable, pa.bool_()),
        "appeal_deadline_dt": dates(np.where(appealable, deadline, NAT)),
    }, scenario=c.scen[dn])


def _write_pends(ctx: GenContext, c: ClaimBatch, rep: Replacements) -> None:
    pd_ = np.where(c.status == ClaimStatus.PENDED)[0]
    prr = ctx.rr("claim_pend", c.ckey[pd_])
    reason = prr.choice("reason", ["RECORDS_REQUEST", "COB_INFO", "CLINICAL_REVIEW", "PRICING"], [40, 25, 20, 15]).astype(object)
    forced_reason = c.pend_reason[pd_]
    reason = np.where(forced_reason != None, forced_reason, reason)  # noqa: E711
    pend_dt = np.minimum(c.received[pd_] + prr.integers("pend_lag", 0, 4).astype("timedelta64[D]"), ctx.as_of)
    ctx.write("claim_pend", {
        "pend_id": pa.array(ctx.ids("claim_pend", c.ckey[pd_]), pa.string()),
        "claim_id": pa.array(rep.claim_ids[pd_].tolist(), pa.string()),
        "pend_reason": pa.array(reason.tolist(), pa.string()),
        "pend_dt": dates(pend_dt),
        "expected_resolution_dt": dates(pend_dt + prr.integers("res", 10, 31).astype("timedelta64[D]")),
        "info_needed_from": pa.array(np.where(reason == "COB_INFO", "MEMBER", np.where(reason == "PRICING", "PAYER", "PROVIDER")).tolist(), pa.string()),
        "resolved_dt": dates(np.full(len(pd_), NAT)),
    }, scenario=c.scen[pd_])


def _write_adjustments(ctx: GenContext, c: ClaimBatch, rp: Reprocessing, rep: Replacements) -> None:
    """F-05: paid lines CO-45 / PR-1 / PR-2 / PR-3; denied lines CO-<carc> = billed; replacements CO-45 / PR-2."""
    li, scen = c.li, c.scen
    a_line, a_code, a_amt, a_key, a_date, a_scen = [], [], [], [], [], []

    def add(line_ids, code, amt, akey, adate, ascen) -> None:
        a_line.append(line_ids); a_code.append(code); a_amt.append(amt)
        a_key.append(akey); a_date.append(adate); a_scen.append(ascen)

    paid_l = c.line_status == ClaimStatus.PAID
    for slot, code, amt in ((0, "CO-45", c.billed - c.allowed), (1, "PR-1", c.ded), (2, "PR-2", c.coins), (3, "PR-3", c.copay)):
        sel = np.where(paid_l & (amt > 0))[0]
        add(rep.line_ids[sel], np.full(len(sel), code, dtype=object), amt[sel], keys.adjustment_key(c.lkey[sel], slot),
            c.adjud[li[sel]], scen[li[sel]])
    sel = np.where(c.line_status == ClaimStatus.DENIED)[0]
    add(rep.line_ids[sel], np.array([f"CO-{catalog.DENIAL_CATEGORIES[x][0]}" for x in c.denial_cat[li[sel]]], dtype=object),
        c.billed[sel], keys.adjustment_key(c.lkey[sel], keys.DENIAL_ADJUSTMENT_SLOT), c.adjud[li[sel]], scen[li[sel]])
    for slot, code, amt in ((0, "CO-45", c.billed[rep.r_lines] - rep.r_allowed), (2, "PR-2", rep.r_coins)):
        sel = np.where(amt > 0)[0]
        add(rep.r_line_ids[sel], np.full(len(sel), code, dtype=object), amt[sel], keys.adjustment_key(rep.r_lkey[sel], slot),
            rp.when[rep.r_li[sel]], scen[li[rep.r_lines[sel]]])
    akey = np.concatenate(a_key).astype(np.int64)
    order = np.argsort(akey, kind="stable")
    adj_codes = np.concatenate(a_code)[order] if akey.size else np.array([], dtype=object)
    ctx.write("claim_adjustment", {
        "adjustment_id": pa.array(ctx.ids("claim_adjustment", akey[order]), pa.string()),
        "line_id": pa.array(np.concatenate(a_line)[order].tolist(), pa.string()),
        "group_code": pa.array([x.split("-")[0] for x in adj_codes], pa.string()),
        "carc": pa.array(adj_codes.tolist(), pa.string()),
        "amount": money(np.concatenate(a_amt)[order]),
        "adjustment_dt": dates(np.concatenate(a_date)[order]),
    }, scenario=np.concatenate(a_scen)[order] if akey.size else "BASE")


def _write_payments(ctx: GenContext, inp: Inputs, c: ClaimBatch, rep: Replacements) -> None:
    """Originals paid at adjudication (adjusted ones too) plus replacement payments; PPI-03 recoupment, PPI-04 wrong TIN."""
    r, nr, scen = rep.r, rep.nr, c.scen
    py = np.where(c.status == ClaimStatus.PAID)[0]
    yrr = ctx.rr("claim_payment", c.ckey[py])
    payee = np.array([inp.tin[b] for b in c.billing[py]], dtype=object)
    wrong = recipes.tagged(scen[py], lambda s: s.payment == "WRONG_PAYEE")  # payee TIN differs from the billing group
    payee[wrong] = [f"9{(int(t[1:]) + 1234567) % 100000000:08d}" for t in payee[wrong]]
    recoup = recipes.tagged(scen[py], lambda s: s.payment == "RECOUPMENT")
    traces = np.array([f"TRN{int(k):012d}" for k in c.ckey[py]], dtype=object)
    r_traces = [f"TRN{int(k):012d}" for k in rep.r_ckey]
    ctx.write("claim_payment", {
        "payment_id": pa.array(ctx.ids("claim_payment", np.concatenate([c.ckey[py], rep.r_ckey])), pa.string()),
        "claim_id": pa.array(np.concatenate([rep.claim_ids[py], rep.r_claim_ids]).tolist(), pa.string()),
        "payment_dt": dates(np.concatenate([c.paid_dt[py], rep.r_paid_dt])),
        "method": pa.array(np.concatenate([yrr.choice("method", ["EFT", "CHECK", "VCARD"], [80, 15, 5]),
                                           np.full(nr, "EFT", dtype=object)]).tolist(), pa.string()),
        "paid_amount": money(np.concatenate([c.tot_paid[py], rep.total(rep.r_paid)])),
        "trace_no": pa.array(traces.tolist() + r_traces, pa.string()),
        "payee_tin": pa.array(payee.tolist() + [inp.tin[b] for b in c.billing[r]], pa.string()),
        "recoup_flag": pa.array(np.concatenate([recoup, np.zeros(nr, bool)])),
        "offset_ref": pa.array([f"RCP-{t[3:]}" if f else None for t, f in zip(traces, recoup)] + [None] * nr, pa.string()),
        "offset_amount": money(np.concatenate([np.where(recoup, (c.tot_paid[py] * 0.25).astype(np.int64), 0),
                                               np.zeros(nr, np.int64)])),
    }, scenario=np.concatenate([scen[py], scen[r]]))


def _write_appeals(ctx: GenContext, c: ClaimBatch, appeals: Appeals, rep: Replacements) -> None:
    pos = appeals.pos
    ctx.write("appeal_record", {
        "appeal_id": pa.array(ctx.ids("appeal_record", appeals.keys), pa.string()),
        "appeal_number": pa.array(keys.business_numbers("APL", appeals.keys), pa.string()),
        "claim_id": pa.array(rep.claim_ids[pos].tolist(), pa.string()),
        "filed_by": pa.array(["PROVIDER"] * len(pos), pa.string()),
        "level": pa.array(["L1"] * len(pos), pa.string()),
        "reason": pa.array([DenialCategory.MED_NECESSITY if x == DenialCategory.MED_NECESSITY else "OTHER" for x in c.denial_cat[pos]], pa.string()),
        "filed_dt": dates(appeals.filed),
        "deadline_dt": dates(appeals.deadline),
        "status": pa.array(appeals.status.tolist(), pa.string()),
        "outcome": pa.array(appeals.outcome.tolist(), pa.string()),
        "decision_dt": dates(appeals.decision),
        "documents_received": pa.array([["clinical_notes"] if st != AppealStatus.OPEN else [] for st in appeals.status], pa.list_(pa.string())),
        "late_filing_reason": pa.array(["GOOD_CAUSE_REQUESTED" if f > d else None for f, d in zip(appeals.filed, appeals.deadline)],
                                       pa.string()),
    }, scenario=c.scen[pos])


def _write_reprocessing(ctx: GenContext, c: ClaimBatch, rp: Reprocessing, rep: Replacements) -> None:
    """R-07, F-06: the trail from each reprocessed original to its replacement."""
    r = rep.r
    ctx.write("reprocessing_history", {
        "reprocess_id": pa.array(ctx.ids("reprocessing_history", c.ckey[r]), pa.string()),
        "claim_id": pa.array(rep.claim_ids[r].tolist(), pa.string()),
        "new_claim_id": pa.array(rep.r_claim_ids.tolist(), pa.string()),
        "trigger": pa.array(rp.trigger.tolist(), pa.string()),
        "reprocess_dt": stamps(rp.when.astype("datetime64[s]") + np.timedelta64(10, "h")),
        "paid_before": money(c.tot_paid[r]),
        "paid_after": money(rep.total(rep.r_paid)),
        "performed_by": pa.array(np.where(rp.trigger == "APPEAL_OVERTURN", "ANALYST", "SYSTEM").tolist(), pa.string()),
    }, scenario=c.scen[r])


def _write_accumulators(ctx: GenContext, c: ClaimBatch, rep: Replacements, status: np.ndarray) -> None:
    """F-03: member liability on claims that are PAID in their final version, per member and network."""
    li, rl = c.li, rep.r_lines
    final_paid_orig = (status == ClaimStatus.PAID)[li] & (c.line_status == ClaimStatus.PAID)
    pl = np.where(final_paid_orig)[0]
    mem = np.concatenate([c.midx[c.ci[li[pl]]], c.midx[c.ci[li[rl]]]])
    net = np.concatenate([c.network[li[pl]], c.network[li[rl]]])
    ded = np.concatenate([c.ded[pl], np.zeros(len(rl), np.int64)])
    oop = np.concatenate([(c.ded + c.copay + c.coins)[pl], rep.r_coins])
    gkey = keys.accumulator_key(mem, np.where(net == NetworkStatus.IN, 0, 1))
    uniq, inv = np.unique(gkey, return_inverse=True)
    member_of = {int(mx): mid for mx, mid in zip(c.midx, c.m["member_id"])}
    ctx.write("member_accumulator", {
        "accumulator_id": pa.array(ctx.ids("member_accumulator", uniq), pa.string()),
        "member_id": pa.array([member_of[int(k // keys.ACCUMULATOR_SLOTS)] for k in uniq], pa.string()),
        "plan_year": pa.array(np.full(len(uniq), int(str(ctx.as_of)[:4]))),
        "network": pa.array(np.where(uniq % keys.ACCUMULATOR_SLOTS == 0, NetworkStatus.IN, NetworkStatus.OON).tolist(), pa.string()),
        "deductible_met": money(np.bincount(inv, weights=ded, minlength=len(uniq)).astype(np.int64)),
        "oop_met": money(np.bincount(inv, weights=oop, minlength=len(uniq)).astype(np.int64)),
    })
