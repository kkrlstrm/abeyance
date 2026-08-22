"""The disposable planner: a worker whose one job is deciding what the case should try next.

`warrant.py` covers the steps you can write down. A rule fires because a number crossed a line,
and that covers most of real coordination. What it cannot cover is the moment a case is *stuck in
a way nobody anticipated* — the evidence is in, no rule matches it, and the honest next move is a
judgment call. Today that moment ends the case's autonomy: it sits until a person reads it.

The planner is a disposable worker for exactly that moment. It reads the case, picks from the
registered capabilities, and proposes what to try next. Then it dies. Nothing about it is durable
and nothing about it is in charge:

    stuck case ──▶ tick ──▶ one planner worker ──▶ a RECOMMENDATION ──▶ tick validates it
                                    │                                        │
                                   dies                          registered? ──▶ dispatch
                                                                          no ──▶ BLOCKED

Two separations do all the work here.

**The planner decides what to try; abeyance decides what is allowed.** A plan is a
`RECOMMENDATION` — the same contribution kind a fraud score or a security review writes, with the
same authority, which is none. It never reaches `standing.py`. It cannot mint a capability, widen
reach, authorize anything, or answer a human's question. What it produces is *data*: labels and
freeform specs. The `Need` objects that come out of that data are constructed here, by the
library, which is why a planner cannot mark its own evidence `optional` or route around the
registry with `external=True`. It never touches those fields.

**A planner may not extend the case's life.** This is the half most planner designs get wrong,
and it is the reason this module is mostly limits. An agent asked "what else should we look at?"
will always find an answer, so a planner with no budget produces a case that investigates
forever, each round individually reasonable, the whole never closing. Six constraints prevent it,
and every one of them is deterministic — none of them relies on the model being disciplined:

  **A hard round budget.** `max_plans` planning rounds per case, ever (default 2). Not per day,
  not per phase.

  **A hard work budget.** `max_planned_needs` requests a planner may add across all rounds
  (default 3), and `max_needs_per_plan` in any one round (default 2).

  **Every proposal must name what would change.** `changes_decision_if` is required and a
  proposal without it is dropped before it can be dispatched. "It would be good to know" is not a
  reason to spend a container; "if the bounce rate is over 3% we send to a tenth of the list" is.

  **Planning only happens at a standstill.** Never while a worker is in flight, never while a
  human is being asked, never once anybody has decided, and — importantly — never around a
  request that FAILED. Planning past a hole in the evidence is how a case proceeds on the facts
  it happens to have.

  **Running out of budget ends in a person, not a stall.** When the budget is spent the planner's
  last act is to warrant the human decision on whatever is already on the record. The terminal
  state of pathfinding is a decision, not another loop.

  **A round that proposes nothing usable goes straight to a person.** No retry, no second
  attempt at the same tick. The planner had its turn.

The net guarantee, which is the sentence to hold onto: **a planner can add at most
`max_planned_needs` pieces of work to a case, across at most `max_plans` rounds, and then the case
goes to a human.** Both numbers are on the case's own `PlanBudget`, both are checked from durable
state rather than from anything the planner says about itself, and neither can be raised by
anything the planner writes.

Wiring, in full:

    from abeyance import Planner, planner_capability

    planner = Planner(registry)                      # after the registry is built
    registry.add(planner_capability(image="ghcr.io/you/planner:sha-...", app="workers-model"))

    cases = CaseLoop("launches", store=..., registry=registry, runner=...,
                     rules=[*your_deterministic_rules, *planner.rules()])

`planner.rules()` returns two: an adopter that turns a landed plan into requests, and a trigger
that asks for a plan — the trigger being a *fallback* rule, so it only runs on a tick where no
deterministic rule warranted anything. Deterministic work always wins; the planner is what happens
when there is none.

The worker itself is not shipped, deliberately — this library holds no model router and no API
key. What is shipped is the whole instruction, in the request `spec`: the case digest, the
capability catalogue, the budget, the output schema and `PLANNER_INSTRUCTIONS`. A generic
model-hosting image that reads `ABEYANCE_SPEC`, calls a model, and writes one contribution is
enough. `examples/planner_case.py` is a working one in about thirty lines.

One small deliberate thing about the payload: a plan says `assessment: "ready-for-decision"`, not
`decision` or `approved`. It is a recommendation about *whose turn it is*, and it should not have
to be read carefully to see that it is not claiming authority — in the record, in an alert, or by
`standing._claims_authority_without_standing`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .capability import Capability, CapabilityRegistry
from .cases import HUMAN_DECISION
from .errors import ConfigurationError
from .models import Case, Contribution, ContributionKind, ContributionRequest, RequestStatus
from .warrant import CaseView, Need, Rule

PLAN_NEED = "plan-next-step"
"""The conventional need label for "work out what to try next". One capability produces it."""

PLAN_TAG = "_plan"
"""Spec key stamped on every request a plan produced, holding that plan's contribution id.

