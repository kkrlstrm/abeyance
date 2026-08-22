"""When the facts change under a decision: does the case die, or does it work the problem?

The scenario every long-running system eventually hits and most get wrong. A human approves a
plan. New evidence arrives that contradicts what they were shown. Three things must all happen,
and the third is the one nobody builds:

  1. The old approval stops counting. Not "expires eventually" — stops counting, now.
  2. Nothing is executed on it.
  3. The case **derives a different course of action**, gathers what that needs, and asks for a
     fresh decision on the new plan.

Without (3) you have a safety mechanism that turns every changed fact into a dead case and a
human doing the thinking from scratch. With it, the durable object is doing the work.
"""
from __future__ import annotations

import pytest

from abeyance import (Actor, Approver, Capability, CapabilityRegistry, CaseLoop, CasePolicy,
                      CaseStatus, ContributionKind, Escalation, Need, RequestStatus, Rule,
                      always, when_payload)

ACTION = "launch-campaign"
STANDING = {"owner@example.com": (ACTION,)}


# --------------------------------------------------------------- the world-changed registry


@pytest.fixture
def recovery_registry():
    """Four workers: two for the original plan, two the case only reaches for if it has to."""
    return CapabilityRegistry([
        Capability(name="perf", image="postgres:16-alpine", produces=("campaign-performance",),
                   emits=ContributionKind.EVIDENCE, reach=("db-read",), app="w-data"),
        Capability(name="deliverability", image="postgres:16-alpine",
                   produces=("deliverability-check",), emits=ContributionKind.EVIDENCE,
                   reach=("db-read",), app="w-data"),
        Capability(name="segments", image="postgres:16-alpine", produces=("segment-analysis",),
                   emits=ContributionKind.EVIDENCE, reach=("db-read",), app="w-data"),
        Capability(name="designer", image="python:3.12-slim", produces=("campaign-design",),
                   emits=ContributionKind.RECOMMENDATION, reach=(), app="w-model"),
    ])


@pytest.fixture
def recovery_rules():
    """The chain. Each rule reads only evidence, and knows nothing about staleness or authority —
    that separation is what keeps rules from becoming a policy engine."""
    def redesign_needed(view):
        """The plan is off. Find out who still replies before proposing anything."""
        d = view.payload("deliverability-check")
        if view.satisfied("deliverability-check") and d.get("safe_to_resume") is False:
            if not view.requested("segment-analysis"):
                return [Need("segment-analysis",
                             spec={"client": d.get("client"),
                                   "because": f"bounce {d.get('worst_campaign_bounce_pct')}%"})]
        return []

    def design_the_alternative(view):
        seg = view.payload("segment-analysis")
        if view.satisfied("segment-analysis") and seg.get("viable_segments"):
            if not view.requested("campaign-design"):
                return [Need("campaign-design",
                             spec={"segments": seg["viable_segments"],
                                   "constraint": "warm-up safe, narrow"})]
        return []

    def the_plan_changed_so_reask(view):
        """A different plan needs a different yes. External: no container, it can only block."""
        if view.satisfied("campaign-design") and not view.requested("revised-decision"):
            return [Need("revised-decision", external=True,
                         request_id="revised-decision",
                         spec={"plan": view.payload("campaign-design").get("plan")})]
        return []

    return [always("campaign-performance"),
            when_payload("deliverability-check", given="campaign-performance",
                         key="gone_quiet", carry=("client",)),
            Rule("redesign-if-unsafe", redesign_needed),
            Rule("design-the-alternative", design_the_alternative),
            Rule("reask-because-plan-changed", the_plan_changed_so_reask)]


@pytest.fixture
def recovery(store, runner, clock, escalations, loop, recovery_registry, recovery_rules):
    return CaseLoop("recovery", store=store, registry=recovery_registry, rules=recovery_rules,
                    policy=CasePolicy(), runner=runner, approval=loop, clock=clock,
                    on_escalate=escalations.append)


def worker(cases, case_id, request_id, payload, *, name="w", kind=ContributionKind.EVIDENCE,
           scope=None, supersedes=""):
    return cases.contribute(case_id, kind=kind, actor=Actor.worker(name),
                            request_id=request_id, payload=payload, scope=scope or {},
                            supersedes=supersedes)


