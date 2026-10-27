"""Capability agents (phase 2): an LLM, guided by the capability skill, writes the packs a build needs.

  planner           gaps + payload context -> a plan: which entity and use-case packs to write, assumptions,
                    confidence; or questions when a name cannot be read (the build becomes UNDERSPECIFIED)
  entity author     one entity pack per planned entity (YAML)
  use-case author   one use-case pack per planned use case (YAML), given the entities it reads
  repair loop       validation errors (G1) and failed gates (G2-G5) go back to the author with the details, up to
                    a budget of attempts; nothing stays active unless it passes every gate

Agents only produce data (packs). The engines interpret packs; no LLM output is ever executed. Every prompt and
response is kept in the build log for audit. The LLM is the profile's (Gemini on Vertex AI, or a local
OpenAI-compatible server such as LM Studio).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from factory.data_engine import catalog, packs

# ------------------------------------------------------------------ the skill
PLANNER_REFS = ["references/domain/glossary.md", "references/domain/use-cases.md", "references/domain/entities.md"]
ENTITY_REFS = ["references/generators.md", "references/data-conventions.md", "references/domain/entities.md",
               "references/domain/codes.md", "references/domain/standards.md", "examples/sdoh_indicator.yaml",
               "examples/mbp/premium_invoice.yaml"]
USE_CASE_REFS = ["references/api-conventions.md", "references/domain/glossary.md", "references/domain/use-cases.md",
                 "examples/mbp/uc_mbp.yaml"]


class Skill:
    """The capability skill folder: SKILL.md plus references, loaded per agent."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        if not (self.root / "SKILL.md").exists():
            raise FileNotFoundError(f"capability skill not found at {self.root} (set FACTORY_SKILL_DIR)")
        self.version = re.search(r"^version: (.+)$", self.read("SKILL.md"), re.M).group(1).strip()

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def context(self, refs: list[str]) -> str:
        parts = [self.read("SKILL.md")]
        for rel in refs:
            body = self.read(rel)
            fence = "yaml" if rel.endswith(".yaml") else ""
            parts.append(f"=== {rel} ===\n" + (f"```{fence}\n{body}\n```" if fence else body))
        return "\n\n".join(parts)


# ------------------------------------------------------------------ plan model
class PlannedEntity(BaseModel):
    name: str
    purpose: str
    rows: str = Field(description="e.g. 'many: one per month for 6 months' or 'zero_or_one: one per screened member'")
    columns_hint: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class PlannedEndpoint(BaseModel):
    path: str
    entity: str
    kind: Literal["list", "item"] = "list"


class PlannedUseCase(BaseModel):
    code: str
    name: str
    purpose: str
    needs: list[str]
    endpoints: list[PlannedEndpoint]
    caregiver_scope: str = "BENEFITS"
    assumptions: list[str] = Field(default_factory=list)


class Unsupported(BaseModel):
    gap: str
    reason: str


class Plan(BaseModel):
    confidence: float = Field(ge=0, le=1)
    entities: list[PlannedEntity] = Field(default_factory=list)
    use_cases: list[PlannedUseCase] = Field(default_factory=list)
    unsupported: list[Unsupported] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ build log
@dataclass
class Step:
    agent: str
    attempt: int
    outcome: str  # ok | invalid | gates_failed | llm_error | underspecified
    detail: str = ""
    prompt: str = ""
    response: str = ""
    seconds: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {"agent": self.agent, "attempt": self.attempt, "outcome": self.outcome, "detail": self.detail[:600],
                "seconds": round(self.seconds, 1)}


@dataclass
class AgentResult:
    status: str  # READY | UNDERSPECIFIED | FAILED
    steps: list[Step] = field(default_factory=list)
    published: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    error: str | None = None


def explain(detail: str) -> str:
    """Turn validation errors into instructions an author can act on (e.g. 'join is not a pack key here: remove it'),
    followed by the original message."""
    hints, lines = [], detail.splitlines()
    for loc, msg in zip(lines, lines[1:]):
        loc, msg = loc.strip(), msg.strip()
        if not loc or loc.startswith(("For further", "Value error")) or " " in loc:
            continue
        key = loc.split(".")[-1]
        where = ".".join(p for p in loc.split(".")[2:-1] if p)
        if "Extra inputs are not permitted" in msg:
            hints.append(f"- `{key}` is not a pack key at {where or 'the top level'}: remove it (the format has no such field).")
        elif msg.startswith("Field required"):
            hints.append(f"- `{key}` is required at {where or 'the top level'}: add it.")
        elif "does not match any of the expected tags" in msg or "Input tag" in msg:
            hints.append(f"- {loc}: unknown building block op; use one listed in generators.md.")
        elif msg:
            hints.append(f"- {loc}: {msg.split(' [type=')[0]}")
    return ("What to fix:\n" + "\n".join(dict.fromkeys(hints)) + "\n\nValidator output:\n" + detail) if hints else detail