Load-bearing rather than decorative. Rules are pure and re-evaluated on every tick, so the adopter
has to answer "have I already turned this plan into work?" from durable state alone. This is that
state — and it doubles as the audit trail: every planner-derived request names the plan that asked
for it, so "why is there a deliverability check on this case?" resolves to a specific plan, its
rationale, and the round it came from.
"""

# --------------------------------------------------------------------------- the plan contract

ASSESS_READY = "ready-for-decision"
"""Enough is on the record; a person with standing should decide. Proposals are ignored."""

ASSESS_WORK = "needs-work"
"""Named work would change what the case does. Proposals are reviewed."""

ASSESS_BLOCKED = "blocked"
"""The case cannot progress with the capabilities that exist. `missing_capabilities` says why."""

ASSESSMENTS = (ASSESS_READY, ASSESS_WORK, ASSESS_BLOCKED)

MAX_PROPOSALS_PARSED = 25
"""Hard parse-time ceiling, well above any sane `max_needs_per_plan`. A plan with two hundred
proposals is a malfunction, not an ambitious plan, and the excess is dropped with a note rather
than iterated over."""

# Rejection reasons. Named constants because they end up in two places a person reads — the
# request spec that explains why work happened, and `abeyance case-plan` — and a reason typed
# inline twice is a reason that reads differently in the two places it matters.
REJECT_MALFORMED = "malformed"
REJECT_DUPLICATE = "duplicate-in-plan"
REJECT_SELF_PLANNING = "planner-may-not-plan-more-planning"
REJECT_DECISION_IS_NOT_A_NEED = "a-decision-is-not-a-need-use-the-assessment"
REJECT_ALREADY_REQUESTED = "already-requested-on-this-case"
REJECT_NO_DECISION_RELEVANCE = "no-changes_decision_if"
REJECT_SPEC_TOO_LARGE = "spec-too-large"
REJECT_OVER_FANOUT = "over-max_needs_per_plan"
REJECT_BUDGET_SPENT = "over-max_planned_needs"
REJECT_READY_MEANS_PROPOSE_NOTHING = "assessment-is-ready-for-decision"


@dataclass(frozen=True)
class PlanProposal:
    """One thing a planner thinks the case should do next. Data, not a `Need`.

    The distinction is the safety boundary. A `Need` can be `optional` (its failure stops blocking
    authorization) or `external` (no capability is matched at all, so the registry never gets a
    say). Neither field exists here, so no plan can set them: the library reads these four values
    and builds the `Need` itself.
    """

    need: str
    """A need label from the catalogue the planner was given. Anything else is a reach request."""

    why: str = ""
    """One line, for the record. Ends up as `because` in the dispatched request's spec."""

    changes_decision_if: str = ""
    """**The pragmatism gate.** What different answer would change what this case does?

    Required by default (`PlanBudget.require_decision_relevance`) and checked deterministically:
    empty means the proposal is dropped. It is the cheapest available filter on the failure this
    module exists to prevent, because the question "what would this change?" has no good answer
    for the investigation nobody needs, and a planner cannot bluff past a string-emptiness check.
    """

    spec: Dict[str, Any] = field(default_factory=dict)
    """Freeform instruction for the worker — Tier 1, ungated, exactly as a hand-written rule's
    spec is. Bounded by `PlanBudget.max_spec_bytes` because a spec rides in an env var."""

    def to_doc(self) -> Dict[str, Any]:
        return {"need": self.need, "why": self.why,
                "changes_decision_if": self.changes_decision_if, "spec": dict(self.spec)}


@dataclass(frozen=True)
class Plan:
    """A parsed plan contribution. Tolerant to read, strict to act on.

    Parsing never raises: a plan is written by a model, and a model that emits the wrong shape
    should produce a *rejected* plan with a stated reason, not an exception inside a cron tick
    that also has ninety other cases to advance. Everything unusable lands in `malformed`, which
    is surfaced rather than swallowed.
    """

    assessment: str = ""
    rationale: str = ""
    proposals: Tuple[PlanProposal, ...] = ()
    missing_capabilities: Tuple[Dict[str, str], ...] = ()
    contribution_id: str = ""
    request_id: str = ""
    malformed: Tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.assessment == ASSESS_READY

    def to_doc(self) -> Dict[str, Any]:
        return {"assessment": self.assessment, "rationale": self.rationale,
                "proposals": [p.to_doc() for p in self.proposals],
                "missing_capabilities": [dict(m) for m in self.missing_capabilities],
                "contribution_id": self.contribution_id, "request_id": self.request_id,
                "malformed": list(self.malformed)}