def human_approves(cases, loop, case_id, *, summary="go?", request_id="human-decision"):
    asked = cases.ask_humans(case_id, summary=summary, request_id=request_id,
                             approvers=[Approver("owner@example.com", role="owner")])
    loop.transport.receive(asked.id, "owner@example.com", "approve 1")
    for inbound in loop.read(asked.id):
        loop.record_from(asked.id, inbound)
    return cases.harvest(case_id, standing=STANDING)


# --------------------------------------------------------------- the staleness rule


def test_a_decision_records_what_it_rested_on(cases, loop):
    """Without this stamp there is nothing to invalidate against later."""
    case = cases.open(action=ACTION, subject_key="acme", needs=["campaign-performance"])
    cases.tick(case.id)
    ev = worker(cases, case.id, "campaign-performance", {"reply_rate": 4.0})
    cases.tick(case.id)

    decision = human_approves(cases, loop, case.id)[0]
    assert decision.dependencies == [ev.id], (
        "the decision has to name the evidence the person was actually shown")


def test_superseding_that_evidence_stops_the_decision_counting(cases, loop, clock):
    """The whole point. Their yes was about a picture that no longer exists."""
    case = cases.open(action=ACTION, subject_key="acme", needs=["campaign-performance"])
    cases.tick(case.id)
    first = worker(cases, case.id, "campaign-performance", {"reply_rate": 4.0, "safe": True})
    cases.tick(case.id)
    human_approves(cases, loop, case.id)
    assert cases.authority(case.id).granted is True

    clock.advance(hours=3)
    worker(cases, case.id, "campaign-performance-v2",
           {"reply_rate": 0.1, "safe": False}, supersedes=first.id)

    auth = cases.authority(case.id)
    assert auth.granted is False
    assert auth.stale_decisions, "the stale yes must be named, not silently dropped"
    assert "no longer apply" in auth.reason
    assert "human:owner@example.com" in auth.reason


def test_a_decision_survives_evidence_it_never_rested_on(cases, loop, clock):
    """The narrow version of invalidation, on purpose.

    If every new contribution invalidated every decision, no case with an active monitor could
    ever act. Only superseding something the decider was actually shown counts.
    """
    case = cases.open(action=ACTION, subject_key="acme", needs=["campaign-performance"])
    cases.tick(case.id)
    worker(cases, case.id, "campaign-performance", {"reply_rate": 4.0})
    cases.tick(case.id)
    human_approves(cases, loop, case.id)

    clock.advance(hours=1)
    # Something arrives that nobody's decision depended on, superseding nothing.
    worker(cases, case.id, "unrelated-note", {"fyi": True})
    assert cases.authority(case.id).granted is True


def test_execute_refuses_on_a_stale_decision_and_writes_nothing(cases, loop, clock, escalations):
    case = cases.open(action=ACTION, subject_key="acme", needs=["campaign-performance"])
    cases.tick(case.id)
    first = worker(cases, case.id, "campaign-performance", {"safe": True})
    cases.tick(case.id)
    human_approves(cases, loop, case.id)
    cases.tick(case.id)

    clock.advance(hours=3)
    worker(cases, case.id, "campaign-performance-v2", {"safe": False}, supersedes=first.id)

    out = cases.execute(case.id, lambda *a: pytest.fail("must not act on a stale yes"))
    assert out.written is False
    assert cases.get(case.id).status is not CaseStatus.EXECUTED
    assert Escalation.STALE_AUTHORITY in [e.kind for e in escalations]


def test_a_fresh_decision_on_the_new_evidence_authorizes_again(cases, loop, clock):
    """Recovery has to be possible, or the mechanism is just a way to kill cases."""
    case = cases.open(action=ACTION, subject_key="acme", needs=["campaign-performance"])
    cases.tick(case.id)
    first = worker(cases, case.id, "campaign-performance", {"safe": True})
    cases.tick(case.id)
    human_approves(cases, loop, case.id)

    clock.advance(hours=3)
    worker(cases, case.id, "campaign-performance-v2", {"safe": False}, supersedes=first.id)
    assert cases.authority(case.id).granted is False

    # A second question, and a second answer, about the world as it now is.
    cases.request(case.id, Need("second-look", external=True, request_id="second-look"))
    clock.advance(hours=1)
    human_approves(cases, loop, case.id, summary="revised plan ok?",
                   request_id="second-look")

    auth = cases.authority(case.id)
    assert auth.granted is True
    assert auth.authorization.granted_by == ["human:owner@example.com"]


# --------------------------------------------------------------- external needs


