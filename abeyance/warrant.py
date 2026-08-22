"""What the case needs next, derived from what it already knows.

This is dynamic activity selection: the coordination graph is not known when the case opens.
Evidence arrives, and the arrival of that evidence is what makes new work warranted. A
security review reports that the vendor retains prompts; *that* is what warrants a privacy
review; the privacy review finds EU personal data; *that* is what warrants legal.

It is also the exact place this design could quietly become a rule engine, and CMMN's grave is
full of systems that did. So the model here is deliberately impoverished, and every limitation
is load-bearing:

  **A rule may only ADD a need.** It cannot retract one, cancel a request, modify another
  rule's request, or set a priority. There is no agenda, no conflict resolution, no salience.

  **Rules are pure and independent.** Each is `(CaseView) -> Sequence[Need]`, evaluated
  against the whole current state, in registration order for determinism. No rule sees another
  rule's output within a pass, so there is no ordering subtlety to reason about.

  **Idempotence is structural, not the rule author's problem.** `derive()` drops any need that
  already has a request. A rule that fires on every tick forever is a no-op after the first,
  which means rule authors write the obvious predicate and cannot create a duplicate-dispatch
  bug by forgetting a guard.

  **Chaining happens across ticks, not within one.** Evidence lands, the next tick derives
  from it. So the "chain" is bounded by real work completing, not by a fixpoint loop, and it is
  observable one step at a time in the case history.

  **The total is capped.** `CasePolicy.max_derived_requests` stops two rules that warrant each
  other from spending money forever. Hitting the cap blocks the case loudly — a silently
  truncated investigation looks like a thorough one.

  **There is exactly one tier.** A rule marked `fallback=True` runs only on a pass where no
  ordinary rule warranted anything. That is not a priority scheme — it is the difference between
  "here is what to do" and "nothing applies, now what", and only the second kind of rule needs to
  know whether the first kind had an answer. It is what `planner.py` is built on.

If you find yourself needing retraction, priorities, or within-pass chaining, that is the
signal to stop and reconsider rather than to grow this file. A workflow engine is the right
tool for a process whose shape you actually know.

    def needs_privacy_review(view):
        sec = view.payload("security-review")
        if sec.get("retains_customer_data") and not view.requested("privacy-review"):
            return [Need("privacy-review", spec={"because": "vendor retains prompts"})]
        return []

    rules = [Rule("privacy-if-retention", needs_privacy_review)]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .capability import CapabilityRegistry
from .models import (Case, Contribution, ContributionKind, ContributionRequest, RequestStatus)
from .policy import CasePolicy
from .standing import active


# --------------------------------------------------------------------------- what a rule wants


@dataclass
class Need:
    """A request a rule is asking for. Resolved to a capability by the registry, not by the rule.

    Rules name *what they need*, never *which container to run*. That keeps the rule about the
    domain ("this needs a privacy review") and keeps the choice of who can reach what in the
    registry, where it is reviewed.
    """

    need: str
    spec: Dict[str, Any] = field(default_factory=dict)
    """Freeform instruction handed to the worker at runtime. This is Tier 1 — arbitrary new
    behaviour, no gate, no new image. Put the whole prompt here if the worker is a model."""

    expects: Optional[ContributionKind] = None
    """Defaults to whatever the matched capability declares it emits. Set it to assert an
    expectation and have a mismatch be a configuration error instead of a surprise."""

    optional: bool = False
    """An optional need that fails does not block authorization. Use sparingly: most evidence
    that is worth gathering is worth blocking on."""

    external: bool = False
    """Answered out of band rather than by a container — the canonical case being "a person has
    to decide this, again, because the plan changed".

    No capability is matched for an external need, so it sits outside the reach ceiling. That
    exemption is safe in exactly one direction: an external request can only ever *block* a case
    until something answers it. It cannot cause anything to run, reach anything, or spend
    anything. It has to exist because a case whose plan materially changes needs a *fresh*
    decision, and a rule is the only thing positioned to notice — but rules must never mint
    capabilities, so the alternative would be a case proceeding on a yes given about a different
    plan.
    """

    request_id: str = ""
    """Defaults to the need label. Override only when one case legitimately needs the same kind
    of work twice (two vendors, two regions, a decision revisited)."""

    def id_for(self) -> str:
        return self.request_id or self.need


Predicate = Callable[["CaseView"], Sequence[Need]]


@dataclass(frozen=True)
class Rule:
    """A named predicate. The name is not decoration — it is written onto every request it
    creates as `warranted_by`, so "why did a privacy review happen on this case?" is answerable
    from the record months later without re-reading the code that was deployed at the time."""

    name: str
    predicate: Predicate
    description: str = ""

    fallback: bool = False
    """Evaluate this rule only on a pass where no ordinary rule warranted anything.

    The one tier this module has, and it exists for a single question: *what should happen when
    nothing deterministic applies?* A rule that answers that must not compete with the rules that
    know the answer, or it fires alongside them and plans against a picture that is about to
    change. This is what `planner.Planner` uses, and it is why a planner costs nothing on any tick
    where an ordinary rule had something to say.

    It adds no agenda and no priorities: within each tier, rules are still pure, independent, and
    evaluated in registration order. Two tiers, resolved by "did the first one produce anything",
    is the whole of it. If you find yourself wanting a third, that is the signal to stop.
    """

    def __call__(self, view: "CaseView") -> Sequence[Need]:
        out = self.predicate(view) or ()
        if isinstance(out, Need):  # tolerate a single Need, it is the common shape
            return [out]
        return out


# --------------------------------------------------------------------------- reading a case


class CaseView:
    """Read-only window onto a case and its live contributions, with the queries rules need.

    Read-only on purpose: a rule handed the mutable `Case` would eventually mutate it, and a
    rule that mutates state is the first step to needing an agenda. Everything here answers a
    question; nothing changes anything.
    """

    def __init__(self, case: Case, contributions: Sequence[Contribution]) -> None:
        self.case = case
        self.contributions: List[Contribution] = list(active(contributions))
        self._by_request: Dict[str, List[Contribution]] = {}
        for c in self.contributions:
            self._by_request.setdefault(c.request_id, []).append(c)

    # -- requests --------------------------------------------------------

    def requested(self, need: str) -> bool:
        """Has this need already been asked for, in any state? The idempotence guard rules use."""
        return self.case.request_for_need(need) is not None

    def status_of(self, need: str) -> Optional[RequestStatus]:
        r = self.case.request_for_need(need)
        return r.status if r else None

    def satisfied(self, need: str) -> bool:
        return self.status_of(need) is RequestStatus.SATISFIED

    def failed(self, need: str) -> bool:
        return self.status_of(need) is RequestStatus.FAILED

    def outstanding(self, need: str) -> bool:
        r = self.case.request_for_need(need)
        return bool(r and r.is_outstanding)

    # -- contributions ---------------------------------------------------

    def for_need(self, need: str) -> List[Contribution]:
        r = self.case.request_for_need(need)
        return list(self._by_request.get(r.id, [])) if r else []

    def payload(self, need: str) -> Dict[str, Any]:
        """The payload of the newest contribution answering `need`, or `{}`.

        `{}` rather than None so a rule reads `view.payload("x").get("y")` without guarding —
        a missing contribution and a contribution with nothing useful in it warrant the same
        next step, which is nothing.
        """
        got = self.for_need(need)
        if not got:
            return {}
        return dict(max(got, key=lambda c: (c.created_epoch, c.id)).payload or {})

    def of_kind(self, kind: ContributionKind) -> List[Contribution]:
        return [c for c in self.contributions if c.kind is kind]

    @property
    def evidence(self) -> List[Contribution]:
        return self.of_kind(ContributionKind.EVIDENCE)

    @property
    def recommendations(self) -> List[Contribution]:
        return self.of_kind(ContributionKind.RECOMMENDATION)

    @property
    def decided(self) -> bool:
        """Is there any decision at all? Note this does NOT check standing — a rule asking
        "has a human weighed in yet" is a scheduling question, and whether that decision
        carries authority is `standing.authorize()`'s job, not a rule's."""
        return bool(self.of_kind(ContributionKind.DECISION))

    def context(self, key: str, default: Any = None) -> Any:
        return self.case.context.get(key, default)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CaseView {self.case.id} contributions={len(self.contributions)}>"


# --------------------------------------------------------------------------- derivation


@dataclass
class Derivation:
    """What one derivation pass concluded."""

    new_requests: List[ContributionRequest] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    """Needs a rule warranted that no capability can produce. The ceiling, hit. The caller
    blocks the case and escalates — it does not approximate."""

    capped: List[str] = field(default_factory=list)
    """Needs dropped because the case hit `max_derived_requests`. Reported, never silent."""

    fired: List[str] = field(default_factory=list)
    """Rule names that produced at least one new request. The audit trail for "what changed"."""

    @property
    def anything(self) -> bool:
        return bool(self.new_requests or self.unmatched or self.capped)

    def to_doc(self) -> Dict[str, Any]:
        return {"new_requests": [r.to_doc() for r in self.new_requests],
                "unmatched": self.unmatched, "capped": self.capped, "fired": self.fired}


def derive(case: Case, contributions: Sequence[Contribution], rules: Sequence[Rule],
           registry: CapabilityRegistry, policy: CasePolicy) -> Derivation:
    """One monotone pass: evaluate every rule against current state, add what is missing.

    Pure — it builds `ContributionRequest` objects and returns them. It does not attach them to
    the case, does not dispatch, and does not touch the store. The caller decides whether to
    accept the derivation, which keeps "what would this do?" answerable without side effects.

    Ordinary rules go first. Fallback rules run only if that pass produced nothing at all — no
    request, no unmatched need, no cap — because a fallback rule answers "nothing deterministic
    applies, now what?" and that question is not open on a tick where something did apply.
    """
    view = CaseView(case, contributions)
    out = Derivation()

    # Idempotence is keyed on the REQUEST ID, not the need label. For every rule that does not
    # set `Need.request_id` these are the same string, so nothing changes — but it is what makes
    # the documented override work through rules: one case can legitimately want the same kind of
    # work twice (two vendors, two regions, the same question asked again a week later), and
    # keying on the label made that silently impossible.
    claimed = {r.id for r in case.requests}
    room = policy.max_derived_requests - len(case.requests)

    def run(subset: Sequence[Rule]) -> None:
        nonlocal room
        for rule in subset:
            produced = False
            for need in rule(view):
                if need.id_for() in claimed:
                    continue  # structural idempotence — a rule cannot double-request
                if room <= 0:
                    out.capped.append(need.need)
                    continue
                if need.external:
                    # No capability, no reach, nothing dispatched. It can only block.
                    out.new_requests.append(ContributionRequest(
                        id=need.id_for(), need=need.need, capability="",
                        expects=need.expects or ContributionKind.DECISION, spec=dict(need.spec),
                        status=RequestStatus.REQUESTED, warranted_by=rule.name,
                        optional=need.optional))
                    claimed.add(need.id_for())
                    room -= 1
                    produced = True
                    continue
                cap = registry.match(need.need)
                if cap is None:
                    out.unmatched.append(need.need)
                    claimed.add(need.id_for())  # not the same gap once per rule per tick
                    continue
                out.new_requests.append(ContributionRequest(
                    id=need.id_for(), need=need.need, capability=cap.name,
                    expects=need.expects or cap.emits, spec=dict(need.spec),
                    status=RequestStatus.REQUESTED, warranted_by=rule.name,
                    optional=need.optional))
                claimed.add(need.id_for())
                room -= 1
                produced = True
            if produced:
                out.fired.append(rule.name)

    run([r for r in rules if not r.fallback])
    if not out.anything:
        run([r for r in rules if r.fallback])
    return out


# --------------------------------------------------------------------------- common rules


def always(need: str, **spec: Any) -> Rule:
    """A need the case has from the moment it opens. Sugar for the non-dynamic majority."""
    return Rule(f"always:{need}",
                lambda view: [] if view.requested(need) else [Need(need, spec=spec)],
                description=f"{need} is always required")


def when_payload(need: str, *, given: str, key: str, equals: Any = True,
                 carry: Sequence[str] = (), name: str = "", **spec: Any) -> Rule:
    """`need` becomes warranted once `given`'s contribution reports `key == equals`.

    The common dynamic shape, as a helper, so the frequent case does not need a hand-written
    closure and therefore cannot get the idempotence guard subtly wrong.

    `carry` names keys to copy forward from `given`'s payload into the new request's spec. Almost
    every real dynamic rule needs this and it is easy to forget: the new worker runs in a fresh
    container and knows nothing except its spec, so a rule that warrants "check deliverability"
    without carrying forward *which client* produces a worker that dutifully queries nothing and
    reports zeros. That failure is quiet — the request is satisfied, the evidence is present, and
    it is empty — which is exactly the kind of thing this library exists to make loud, so the
    parameter is here rather than left to each caller to remember.
    """
    def predicate(view: CaseView) -> Sequence[Need]:
        if view.requested(need) or not view.satisfied(given):
            return []
        payload = view.payload(given)
        got = payload.get(key)
        if got == equals or (equals is True and bool(got)):
            carried = {k: payload[k] for k in carry if k in payload}
            return [Need(need, spec={"because": f"{given}.{key}={got!r}", **carried, **spec})]
        return []

    return Rule(name or f"{need}-if-{given}.{key}", predicate,
                description=f"{need} when {given} reports {key}={equals!r}")