def parse_plan(payload: Dict[str, Any], *, contribution_id: str = "",
               request_id: str = "") -> Plan:
    """Read a plan out of a contribution payload. Never raises."""
    bad: List[str] = []
    payload = payload or {}

    assessment = str(payload.get("assessment") or "").strip().lower()
    if assessment not in ASSESSMENTS:
        bad.append(f"assessment {assessment!r} is not one of {list(ASSESSMENTS)}")
        # An unreadable assessment is treated as `needs-work`, not as ready. Failing towards "keep
        # working" rather than "go ahead and decide" is the safe direction: the review below still
        # has to accept each proposal on its own merits, so the worst case is a wasted round,
        # whereas defaulting to ready would put a garbled plan in front of a human as though the
        # case had converged.
        assessment = ASSESS_WORK

    raw = payload.get("proposals")
    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        bad.append(f"proposals is {type(raw).__name__}, expected a list")
        raw = []
    if len(raw) > MAX_PROPOSALS_PARSED:
        bad.append(f"{len(raw)} proposals; only the first {MAX_PROPOSALS_PARSED} were read")
        raw = list(raw)[:MAX_PROPOSALS_PARSED]

    proposals: List[PlanProposal] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            bad.append(f"proposal {i} is {type(item).__name__}, expected an object")
            continue
        spec = item.get("spec") or {}
        if not isinstance(spec, dict):
            bad.append(f"proposal {i} has a non-object spec; dropped the spec, kept the need")
            spec = {}
        proposals.append(PlanProposal(
            need=str(item.get("need") or "").strip(),
            why=str(item.get("why") or "")[:500],
            changes_decision_if=str(item.get("changes_decision_if") or "")[:500],
            spec=spec))

    missing: List[Dict[str, str]] = []
    for item in (payload.get("missing_capabilities") or []):
        if isinstance(item, str):
            missing.append({"need": item.strip(), "why": ""})
        elif isinstance(item, dict) and item.get("need"):
            missing.append({"need": str(item["need"]).strip(),
                            "why": str(item.get("why") or "")[:500]})
        else:
            bad.append(f"unreadable missing_capabilities entry: {str(item)[:80]}")

    return Plan(assessment=assessment,
                rationale=str(payload.get("rationale") or "")[:2000],
                proposals=tuple(proposals),
                missing_capabilities=tuple(m for m in missing if m["need"]),
                contribution_id=contribution_id, request_id=request_id,
                malformed=tuple(bad))


# --------------------------------------------------------------------------- the budget


@dataclass(frozen=True)
class PlanBudget:
    """What a planner may spend on one case. Every field is a ceiling; none is a target.

    The defaults are deliberately stingy. A planner that gets two rounds and three units of work
    has to spend them on what actually decides the case, and a case that is not decidable inside
    that is a case a person should be looking at anyway. Raise them when you have watched a real
    case want more — not in advance, on the theory that more thinking is better.
    """

    max_plans: int = 2
    """Planning rounds per case, for the life of the case. Counted from the case's own requests,
    so it survives every process that ever touched it."""

    max_needs_per_plan: int = 2
    """Proposals accepted from any one plan. A plan naming five things has not prioritised, and
    a planner that knows only two will land is pushed to rank them."""

    max_planned_needs: int = 3
    """Requests a planner may add across every round, ever. The backstop that stops a raised
    `max_plans` quietly becoming an unbounded work budget."""

    require_decision_relevance: bool = True
    """Drop any proposal that does not say what different answer would change the outcome. Turn
    it off only if you enjoy paying for curiosity."""

    max_spec_bytes: int = 4096
    """Ceiling on one proposal's serialized spec. It rides in an env var to the worker."""

    ask_human_when_spent: bool = True
    """When the budget is spent, warrant the human decision on what is already on the record.

    On by default because the alternative — stopping quietly — produces a case that is neither
    progressing nor asking for anything, which is the exact shape of failure this library is built
    to make impossible. Set False only when something else is driving the human ask.
    """

    def validate(self) -> None:
        if self.max_plans < 1:
            raise ConfigurationError("max_plans must be >= 1 — use no planner rules instead")
        if self.max_needs_per_plan < 1:
            raise ConfigurationError("max_needs_per_plan must be >= 1")
        if self.max_planned_needs < 1:
            raise ConfigurationError("max_planned_needs must be >= 1")
        if self.max_spec_bytes < 1:
            raise ConfigurationError("max_spec_bytes must be >= 1")


PLAN_FRUGAL = PlanBudget(max_plans=1, max_needs_per_plan=1, max_planned_needs=1)
"""One round, one extra piece of work, then a person. The most conservative useful planner."""

PLAN_STANDARD = PlanBudget()
"""Two rounds, three units of work. The default."""


@dataclass(frozen=True)
class DigestLimits:
    """How much of the case the planner is shown. Truncation is always marked, never silent."""

    max_contributions: int = 20
    """Newest-first per kind, then presented oldest-first. A dropped count is reported."""

    max_payload_chars: int = 1200
    max_summary_chars: int = 300
    max_context_chars: int = 2000


# --------------------------------------------------------------------------- the review


@dataclass
class PlanReview:
    """What the library decided to do with a plan. Pure, and recomputable from durable state.

    Deliberately a value rather than something written to the case: it is a function of the plan,
    the case and the registry, so it can be re-derived by `abeyance case-plan` months later and
    will read the same as it did on the tick that acted on it.
    """

    plan: Plan
    accepted: List[Need] = field(default_factory=list)
    rejected: List[Tuple[str, str]] = field(default_factory=list)
    """`(need, reason)` — one entry per proposal that will not be dispatched, and why."""

    missing: List[str] = field(default_factory=list)
    """Proposed needs no registered capability produces. The reach ceiling, named."""

    ready: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return bool(self.accepted)

    def to_doc(self) -> Dict[str, Any]:
        return {"plan": self.plan.to_doc(),
                "accepted": [{"need": n.need, "spec": n.spec} for n in self.accepted],
                "rejected": [{"need": n, "reason": r} for n, r in self.rejected],
                "missing": list(self.missing), "ready": self.ready, "notes": list(self.notes)}