def test_harvesting_again_never_re_stamps_what_a_decider_was_shown(cases, loop, clock):
    """The most dangerous bug found in the live run, and it was silent.

    Harvest runs on every tick. Re-stamping an existing decision recomputed `dependencies`
    against whatever was live at that moment — so a decision that had correctly gone stale came
    back to life on the next tick, and the record was rewritten to claim the person had approved
    on the basis of evidence that did not exist when they answered.

    Two failures for the price of one: the guarantee stops working, and the audit trail lies
    about it.
    """
    case = cases.open(action=ACTION, subject_key="acme", needs=["campaign-performance"])
    cases.tick(case.id)
    first = worker(cases, case.id, "campaign-performance", {"safe": True})
    cases.tick(case.id)
    human_approves(cases, loop, case.id)

    decision = [c for c in cases.contributions(case.id)
                if c.kind is ContributionKind.DECISION][0]
    assert decision.dependencies == [first.id]

    # The world moves on, and the decision correctly goes stale.
    clock.advance(hours=2)
    revised = worker(cases, case.id, "campaign-performance", {"safe": False},
                     supersedes=first.id)
    assert cases.authority(case.id).stale_decisions == [decision.id]

    # Many more ticks, each of which harvests. The stale yes must stay stale.
    for _ in range(5):
        clock.advance(minutes=10)
        cases.tick(case.id, harvest_standing=STANDING)

    again = [c for c in cases.contributions(case.id)
             if c.kind is ContributionKind.DECISION][0]
    assert again.dependencies == [first.id], (
        "the record of what they were shown is a historical fact and must not move")
    assert revised.id not in again.dependencies, (
        "they never saw the revision — claiming otherwise falsifies the audit trail")
    assert cases.authority(case.id).stale_decisions == [decision.id]
    assert cases.authority(case.id).granted is False


def test_a_decider_who_answers_again_gets_a_fresh_stamp(cases, loop, clock):
    """The other side of it: a genuinely new answer is about the world as it now is."""
    case = cases.open(action=ACTION, subject_key="acme", needs=["campaign-performance"])
    cases.tick(case.id)
    first = worker(cases, case.id, "campaign-performance", {"safe": True})
    cases.tick(case.id)
    human_approves(cases, loop, case.id)

    clock.advance(hours=2)
    revised = worker(cases, case.id, "campaign-performance", {"safe": False},
                     supersedes=first.id)
    assert cases.authority(case.id).granted is False

    # They reply again on the same thread, having now seen the revision.
    clock.advance(minutes=5)
    proposal_id = cases.get(case.id).proposal_id
    loop.transport.receive(proposal_id, "owner@example.com", "approve 1")
    for inbound in loop.read(proposal_id):
        loop.record_from(proposal_id, inbound)
    cases.harvest(case.id, standing=STANDING)

    decision = [c for c in cases.contributions(case.id)
                if c.kind is ContributionKind.DECISION][0]
    assert revised.id in decision.dependencies
    assert first.id not in decision.dependencies
    assert cases.authority(case.id).granted is True


def test_a_case_stops_calling_itself_authorized_once_authority_is_withdrawn(cases, loop, clock):
    """Caught in the live run, not by a test.

    The case had gone AUTHORIZED, the evidence under it was superseded, authority evaluated
    False — and the status label stayed AUTHORIZED. Not BLOCKED (a rule may be deriving a
    replacement plan, which is healthy) but definitely not authorized either.
    """
    case = cases.open(action=ACTION, subject_key="acme", needs=["campaign-performance"])
    cases.tick(case.id)
    first = worker(cases, case.id, "campaign-performance", {"safe": True})
    cases.tick(case.id)
    human_approves(cases, loop, case.id)
    assert cases.tick(case.id)[0].status is CaseStatus.AUTHORIZED

    clock.advance(hours=2)
    worker(cases, case.id, "campaign-performance", {"safe": False}, supersedes=first.id)
    report = cases.tick(case.id)[0]

    assert report.authorized is False
    assert report.status is CaseStatus.OPEN
    assert report.actionable is False