def _extract(text: str, prefer: str) -> str:
    """The last fenced block (```yaml / ```json); an unclosed last fence (a truncated or sloppy answer) counts too;
    else the whole text."""
    blocks = re.findall(r"```(?:[a-zA-Z]+)?\s*\n(.*?)```", text, re.S)
    if blocks:
        return blocks[-1].strip()
    opened = re.search(r"```(?:[a-zA-Z]+)?\s*\n(.*)$", text, re.S)
    return opened.group(1).strip() if opened else text.strip()


def _load(text: str) -> Any:
    doc = yaml.safe_load(_extract(text, "yaml"))
    if not isinstance(doc, dict):
        raise ValueError("the answer must be one YAML (or JSON) mapping")
    return doc


def agent_builder(llm: Any, skill: Skill, publish: Callable[[dict], Any], max_attempts: int = 3):
    """A Builds builder: run the agents for a build's open gaps and record the outcome on the build."""
    from dataclasses import asdict

    def build_fn(build) -> None:
        open_gaps = [asdict(g) for g in (build.remaining or build.gaps)]
        res = CapabilityAgents(llm, skill, publish, max_attempts=max_attempts).run(open_gaps, build.context)
        build.steps += res.steps
        build.published += res.published
        build.questions, build.plan, build.error, build.agent_status = res.questions, res.plan, res.error, res.status

    return build_fn