def review_plan(plan: Plan, view: CaseView, registry: CapabilityRegistry, budget: PlanBudget, *,
                plan_need: str = PLAN_NEED,
                decision_need: str = HUMAN_DECISION) -> PlanReview:
    """Validate a plan against the registry, the case, and the budget. Pure; no side effects.

    Order matters in one place: the registry check comes *before* the fan-out and budget caps, so
    a plan whose third proposal needs an unregistered capability still reports that gap. Hitting a
    spending limit must never hide the fact that the case has reached the edge of its reach.
    """
    review = PlanReview(plan=plan, ready=plan.ready)
    for m in plan.malformed:
        review.notes.append(f"{REJECT_MALFORMED}: {m}")

    # A plan that names something nothing can reach short-circuits the whole plan. The alternative
    # — dispatch the reachable half and block on the rest — half-executes a plan whose own author
    # said it was incomplete, and then needs another round to find that out. The case blocks, a
    # person mints the capability or closes the case, and the next tick picks up exactly here.
    reserved = (plan_need, decision_need)
    named_missing = [m["need"] for m in plan.missing_capabilities
                     if m.get("need") and m["need"] not in reserved]
    named_missing += [p.need for p in plan.proposals if p.need and p.need not in reserved]
    # `match` has the last word in both directions: a proposal for something unregistered is a
    # gap whether or not the planner noticed, and a "missing" capability that turns out to exist
    # is the planner being wrong about the catalogue, not a reason to block a case.
    missing = sorted({m for m in named_missing if registry.match(m) is None})
    if missing:
        review.missing = missing
        review.notes.append(
            "the plan needs a capability that does not exist; nothing else in it was adopted")
        return review

    room_round = budget.max_needs_per_plan
    room_total = budget.max_planned_needs - planned_needs_used(view.case)
    seen: set = set()

    for p in plan.proposals:
        need = (p.need or "").strip()
        if not need:
            review.rejected.append(("", REJECT_MALFORMED))
            continue
        if plan.ready:
            # `ready-for-decision` and a list of things to do next are contradictory. Honouring the
            # assessment is the safe reading of the contradiction: it is the one that closes.
            review.rejected.append((need, REJECT_READY_MEANS_PROPOSE_NOTHING))
            continue
        if need in seen:
            review.rejected.append((need, REJECT_DUPLICATE))
            continue
        seen.add(need)
        if need == plan_need:
            # Self-perpetuation, and the one proposal a planner has an obvious incentive to make.
            review.rejected.append((need, REJECT_SELF_PLANNING))
            continue
        if need == decision_need:
            review.rejected.append((need, REJECT_DECISION_IS_NOT_A_NEED))
            continue
        if view.requested(need):
            review.rejected.append((need, REJECT_ALREADY_REQUESTED))
            continue
        if budget.require_decision_relevance and not p.changes_decision_if.strip():
            review.rejected.append((need, REJECT_NO_DECISION_RELEVANCE))
            continue
        try:
            size = len(json.dumps(p.spec, ensure_ascii=False))
        except (TypeError, ValueError):
            review.rejected.append((need, REJECT_MALFORMED))
            continue
        if size > budget.max_spec_bytes:
            review.rejected.append((need, f"{REJECT_SPEC_TOO_LARGE} ({size}b)"))
            continue
        if room_round <= 0:
            review.rejected.append((need, REJECT_OVER_FANOUT))
            continue
        if room_total <= 0:
            review.rejected.append((need, REJECT_BUDGET_SPENT))
            continue

        # The library builds the Need. The plan supplied four strings and a dict; it did not get
        # to choose `optional` or `external`, which is what keeps a plan from marking its own
        # evidence non-blocking or routing around the registry.
        review.accepted.append(Need(need, spec={
            **p.spec,
            "because": p.why or f"proposed by the planner on {view.case.id}",
            "changes_decision_if": p.changes_decision_if,
            PLAN_TAG: plan.contribution_id}))
        room_round -= 1
        room_total -= 1

    return review


# --------------------------------------------------------------------------- counting, purely


def plan_requests(case: Case, plan_need: str = PLAN_NEED) -> List[ContributionRequest]:
    """Every planning round this case has asked for, oldest first."""
    return [r for r in case.requests if r.need == plan_need]


def rounds_used(case: Case, plan_need: str = PLAN_NEED) -> int:
    return len(plan_requests(case, plan_need))


def planned_needs_used(case: Case) -> int:
    """Dispatchable requests a planner has added to this case, across every round.

    Counted from the requests themselves rather than from the plans, because requests are what
    cost money, and because a plan that was written but never adopted should not count against
    anything.
    """
    return len([r for r in case.requests if (r.spec or {}).get(PLAN_TAG) and r.capability])