def test_a_stale_decision_is_kept_in_the_record_but_out_of_the_basis(cases, loop, clock):
    """Without this, recovery is impossible and the failure is invisible.

    A stale decision is still *live* — nothing superseded it — so a naive basis includes it. Its
    dependency is dead, so `still_valid()` then fails on every envelope from then on: the fresh
    decision grants authority, and the commit-time check refuses forever. The case would look
    authorized and never execute.
    """
    case = cases.open(action=ACTION, subject_key="acme", needs=["campaign-performance"])
    cases.tick(case.id)
    first = worker(cases, case.id, "campaign-performance", {"safe": True})
    cases.tick(case.id)
    human_approves(cases, loop, case.id)
    stale_id = [c.id for c in cases.contributions(case.id)
                if c.kind is ContributionKind.DECISION][0]

    clock.advance(hours=2)
    worker(cases, case.id, "campaign-performance", {"safe": False}, supersedes=first.id)
    cases.request(case.id, Need("re-decide", external=True, request_id="re-decide"))
    clock.advance(hours=1)
    human_approves(cases, loop, case.id, summary="again?", request_id="re-decide")

    auth = cases.authority(case.id)
    assert auth.granted is True
    assert stale_id not in auth.authorization.basis, "a refused decision must not be a basis"
    assert stale_id in {c.id for c in cases.contributions(case.id)}, (
        "it stays in the record — 'what did we believe when we acted' needs the whole history")

    ok, why = auth.authorization.still_valid(cases.contributions(case.id), cases.clock.now())
    assert ok is True, f"the envelope must validate at commit time, got: {why}"
    assert cases.execute(case.id, lambda *a: {"ran": True}).written is True


def test_an_external_need_is_exempt_from_the_reach_ceiling(cases, runner):
    """It has no capability and is never dispatched — it can only block."""
    case = cases.open(action=ACTION, subject_key="acme",
                      needs=[Need("sign-off", external=True)])
    report = cases.tick(case.id)[0]

    req = cases.get(case.id).request("sign-off")
    assert req.capability == ""
    assert req.expects is ContributionKind.DECISION
    assert req.blocks_authorization is True
    assert runner.started == [], "an external need must never start a container"
    assert report.dispatch.of("external")


def test_a_rule_can_warrant_a_fresh_decision_without_minting_a_capability(make_cases, runner):
    """The gap this closed. A rule notices the plan changed; rules may not mint capabilities;
    so without `external` the case would proceed on a yes given about a different plan."""
    rule = Rule("reask", lambda v: [] if v.requested("re-decide")
                else [Need("re-decide", external=True)])
    cases = make_cases(rules=[rule])
    case = cases.open(action=ACTION, subject_key="acme")
    report = cases.tick(case.id)[0]

    assert [r.need for r in cases.get(case.id).requests] == ["re-decide"]
    assert report.derivation.unmatched == [], "no capability is looked for, so none is missing"
    assert runner.started == []


def test_a_non_external_need_with_no_capability_still_blocks_loudly(make_cases, escalations):
    """The exemption must not become a way to smuggle unreachable work past the ceiling."""
    rogue = Rule("rogue", lambda v: [Need("wire-money")])
    cases = make_cases(rules=[rogue])
    case = cases.open(action=ACTION, subject_key="acme")
    report = cases.tick(case.id)[0]

    assert report.derivation.unmatched == ["wire-money"]
    assert report.status is CaseStatus.BLOCKED
    assert Escalation.CAPABILITY_MISSING in [e.kind for e in report.escalations]


# --------------------------------------------------------------- the whole recovery


