"""The authority math: can this case be acted on, by whose decision, within what limits.

Sibling of `verdict.py`, and held to the same standard — a pure function of state, no clock
beyond one passed in, no store, no transport, readable in one screen. "Who was allowed to do
this" is the question asked after an incident, and it should be answerable by reading this
file.

The one structural guarantee, and the reason the case layer exists at all:

    **Authority is derived from the contribution's TYPE and the actor's STANDING.
    It is never derived from the payload.**

`counts_as_decision()` looks at three things — kind, actor kind, standing — and never at
`payload`. So a model that emits a RECOMMENDATION whose payload reads
`{"verdict": "approved", "authorized": true, "proceed": true}` has said something with exactly
zero authority, and no amount of confident phrasing changes that. This is the property that a
shared document, a message bus, or an untyped blackboard cannot give you: there, "approved" is
just content, and whoever reads it next decides how much weight it carries.

Two guards exist for the same thing, on purpose. `Contribution.__post_init__` refuses to
*construct* a worker DECISION, and this module refuses to *count* one. The second is the
authoritative one: contributions are written by any process that can reach the store,
including a shell script running raw SQL, so a check that only runs in our constructor is a
check an attacker or a bug simply routes around. The expensive guard goes where the decision
is made.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .models import (ActorKind, Authorization, Case, Contribution, ContributionKind)
from .policy import CasePolicy

DECIDING_ACTORS = (ActorKind.HUMAN, ActorKind.POLICY)
"""Only a human, or a rule acting under explicitly delegated authority, can decide. A worker
never can — and `ActorKind.POLICY` is named separately from HUMAN so that a delegated
auto-decision stays auditable as a delegation instead of being disguised as a person."""

REJECT_VALUES = ("reject", "rejected", "no", "deny", "denied", "veto")
"""Read from a DECISION's payload only — after standing has already been established. This is
not authority derived from wording: the actor's right to decide is settled before we look at
what they decided, and this only reads *which way* an authorized decider went."""


# --------------------------------------------------------------------------- the guarantee


def counts_as_decision(c: Contribution, action: str) -> bool:
    """Does this contribution carry authority over `action`?

    Three checks, none of which touch `c.payload`. If you ever find yourself wanting to add a
    fourth that does, that is the moment this design is being dismantled.
    """
    if c.kind is not ContributionKind.DECISION:
        return False
    if c.actor.kind not in DECIDING_ACTORS:
        return False
    return action in c.actor.standing or "*" in c.actor.standing


def decision_direction(c: Contribution) -> str:
    """`"approve"` or `"reject"`, for a contribution that has *already* cleared standing."""
    for key in ("decision", "verdict", "direction", "answer"):
        v = c.payload.get(key)
        if isinstance(v, str) and v.strip().lower() in REJECT_VALUES:
            return "reject"
    if c.payload.get("approved") is False or c.payload.get("rejected") is True:
        return "reject"
    return "approve"


# --------------------------------------------------------------------------- result


@dataclass
class Authority:
    """The answer to "may this case be acted on now?", with the reasoning attached.

    `granted` is the boolean; everything else is why. A refusal that cannot explain itself
    gets overridden by the next person in a hurry, so the explanation is part of the value.
    """

    granted: bool
    reason: str = ""
    authorization: Optional[Authorization] = None

    missing_requests: List[str] = field(default_factory=list)
    """Request ids still outstanding or failed. Non-empty means "not yet", not "no"."""

    conflicting: List[str] = field(default_factory=list)
    """Actor ids whose decisions disagree. Non-empty is the case the machine must not
    resolve — the same principle as `Verdict.DEADLOCKED` in the approval layer."""

    ignored_claims: List[str] = field(default_factory=list)
    """Contribution ids that *asserted* authority and were not counted — a worker's
    recommendation that said "approved", a decision from an actor without standing for this
    action. Surfaced rather than dropped: something trying and failing to grant itself
    authority is exactly what a reviewer wants to see, and silence here would make the
    guarantee unobservable.
    """

    stale_decisions: List[str] = field(default_factory=list)
    """Decisions that had standing and no longer count, because a fact they rested on has been
    superseded.

    This is the difference between "nobody has decided" and "somebody decided, about a world
    that has since changed" — and the second needs a *new* decision, not patience. Without it, a
    human's yes silently carries forward onto evidence they never saw, which is the most
    dangerous thing a long-running case can do: the approval is real, the audit trail shows a
    genuine human approving, and what they approved is not what happens.
    """

    def to_doc(self) -> Dict[str, object]:
        return {"granted": self.granted, "reason": self.reason,
                "authorization": self.authorization.to_doc() if self.authorization else None,
                "missing_requests": self.missing_requests, "conflicting": self.conflicting,
                "ignored_claims": self.ignored_claims,
                "stale_decisions": self.stale_decisions}


# --------------------------------------------------------------------------- the rules


def authorize(case: Case, contributions: Sequence[Contribution], policy: CasePolicy,
              *, now: int) -> Authority:
    """Derive authority from accumulated case state. In order, and the order is the argument.

    1. **Every blocking request satisfied.** Failed or outstanding evidence blocks. "We could
       not gather it, so we proceeded" is the failure this layer exists to prevent.
    2. **A decision with standing exists, and it is still about this world.** Recommendations and
       evidence never suffice. Neither does a decision whose supporting facts have since been
       superseded — that person decided about something else.
    3. **No conflict between deciders.** Two people with standing who disagree is escalated,
       never resolved by recency or majority.
    4. **Not rejected.** A reject from a stander with standing is a decision, and it is final.
    5. **Scope is the intersection** of what every counted contribution asserted — narrowing
       only, never widening.
    """
    live = active(contributions)
    superseded = {c.supersedes for c in contributions if c.supersedes}
    ignored = [c.id for c in live if _claims_authority_without_standing(c, case.action)]

    # 1 — outstanding or failed work
    blocking = [r.id for r in case.requests if r.blocks_authorization]
    if blocking:
        return Authority(granted=False, missing_requests=blocking, ignored_claims=ignored,
                         reason=f"{len(blocking)} request(s) not satisfied: {blocking}")

    # 2 — a real decision, about the world as it now stands.
    #
    # A decision records the facts it rested on (`dependencies`, stamped at harvest time). If any
    # of those has since been superseded, the decider approved a different picture than the one
    # in front of us, and their yes does not transfer. This is the narrow, one-hop version of
    # dependency-aware invalidation — enough for the real case, and deliberately not a general
    # truth-maintenance system.
    all_decisions = [c for c in live if counts_as_decision(c, case.action)]
    stale = [c for c in all_decisions if any(d in superseded for d in c.dependencies)]
    decisions = [c for c in all_decisions if c not in stale]
    stale_ids = [c.id for c in stale]

    if not decisions:
        if stale:
            return Authority(
                granted=False, ignored_claims=ignored,
                stale_decisions=stale_ids,
                reason=(f"{len(stale)} decision(s) rested on evidence that has since been "
                        f"superseded, so they no longer apply — a fresh decision is needed: "
                        f"{sorted(c.actor.id for c in stale)}"))
        return Authority(
            granted=False, ignored_claims=ignored, stale_decisions=stale_ids,
            reason=(f"no DECISION with standing for {case.action!r}"
                    + (f"; {len(ignored)} contribution(s) asserted authority without it"
                       if ignored else "")))

    if policy.required_standing:
        held = {s for c in decisions for s in c.actor.standing}
        short = [s for s in policy.required_standing if s not in held and "*" not in held]
        if short:
            return Authority(granted=False, ignored_claims=ignored, stale_decisions=stale_ids,
                             reason=f"policy requires standing {short} and no decision carries it")

    if len(decisions) < policy.min_deciders:
        return Authority(
            granted=False, ignored_claims=ignored, stale_decisions=stale_ids,
            reason=(f"{len(decisions)} decision(s), policy requires {policy.min_deciders} — "
                    "waiting rather than acting on a partial panel"))

    # 3 — disagreement between people who both had the right to decide
    directions = {c.actor.id: decision_direction(c) for c in decisions}
    if len({*directions.values()}) > 1:
        return Authority(
            granted=False, conflicting=sorted(directions), ignored_claims=ignored,
            stale_decisions=stale_ids,
            reason=("deciders disagree — nothing is authorized and a human must break the "
                    f"tie: {directions}"))

    # 4 — a decision against
    if all(d == "reject" for d in directions.values()):
        return Authority(granted=False, ignored_claims=ignored, stale_decisions=stale_ids,
                         reason=f"decided against by {sorted(directions)}")

    # 5 — the envelope.
    #
    # The basis is every live contribution that was actually COUNTED. Two exclusions, and both
    # are load-bearing:
    #
    #   Superseded contributions are already out (`live`). Authority rests on the case as it
    #   stands, so revising any fact it rests on invalidates the stored envelope and forces a
    #   re-derivation. That costs one tick and fails closed, which is the right direction.
    #
    #   Stale decisions are out too. They are still live — nothing superseded them — but they
    #   were refused above, and a refused decision must not end up in the basis. If it did, its
    #   dead dependency would invalidate every envelope from then on, and a case could never
    #   recover from a changed fact: the fresh decision would grant authority and the stored
    #   envelope would fail its commit-time check forever.
    counted = [c for c in live if c not in stale]
    basis = sorted(c.id for c in counted)
    ttl = policy.authorization_ttl_seconds
    auth = Authorization(
        case_id=case.id, action=case.action, scope=narrow_scope(counted),
        granted_epoch=now, expires_epoch=(now + ttl) if ttl else 0,
        basis=basis, granted_by=sorted(directions))
    return Authority(granted=True, authorization=auth, ignored_claims=ignored,
                     stale_decisions=stale_ids,
                     reason=f"authorized by {sorted(directions)} on {len(basis)} contribution(s)")


APPROVAL_WORDS = ("approve", "approved", "approves", "authorize", "authorized", "cleared",
                  "clear", "yes", "ok", "okay", "proceed", "go")
"""Payload values that read as a yes. Used only by the reporting heuristic below."""


def _claims_authority_without_standing(c: Contribution, action: str) -> bool:
    """Did this contribution try to confer authority it does not have?

    Only used for reporting. A recommendation saying "approved" is not an error — it is a
    recommendation doing its job — but a reviewer should be able to see that the system read it
    and declined to treat it as permission.

    It asks whether the payload says *yes*, not merely whether it uses an authority-shaped word.
    The first version flagged any payload carrying a `verdict` key, so a QA worker reporting
    `verdict: "blocked"` — a fact, and a refusal at that — was reported as having tried to grant
    itself permission, on every tick. Two things wrong with that, the second worse than the first:
    it read the payload to decide something, which is exactly what this module exists not to do;
    and a channel that exists to make the guarantee observable becomes worthless if it cries wolf
    hourly, because people learn to filter it and then miss the real one.
    """
    if counts_as_decision(c, action):
        return False
    if c.kind is ContributionKind.DECISION:
        return True  # a decision from an actor without standing for this action, whatever it says
    for key in ("approved", "authorized"):
        got = c.payload.get(key)
        if got is True or str(got).strip().lower() in APPROVAL_WORDS:
            return True
    for key in ("decision", "verdict"):
        if str(c.payload.get(key, "")).strip().lower() in APPROVAL_WORDS:
            return True
    return False


# --------------------------------------------------------------------------- helpers


def active(contributions: Sequence[Contribution]) -> List[Contribution]:
    """Contributions that still count: superseded ones stay in the record and stop counting.

    The record keeps everything — "what did we believe when we acted" is unanswerable
    otherwise — but a superseded fact must not vote.
    """
    superseded = {c.supersedes for c in contributions if c.supersedes}
    return [c for c in contributions if c.id not in superseded]


def narrow_scope(contributions: Sequence[Contribution]) -> Dict[str, object]:
    """Intersect the scope limits every contributor asserted. Narrowing only.

    Numeric limits take the minimum, booleans take logical AND, lists intersect, and a
    conflicting scalar collapses to the *stricter* reading by keeping the first and recording
    the disagreement. A scope that could widen by adding another contribution would be an
    escalation-of-privilege primitive, so the merge is deliberately one-directional.
    """
    out: Dict[str, object] = {}
    conflicts: Dict[str, list] = {}
    for c in sorted(contributions, key=lambda x: (x.created_epoch, x.id)):
        for k, v in (c.scope or {}).items():
            if k not in out:
                out[k] = v
                continue
            cur = out[k]
            if isinstance(cur, bool) or isinstance(v, bool):
                out[k] = bool(cur) and bool(v)
            elif isinstance(cur, (int, float)) and isinstance(v, (int, float)):
                out[k] = min(cur, v)
            elif isinstance(cur, list) and isinstance(v, list):
                out[k] = [x for x in cur if x in v]
            elif cur != v:
                conflicts.setdefault(k, [cur]).append(v)
    if conflicts:
        out["_scope_conflicts"] = conflicts
    return out


def summarize(case: Case, contributions: Sequence[Contribution]) -> Dict[str, object]:
    """A flat, printable picture of where a case stands. For the CLI and for humans."""
    live = active(contributions)
    by_kind: Dict[str, int] = {}
    for c in live:
        by_kind[c.kind.value] = by_kind.get(c.kind.value, 0) + 1
    return {
        "case": case.id, "action": case.action, "status": case.status.value,
        "contributions": by_kind,
        "requests": {r.id: r.status.value for r in case.requests},
        "outstanding": [r.id for r in case.outstanding],
        "blocking": [r.id for r in case.blocking],
        "deciders": sorted({c.actor.id for c in live
                            if counts_as_decision(c, case.action)}),
    }