def round_request_id(n: int, plan_need: str = PLAN_NEED) -> str:
    """Round 1 keeps the bare need label; later rounds get a suffix.

    Distinct request ids are what make a second round possible at all — `derive()` drops a need
    whose request id is already on the case, which is the structural idempotence that stops rules
    double-requesting. The rounds are genuinely different requests asking the same question at
    different times, which is exactly what `Need.request_id` is for.
    """
    return plan_need if n <= 1 else f"{plan_need}#{n}"


def latest_plan(view: CaseView, plan_need: str = PLAN_NEED) -> Optional[Plan]:
    """The newest plan on the case, or None. Superseded contributions never count (`CaseView`)."""
    ids = {r.id for r in plan_requests(view.case, plan_need)}
    plans = [c for c in view.contributions
             if c.request_id in ids and c.kind is ContributionKind.RECOMMENDATION]
    if not plans:
        return None
    newest = max(plans, key=lambda c: (c.created_epoch, c.id))
    return parse_plan(newest.payload, contribution_id=newest.id, request_id=newest.request_id)


# --------------------------------------------------------------------------- the worker's brief

PLANNER_INSTRUCTIONS = """\
You are a disposable planner for one case. You run once, you will not see this case again, and
another planner may take over later from what is written in the case. Nothing you produce is a
decision, an approval, or permission for anything.

Your job is to get this case to the point where a person can decide it. It is NOT to make the
case thorough. A case that closes on adequate evidence beats a case that is still gathering
excellent evidence a week later.

How to answer:

1. FIRST ask whether the case can already be decided. If a person with standing could reasonably
   answer on what is already on the record, reply with assessment "ready-for-decision", give
   your rationale, and propose nothing. That is the best outcome available to you, not a failure
   to contribute.

2. If not, propose the FEWEST needs that would change what this case does. You may propose at
   most `budget.needs_you_may_propose` of them, and they must come from `capabilities` — those
   labels and no others.

3. Every proposal must fill in `changes_decision_if`: name the specific finding that would change
   the outcome ("if the bounce rate is above 3% we cut the wave to a tenth"). If you cannot name
   one, do not propose it. Proposals without this are discarded before anything runs, so an
   unfillable one costs you a slot and buys nothing.

4. Do not re-propose anything in `case.requests` — it has already been asked. Do not propose work
   that was proposed in a previous round and not adopted; `case.previous_plans` shows those, and
   they were dropped for a reason.

5. If this case genuinely cannot be closed with the capabilities listed, reply with assessment
   "blocked" and put what is missing in `missing_capabilities`, naming the system it would have to
   reach and why. A person decides whether to build it. Do not approximate it with a capability
   that reaches somewhere else.

6. You have a hard budget: `budget.rounds_left` planning rounds remain on this case, for its
   entire life. When they are gone the case goes to a person with whatever is on the record. Spend
   what you have on what decides the case.

Answer with a single JSON object and nothing else.
"""

PLAN_SCHEMA = {
    "assessment": "ready-for-decision | needs-work | blocked",
    "rationale": "one paragraph: where this case stands and why this is the right next move",
    "proposals": [{
        "need": "a label from capabilities[].need",
        "why": "one line for the record",
        "changes_decision_if": "REQUIRED — the finding that would change the outcome",
        "spec": {"...": "freeform instruction for that worker"},
    }],
    "missing_capabilities": [{"need": "label", "why": "what it would have to reach, and why"}],
}


# --------------------------------------------------------------------------- the planner