# ------------------------------------------------------------------ the agents
class CapabilityAgents:
    """Runs planner and authors for one build. `publish(doc)` validates, gates and activates a pack (it raises
    PackRejected with gate details on failure)."""

    def __init__(self, llm: Any, skill: Skill, publish: Callable[[dict], Any], max_attempts: int = 3,
                 min_confidence: float = 0.5) -> None:
        self.llm, self.skill, self.publish = llm, skill, publish
        self.max_attempts, self.min_confidence = max_attempts, min_confidence

    # -------------------------------------------------------------- LLM call with logging
    def _ask(self, result: AgentResult, agent: str, attempt: int, system: str, prompt: str) -> tuple[str, Step]:
        t0 = time.perf_counter()
        step = Step(agent, attempt, "ok", prompt=prompt)
        try:
            step.response = self.llm.generate(prompt, system=system)
        except Exception as exc:  # the server is down, timed out, refused
            step.outcome, step.detail = "llm_error", repr(exc)[:600]
        step.seconds = time.perf_counter() - t0
        result.steps.append(step)
        return step.response, step

    # -------------------------------------------------------------- planner
    def plan(self, result: AgentResult, gaps: list[dict], context: dict[str, Any]) -> Plan | None:
        system = ("You are the planning agent of a healthcare synthetic-data factory. Read the gaps and decide which "
                  "capability packs to write. Answer with one JSON object only.\n\n" + self.skill.context(PLANNER_REFS))
        prompt = (
            "Gaps the engines cannot serve yet (from the stage payloads):\n" + json.dumps(gaps, indent=1) +
            "\n\nPayload context:\n" + json.dumps(context, indent=1) +
            "\n\nActive capability packs: " + json.dumps([p.ref for p in packs.active()]) +
            "\n\nReturn JSON with keys: confidence (0-1, how sure you are of reading every name), entities (one per new "
            "entity: name, purpose, rows, columns_hint, assumptions), use_cases (one per new use case: code, name, "
            "purpose, needs (tables read), endpoints [{path without /v1, entity, kind list|item}], caregiver_scope, "
            "assumptions), unsupported (gaps phase 1 packs cannot express, with reason), questions (only if a name "
            "cannot be read). Entity gaps must each appear in entities. Endpoints use exactly the payload's paths. "
            "Reuse core tables where they already hold the data (they need a member_id column for member endpoints)."
        )
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            text, step = self._ask(result, "planner", attempt, system, prompt + (f"\n\nYour previous answer was invalid: {last_error}" if last_error else ""))
            if step.outcome == "llm_error":
                return None
            try:
                plan = Plan.model_validate(_load(text))
            except (ValueError, ValidationError, yaml.YAMLError) as exc:
                step.outcome, step.detail, last_error = "invalid", str(exc)[:600], str(exc)[:600]
                continue
            step.detail = f"confidence {plan.confidence}; entities {[e.name for e in plan.entities]}; use cases {[u.code for u in plan.use_cases]}"
            return plan
        return None

    # -------------------------------------------------------------- authors with the repair loop
    def _author(self, result: AgentResult, agent: str, system: str, prompt: str, expected_id: str) -> bool:
        feedback = ""
        for attempt in range(1, self.max_attempts + 1):
            text, step = self._ask(result, agent, attempt, system, prompt + feedback)
            if step.outcome == "llm_error":
                return False
            try:
                doc = _load(text)
                if doc.get("id") != expected_id:
                    raise ValueError(f"the pack id must be {expected_id!r}, got {doc.get('id')!r}")
                pub = self.publish(doc)
            except Exception as exc:  # PackRejected (G1 or gates), YAML errors
                gates = getattr(exc, "results", None)
                detail = str(exc)
                if gates:
                    detail += "; " + "; ".join(f"{g.gate}: {g.detail}" for g in gates if not g.passed)
                step.outcome, step.detail = ("gates_failed" if gates else "invalid"), detail[:1500]
                feedback = (f"\n\nAttempt {attempt} was rejected:\n{explain(detail)[:3500]}\n\nReturn the complete corrected pack "
                            "as one ```yaml block. Fix exactly what was rejected; keep everything that was fine.")
                continue
            step.detail = f"published {pub.ref}"
            result.published.append(pub.ref)
            return True
        return False

    def write_entity(self, result: AgentResult, ent: PlannedEntity) -> bool:
        system = ("You are the entity author of a healthcare synthetic-data factory. Write one entity capability pack "
                  "as YAML. Never write code; only the pack.\n\n" + self.skill.context(ENTITY_REFS))
        prompt = ("Write the entity pack for:\n" + ent.model_dump_json(indent=1) +
                  f"\n\nThe pack id is entity.{ent.name}, version 1.0.0, kind entity, anchor member. Put your assumptions "
                  "in description (use a `>-` block scalar for long text). If the building blocks cannot express "
                  "something, simplify or drop it and say so; do not deliberate at length. Answer with one ```yaml "
                  "block containing the whole pack.")
        return self._author(result, f"entity:{ent.name}", system, prompt, f"entity.{ent.name}")

    def write_use_case(self, result: AgentResult, uc: PlannedUseCase) -> bool:
        tables = {}
        for name in uc.needs:
            pk = packs.entity_pack(name)
            if pk is not None:
                tables[name] = yaml.safe_dump(pk.model_dump(mode="json", by_alias=True)["entity"], sort_keys=False)
        core = self.skill.read("references/domain/entities.md")
        core_lines = [ln for ln in core.splitlines() if any(f"**{n}**" in ln for n in uc.needs)]
        system = ("You are the API author of a healthcare synthetic-data factory. Write one use-case capability pack as "
                  "YAML. Never write code; only the pack.\n\n" + self.skill.context(USE_CASE_REFS))
        prompt = ("Write the use-case pack for:\n" + uc.model_dump_json(indent=1) +
                  "\n\nTables it reads (pack entities, full spec):\n" + ("\n".join(f"--- {k}\n{v}" for k, v in tables.items()) or "(none)") +
                  "\n\nCore tables it reads (columns):\n" + ("\n".join(core_lines) or "(none)") +
                  f"\n\nThe pack id is uc.{uc.code}, version 1.0.0, kind use_case, group member. Fields must map to real "
                  "columns of these tables (check each name against the lists above). Answer with one ```yaml block "
                  "containing the whole pack.")
        return self._author(result, f"use_case:{uc.code}", system, prompt, f"uc.{uc.code}")

    # -------------------------------------------------------------- one build
    def run(self, gaps: list[dict], context: dict[str, Any]) -> AgentResult:
        result = AgentResult(status="FAILED")
        plan = self.plan(result, gaps, context)
        if plan is None:
            result.error = "the planner gave no valid plan"
            return result
        result.plan = plan.model_dump()
        if plan.questions or plan.confidence < self.min_confidence:
            result.status, result.questions = "UNDERSPECIFIED", plan.questions or [f"confidence {plan.confidence} is too low"]
            result.steps[-1].outcome = "underspecified"
            return result
        for ent in plan.entities:
            if ent.name in catalog.PRIMARY_KEY:
                continue  # core, or published meanwhile
            if not self.write_entity(result, ent):
                result.error = f"entity {ent.name}: no pack passed its gates in {self.max_attempts} attempts"
                return result
        for uc in plan.use_cases:
            if packs.use_case_pack(uc.code) is not None or uc.code in catalog.CORE_USE_CASES:
                continue
            if not self.write_use_case(result, uc):
                result.error = f"use case {uc.code}: no pack passed its gates in {self.max_attempts} attempts"
                return result
        if plan.unsupported:
            result.error = "unsupported: " + "; ".join(f"{u.gap}: {u.reason}" for u in plan.unsupported)
        result.status = "READY"
        return result