def test_the_case_works_the_problem_after_the_facts_change(recovery, loop, clock, runner):
    """End to end: approve → sharper check disagrees → refused → explore → new plan → re-approve.

    The assertions to read are the two `[r.need for r in ...]` lines. The case ends up having
    done four things nobody listed when it opened, each one warranted by what the previous one
    found.
    """
    case = recovery.open(action=ACTION, subject_key="northwind",
                         context={"client": "Northwind"})
    assert case.requests == [], "nothing planned at open time"

    # --- the original plan -------------------------------------------------
    recovery.tick(case.id)
    worker(recovery, case.id, "campaign-performance",
           {"client": "Northwind", "gone_quiet": True, "days_since_last_send": 17},
           name="perf")
    recovery.tick(case.id)

    # The coarse check says it is safe. This is what the human will be shown.
    coarse = worker(recovery, case.id, "deliverability-check",
                    {"client": "Northwind", "pooled_bounce_pct": 1.04, "safe_to_resume": True},
                    name="deliverability", scope={"max_leads": 500})
    recovery.tick(case.id)

    human_approves(recovery, loop, case.id, summary="Launch the re-engagement wave")
    report = recovery.tick(case.id)[0]
    assert report.authorized is True
    assert report.authority.authorization.scope["max_leads"] == 500

    # --- a sharper check disagrees ----------------------------------------
    clock.advance(hours=2)
    # Same request, a sharper reading, superseding the coarse one. This is the natural shape:
    # a monitor re-runs the same check and disagrees with itself.
    worker(recovery, case.id, "deliverability-check",
           {"client": "Northwind", "pooled_bounce_pct": 1.04,
            "worst_campaign_bounce_pct": 3.29, "safe_to_resume": False},
           name="deliverability", scope={"max_leads": 150, "warm_up_required": True},
           supersedes=coarse.id)

    blocked = recovery.execute(case.id, lambda *a: pytest.fail("must not launch the old plan"))
    assert blocked.written is False
    assert recovery.authority(case.id).stale_decisions

    # --- the case works the problem ---------------------------------------
    t = recovery.tick(case.id)[0]
    assert "redesign-if-unsafe" in t.derivation.fired
    assert recovery.get(case.id).request("segment-analysis") is not None

    worker(recovery, case.id, "segment-analysis",
           {"viable_segments": [{"title": "Director", "leads": 461, "reply_pct": 8.68},
                                {"title": "Registrar", "leads": 475, "reply_pct": 6.95}]},
           name="segments")
    t = recovery.tick(case.id)[0]
    assert "design-the-alternative" in t.derivation.fired

    worker(recovery, case.id, "campaign-design",
           {"plan": "warm-up wave to Director + Registrar only",
            "target_leads": 150,
            # It is a recommendation, so it may say whatever it likes about approval.
            "verdict": "approved", "authorized": True},
           name="designer", kind=ContributionKind.RECOMMENDATION,
           scope={"max_leads": 150, "warm_up_required": True})
    t = recovery.tick(case.id)[0]
    assert "reask-because-plan-changed" in t.derivation.fired

    needs = [r.need for r in recovery.get(case.id).requests]
    assert needs == ["campaign-performance",   # planned by a rule at open time
                     "deliverability-check",   # warranted: the client had gone quiet
                     "human-decision",         # the first ask
                     "segment-analysis",       # warranted: the sharper check said unsafe
                     "campaign-design",        # warranted: viable segments existed
                     "revised-decision"]       # warranted: the plan is no longer what was approved
    assert t.authorized is False, "a new plan needs a new yes, and the designer's is not one"
    assert t.authority.ignored_claims, "the designer's self-approval is refused and reported"

    # --- a fresh decision on the new plan ---------------------------------
    clock.advance(hours=1)
    human_approves(recovery, loop, case.id, summary="Revised: warm-up wave, 2 segments",
                   request_id="revised-decision")
    final = recovery.tick(case.id)[0]

    assert final.authorized is True
    scope = final.authority.authorization.scope
    assert scope["max_leads"] == 150, "the narrowed plan, not the original 500"
    assert scope["warm_up_required"] is True

    ran = {}
    out = recovery.execute(case.id, lambda c, a, cs: ran.update(a.scope) or {"launched": True})
    assert out.written is True
    assert ran["max_leads"] == 150, (
        "the executor acted on the revised envelope, not the one the human first approved")


def test_the_history_explains_every_derived_step(recovery, loop, clock):
    """Six months later, "why did a segment analysis happen?" must be answerable from the row."""
    case = recovery.open(action=ACTION, subject_key="northwind", context={"client": "Northwind"})
    recovery.tick(case.id)
    worker(recovery, case.id, "campaign-performance",
           {"client": "Northwind", "gone_quiet": True}, name="perf")
    recovery.tick(case.id)
    coarse = worker(recovery, case.id, "deliverability-check",
                    {"client": "Northwind", "safe_to_resume": True}, name="deliverability")
    recovery.tick(case.id)
    human_approves(recovery, loop, case.id)
    recovery.tick(case.id)
    clock.advance(hours=2)
    worker(recovery, case.id, "deliverability-check",
           {"client": "Northwind", "safe_to_resume": False, "worst_campaign_bounce_pct": 3.29},
           name="deliverability", supersedes=coarse.id)
    recovery.tick(case.id)

    warranted = {h["request"]: h["by"] for h in recovery.get(case.id).history
                 if h["event"] == "warranted"}
    assert warranted["deliverability-check"] == "deliverability-check-if-campaign-performance.gone_quiet"
    assert warranted["segment-analysis"] == "redesign-if-unsafe"

    seg = recovery.get(case.id).request("segment-analysis")
    assert "3.29" in seg.spec["because"], (
        "the request carries the number that triggered it, not just a rule name")