class Planner:
    """Two rules and a budget: ask for a plan when stuck, turn a landed plan into work.

    Holds the registry so it can hand the planner a catalogue of what exists and refuse a proposal
    that names anything else. Holds no state of its own — every count it makes comes from the case
    row, so a planner configured on one host and ticked from another agrees with itself.
    """

    def __init__(self, registry: CapabilityRegistry, *, budget: PlanBudget = PLAN_STANDARD,
                 need: str = PLAN_NEED, decision_need: str = HUMAN_DECISION,
                 name: str = "planner", limits: DigestLimits = DigestLimits()) -> None:
        budget.validate()
        self.registry = registry
        self.budget = budget
        self.need = need
        self.decision_need = decision_need
        self.name = name
        self.limits = limits

    # ----------------------------------------------------------------- wiring

    def rules(self) -> List[Rule]:
        """The pair, in the order they must be registered.

        The adopter is ordinary; the trigger is a fallback, which is what makes a planner the last
        resort rather than a competitor to your deterministic rules. On any tick where a rule you
        wrote warrants something, no plan is asked for and no model is called.
        """
        return [
            Rule(f"{self.name}:adopt", self._adopt,
                 description="turn a validated plan into requests, or into a human decision"),
            Rule(f"{self.name}:trigger", self._trigger, fallback=True,
                 description="ask a disposable planner what to try next, when nothing else "
                             "warranted anything and the case is at a standstill"),
        ]

    # ----------------------------------------------------------------- reading

    def latest(self, view: CaseView) -> Optional[Plan]:
        return latest_plan(view, self.need)

    def review(self, view: CaseView) -> Optional[PlanReview]:
        """The current verdict on the newest plan. What `abeyance case-plan` prints."""
        plan = self.latest(view)
        if plan is None:
            return None
        return review_plan(plan, view, self.registry, self.budget,
                           plan_need=self.need, decision_need=self.decision_need)

    def status(self, view: CaseView) -> Dict[str, Any]:
        """Where this case stands with respect to planning. For humans and for the CLI.

        The two questions worth being able to answer without a debugger: how much of its budget
        this case's planner has spent, and — if it is sitting still — why it is not planning right
        now. `why_not` is the second one, in words.
        """
        used = rounds_used(view.case, self.need)
        added = planned_needs_used(view.case)
        standstill = self._standstill(view)
        review = self.review(view)
        return {
            "planner": self.name,
            "budget": {"max_plans": self.budget.max_plans,
                       "max_needs_per_plan": self.budget.max_needs_per_plan,
                       "max_planned_needs": self.budget.max_planned_needs,
                       "require_decision_relevance": self.budget.require_decision_relevance,
                       "ask_human_when_spent": self.budget.ask_human_when_spent},
            "rounds_used": used,
            "rounds_left": max(0, self.budget.max_plans - used),
            "needs_added": added,
            "needs_left": max(0, self.budget.max_planned_needs - added),
            "would_plan_now": not standstill and used < self.budget.max_plans,
            "why_not": standstill,
            "review": review.to_doc() if review else None,
        }

    # ----------------------------------------------------------------- the rules

    def _adopt(self, view: CaseView) -> Sequence[Need]:
        """A plan has landed. Validate it, and turn what survives into requests."""
        plan = self.latest(view)
        if plan is None:
            return []
        review = review_plan(plan, view, self.registry, self.budget,
                             plan_need=self.need, decision_need=self.decision_need)

        if review.missing:
            # Emitted every tick for as long as the gap is real. Each becomes an unmatched need in
            # `derive()`, so the case is BLOCKED and `CAPABILITY_MISSING` names what is missing —
            # once, not hourly. The moment a person registers the capability, the same emission
            # matches, becomes a request, and the case carries on from here with no intervention.
            return [Need(m, spec={"because": self._missing_why(plan, m),
                                  PLAN_TAG: plan.contribution_id})
                    for m in review.missing]

        if self._adopted(view, plan):
            return []  # already turned into work; the rest of this plan's life is history

        out: List[Need] = list(review.accepted)
        if review.ready or not out:
            why = ("the planner assessed the case ready for a decision"
                   if review.ready else
                   "the planner proposed nothing this case can act on: "
                   + (", ".join(f"{n or '?'} ({r})" for n, r in review.rejected)
                      or "it proposed nothing at all"))
            out.extend(self._ask_a_person(view, why, plan=plan))
        return out

    def _trigger(self, view: CaseView) -> Sequence[Need]:
        """Nothing deterministic warranted anything. Is this a standstill worth a planner?"""
        blocked = self._standstill(view)
        if blocked:
            return []

        used = rounds_used(view.case, self.need)
        if used >= self.budget.max_plans:
            return self._ask_a_person(
                view, f"the planner's budget is spent ({used} of {self.budget.max_plans} "
                      "rounds); deciding on what is on the record")
        if planned_needs_used(view.case) >= self.budget.max_planned_needs:
            return self._ask_a_person(
                view, f"the planner has added the {self.budget.max_planned_needs} pieces of work "
                      "it may add to a case; deciding on what is on the record")

        # `optional` on purpose, and it is the one place the planner is treated as less than a
        # first-class contributor — correctly. A planner container that will not boot is a real
        # operational problem and is escalated as `REQUEST_FAILED` like any other, but it must not
        # hold the case hostage: everything the case needs in order to reach a person is already
        # on the record, and blocking a decidable case because the *advisor* crashed is the
        # opposite of what this is for. The round still counts against the budget, so a planner
        # that fails repeatedly cannot buy itself extra attempts either.
        return [Need(self.need, request_id=round_request_id(used + 1, self.need),
                     optional=True, spec=self.brief(view, used + 1))]

    # ----------------------------------------------------------------- the guards

    def _standstill(self, view: CaseView) -> str:
        """`""` if this case is at a standstill a planner should think about; else why not.

        Returning the reason rather than a bool is not decoration. "Why didn't the planner run?"
        is the first question anybody asks of a case that sat still, and the answer has to be
        available without attaching a debugger to a cron job.
        """
        case = view.case
        # Dispatchable work only. An external request is outstanding too, but calling a question
        # sitting in somebody's inbox "in flight" sends whoever reads this looking for a container
        # that does not exist — so the three ways a case can be busy are named separately.
        in_flight = [r.id for r in case.requests if r.is_outstanding and r.capability]
        if in_flight:
            return f"work is in flight: {in_flight}"

        failed = [r.id for r in case.requests
                  if r.status is RequestStatus.FAILED and r.blocks_authorization]
        if failed:
            # The one guard that is about correctness rather than cost. Planning around evidence
            # that could not be gathered is precisely "we could not get it, so we proceeded on
            # what we had" — the failure the case layer exists to refuse. A person fixes the
            # worker, cancels the request, or marks it optional.
            return f"a request failed and blocks authorization: {failed} — a person fixes that"

        pending_decision = [r.id for r in case.requests
                            if r.expects is ContributionKind.DECISION
                            and r.status is not RequestStatus.SATISFIED]
        if pending_decision:
            return f"a human decision is outstanding: {pending_decision}"

        awaiting = [r.id for r in case.requests if r.is_outstanding and not r.capability]
        if awaiting:
            return f"waiting on an out-of-band answer: {awaiting}"

        if view.decided:
            return "somebody has already decided; the case is past planning"

        return ""

    def _adopted(self, view: CaseView, plan: Plan) -> bool:
        if not plan.contribution_id:
            return False
        return any((r.spec or {}).get(PLAN_TAG) == plan.contribution_id
                   for r in view.case.requests)

    def _ask_a_person(self, view: CaseView, why: str, *,
                      plan: Optional[Plan] = None) -> List[Need]:
        """The closing move: warrant the human decision on what is already on the record.

        External, so no capability is matched and nothing is dispatched — it can only block the
        case until somebody answers, which is the whole point. `CaseLoop.ask_humans()` is what
        turns it into a message; a tick that finds this request outstanding is the signal to send
        one.
        """
        if not self.budget.ask_human_when_spent:
            return []
        if view.requested(self.decision_need):
            return []
        spec: Dict[str, Any] = {"because": why, "planned_by": self.name,
                                "rounds_used": rounds_used(view.case, self.need)}
        if plan is not None:
            spec[PLAN_TAG] = plan.contribution_id
            spec["planner_rationale"] = plan.rationale
        return [Need(self.decision_need, external=True,
                     expects=ContributionKind.DECISION, spec=spec)]

    def _missing_why(self, plan: Plan, need: str) -> str:
        for m in plan.missing_capabilities:
            if m.get("need") == need:
                return (m.get("why") or "")[:300] or f"the planner needs {need!r}"
        for p in plan.proposals:
            if p.need == need:
                return (p.why or "")[:300] or f"the planner needs {need!r}"
        return f"the planner needs {need!r}"

    # ----------------------------------------------------------------- the brief

    def brief(self, view: CaseView, round_n: int) -> Dict[str, Any]:
        """Everything the planner worker gets, as one JSON-serializable dict.

        Built here rather than read by the worker from the store, for three reasons that all point
        the same way: the worker needs no read access to the case (so its credential set stays a
        write-only insert), the digest is bounded and testable rather than however big the case
        happens to be, and what a planner was shown is recorded on the request row — so a decision
        that came out of a plan can be re-read years later against the picture that produced it.
        """
        used = rounds_used(view.case, self.need)
        return {
            "role": "planner",
            "instructions": PLANNER_INSTRUCTIONS,
            "output_schema": PLAN_SCHEMA,
            "budget": {
                "round": round_n,
                "rounds_left": max(0, self.budget.max_plans - used),
                "needs_you_may_propose": max(0, min(
                    self.budget.max_needs_per_plan,
                    self.budget.max_planned_needs - planned_needs_used(view.case))),
                "changes_decision_if_required": self.budget.require_decision_relevance,
                "when_rounds_run_out": ("the case goes to a person for a decision on whatever is "
                                        "on the record"),
            },
            "case": self._case_digest(view),
            "capabilities": self._catalogue(),
        }

    def _catalogue(self) -> List[Dict[str, Any]]:
        """What may be proposed, keyed by need label — which is what a proposal names.

        Images, apps, env and reach are deliberately absent. A planner picks among questions it
        can have answered; how those answers get fetched, and what credentials that takes, is not
        its business and is not a thing it should be reasoning about.
        """
        out: List[Dict[str, Any]] = []
        for cap in self.registry.all():
            if self.need in cap.produces:
                continue  # a planner may not propose more planning; do not advertise it
            for need in cap.produces:
                out.append({"need": need, "capability": cap.name,
                            "emits": cap.emits.value,
                            "description": cap.description or ""})
        return sorted(out, key=lambda d: d["need"])

    def _case_digest(self, view: CaseView) -> Dict[str, Any]:
        case, lim = view.case, self.limits
        plan_ids = {r.id for r in plan_requests(case, self.need)}

        def kind(k: ContributionKind) -> List[Contribution]:
            return [c for c in view.contributions
                    if c.kind is k and c.request_id not in plan_ids]

        return {
            "id": case.id,
            "action": case.action,
            "title": case.title,
            "subject_key": case.subject_key,
            "status": case.status.value,
            "goal": case.context.get("goal") or case.title or case.action,
            "context": _clip_json(case.context, lim.max_context_chars),
            "requests": [{"need": r.need, "status": r.status.value,
                          "blocks_authorization": r.blocks_authorization,
                          "warranted_by": r.warranted_by}
                         for r in case.requests],
            "evidence": self._contributions(case, kind(ContributionKind.EVIDENCE)),
            "recommendations": self._contributions(case, kind(ContributionKind.RECOMMENDATION)),
            "decisions": [{"actor": c.actor.id, "summary": c.summary[:lim.max_summary_chars]}
                          for c in kind(ContributionKind.DECISION)],
            "previous_plans": self._previous_plans(view),
        }

    def _contributions(self, case: Case, cs: Sequence[Contribution]) -> List[Dict[str, Any]]:
        lim = self.limits
        # The need label, resolved through the case rather than assumed from the request id. They
        # are the same string for an ordinary request and are not for one that overrode
        # `request_id`, and a planner matching evidence to the catalogue by eye needs the label
        # the catalogue actually uses.
        need_of = {r.id: r.need for r in case.requests}
        ordered = sorted(cs, key=lambda c: (c.created_epoch, c.id))
        dropped = max(0, len(ordered) - lim.max_contributions)
        out: List[Dict[str, Any]] = []
        if dropped:
            # Named, not silently cut. A planner reasoning over "all the evidence" when it was
            # shown two thirds of it is exactly the quiet wrongness this library is shaped against.
            out.append({"_dropped": dropped,
                        "_note": f"{dropped} older contribution(s) omitted for size"})
        for c in ordered[-lim.max_contributions:]:
            out.append({"need": need_of.get(c.request_id, c.request_id or "unsolicited"),
                        "actor": c.actor.id,
                        "summary": c.summary[:lim.max_summary_chars],
                        "payload": _clip_json(c.payload, lim.max_payload_chars),
                        "source": (c.provenance or {}).get("source")
                                  or (c.provenance or {}).get("host") or ""})
        return out

    def _previous_plans(self, view: CaseView) -> List[Dict[str, Any]]:
        """What earlier rounds proposed, and what actually became work.

        The convergence feedback loop, and the cheapest one available: a planner that can see its
        predecessor proposed `vendor-review` and that no request came of it will not spend a slot
        proposing it again. Both halves are read from durable state — the plan contribution and the
        requests stamped with its id — so no interpretation is involved.
        """
        out: List[Dict[str, Any]] = []
        for i, req in enumerate(plan_requests(view.case, self.need), start=1):
            got = [c for c in view.contributions if c.request_id == req.id
                   and c.kind is ContributionKind.RECOMMENDATION]
            if not got:
                continue
            plan = parse_plan(max(got, key=lambda c: c.created_epoch).payload)
            proposed = [p.need for p in plan.proposals]
            adopted = [r.need for r in view.case.requests
                       if (r.spec or {}).get(PLAN_TAG) and r.need in proposed]
            out.append({"round": i, "assessment": plan.assessment,
                        "rationale": plan.rationale[:600], "proposed": proposed,
                        "adopted": adopted,
                        "not_adopted": [n for n in proposed if n not in adopted]})
        return out


# --------------------------------------------------------------------------- wiring helpers


def planner_capability(*, image: str, app: str = "", need: str = PLAN_NEED,
                       name: str = "planner", reach: Sequence[str] = ("model-api",),
                       description: str = "", **kw: Any) -> Capability:
    """The planner's own registered worker. A normal capability, with the narrowest reach there is.

    Worth stating plainly, because a planner sounds like it should be privileged and is the
    opposite: it reads a digest it was handed and returns JSON. It needs no database, no API
    token, no write access to anything — `model-api` and nothing else. Everything it can influence
    it influences by *proposing*, and every proposal is validated against a registry it cannot
    edit. It is the least-privileged worker in the system and the one whose output is checked
    hardest.

    There is no `emits` parameter, the same way `Actor.worker()` has no `standing` parameter: the
    kind a planner contributes is the reason it is safe to let it plan, and a knob for it would be
    a knob for turning that off.
    """
    if "emits" in kw:
        raise ConfigurationError(
            "planner_capability() has no `emits` — a plan is a RECOMMENDATION by construction. "
            "A planner that emitted anything else would be proposing and deciding at once, which "
            "is the one thing this whole arrangement is for not doing.")
    return Capability(
        name=name, image=image, app=app, produces=(need,),
        emits=ContributionKind.RECOMMENDATION, reach=tuple(reach),
        description=description or ("Reads the case digest and proposes what to try next. "
                                    "Emits a RECOMMENDATION; carries no authority."),
        **kw)


def planner_for(rules: Sequence[Rule]) -> Optional["Planner"]:
    """The `Planner` behind a rule set, if one is wired in.

    `Planner.rules()` hands back bound methods, so the instance — and with it the budget those
    rules are *actually* enforcing — is recoverable from the rules themselves. Reporting needs
    that: a CLI that rebuilt a `Planner` with default settings in order to describe a case would
    print a budget the case is not running under, which is worse than printing nothing at all.
    """
    for r in rules:
        owner = getattr(r.predicate, "__self__", None)
        if isinstance(owner, Planner):
            return owner
    return None


def _clip_json(obj: Any, limit: int) -> Any:
    """Return `obj` if its JSON fits in `limit`, else a marked preview. Never raises."""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"_unserializable": str(type(obj).__name__)}
    if len(s) <= limit:
        return obj
    return {"_truncated": True, "_original_chars": len(s), "preview": s[:limit]}
