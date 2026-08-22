"""The disposable planner, and — mostly — the limits on it.

Two thirds of this file is about the planner *not* doing something, and that is the right
proportion. The interesting failure of a planning agent is never that it fails to think of
something; it is that it thinks of one more thing, forever, each round individually defensible
and the case never closing. So the tests that matter here are the ones that pin the budget, the
standstill guards, and the fact that running out of budget ends in a person rather than a stall.

`test_a_planner_cannot_keep_a_case_open_forever` is the one to read first: it drives forty ticks
of a case whose planner asks for work every single time, and asserts that the case is in front of
a human anyway.
"""
from __future__ import annotations

import json

import pytest

from abeyance import (ASSESS_BLOCKED, ASSESS_READY, ASSESS_WORK, Actor, Capability,
                      CapabilityRegistry, CasePolicy, CaseStatus, ContributionKind, Escalation,
                      HUMAN_DECISION, Need, PLAN_NEED, PLAN_TAG, PlanBudget, PlanProposal, Planner,
                      RequestStatus, RunState, Rule, parse_plan, planner_capability)
from abeyance.errors import ConfigurationError
from abeyance.planner import planner_for
from abeyance.warrant import CaseView

STANDING = {"boss@example.com": ("launch-campaign",)}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def registry():
    """Shadows the conftest registry so every case loop here has a planner to dispatch."""
    return CapabilityRegistry([
        Capability(name="db-evidence", image="postgres:16-alpine",
                   produces=("campaign-performance",), emits=ContributionKind.EVIDENCE,
                   reach=("db-read",), app="workers-readonly", timeout_seconds=120,
                   description="Sends, bounces and last-send date for one client."),
        Capability(name="deliverability", image="python:3.12-slim",
                   produces=("deliverability-check",), emits=ContributionKind.EVIDENCE,
                   reach=("db-read",), app="workers-readonly", timeout_seconds=120,
                   description="Bounce rate and spam complaints over the last 30 days."),
        Capability(name="fit-scorer", image="python:3.12-slim",
                   produces=("fit-score",), emits=ContributionKind.RECOMMENDATION,
                   reach=("public-internet",), app="workers-model", timeout_seconds=300,
                   description="Scores how well this list fits the offer."),
        Capability(name="list-check", image="python:3.12-slim",
                   produces=("list-readiness",), emits=ContributionKind.EVIDENCE,
                   reach=("db-read",), app="workers-readonly", timeout_seconds=60,
                   description="Is the list finished and de-duplicated?"),
        planner_capability(image="ghcr.io/example/planner@sha256:abc", app="workers-model"),
    ])


@pytest.fixture
def planner(registry):
    return Planner(registry)


@pytest.fixture
def planned(make_cases, planner):
    """A case loop whose only rules are the planner's. Nothing deterministic to hide behind."""
    return make_cases(rules=planner.rules())


def evidence(cases, case_id, request_id, payload, *, name="db-evidence", scope=None):
    return cases.contribute(case_id, kind=ContributionKind.EVIDENCE, actor=Actor.worker(name),
                            request_id=request_id, payload=payload, scope=scope or {},
                            provenance={"machine": "mem-0001"})


def plan_lands(cases, case_id, *, request_id=PLAN_NEED, assessment=ASSESS_WORK, rationale="",
               proposals=(), missing=(), payload=None):
    """What the planner worker does: one RECOMMENDATION, then it is gone."""
    body = payload if payload is not None else {
        "assessment": assessment,
        "rationale": rationale or "a plan",
        "proposals": [p if isinstance(p, dict) else p.to_doc() for p in proposals],
        "missing_capabilities": list(missing),
    }
    return cases.contribute(case_id, kind=ContributionKind.RECOMMENDATION,
                            actor=Actor.worker("planner"), request_id=request_id,
                            summary="plan", payload=body,
                            provenance={"machine": "mem-plan"})


def proposal(need, *, why="because", changes="if it is bad we cut the wave", spec=None):
    return PlanProposal(need=need, why=why, changes_decision_if=changes, spec=spec or {})


def needs_of(cases, case_id):
    return [r.need for r in cases.get(case_id).requests]


def view_of(cases, case_id):
    return CaseView(cases.get(case_id), cases.contributions(case_id))


def open_and_gather(cases, *, needs=("campaign-performance",), payload=None):
    """Open a case, run its opening evidence to ground, and leave it at a standstill."""
    case = cases.open(action="launch-campaign", subject_key="acme", needs=list(needs))
    cases.tick(case.id)
    for need in needs:
        evidence(cases, case.id, need, payload or {"sent": 14852, "bounce_pct": 1.04})
    return case


# --------------------------------------------------------------------------- when it fires


def test_the_planner_runs_only_when_nothing_deterministic_applies(make_cases, planner, runner):
    """A rule that knows the answer always beats a planner that would have to work it out."""
    deterministic = Rule("always-fit", lambda v: ([] if v.requested("fit-score")
                                                  else [Need("fit-score")]))
    cases = make_cases(rules=[deterministic, *planner.rules()])
    case = open_and_gather(cases)

    cases.tick(case.id)

    assert "fit-score" in needs_of(cases, case.id)
    assert PLAN_NEED not in needs_of(cases, case.id), (
        "a deterministic rule warranted work, so no model should have been called")


def test_the_planner_runs_on_the_next_standstill(make_cases, planner):
    """...and it is the *same* tick's worth of patience, not a permanent veto."""
    deterministic = Rule("always-fit", lambda v: ([] if v.requested("fit-score")
                                                  else [Need("fit-score")]))
    cases = make_cases(rules=[deterministic, *planner.rules()])
    case = open_and_gather(cases)
    cases.tick(case.id)                                   # deterministic rule fires
    evidence(cases, case.id, "fit-score", {"score": 71}, name="fit-scorer")

    cases.tick(case.id)                                   # nothing left to derive

    assert PLAN_NEED in needs_of(cases, case.id)


def test_no_planning_while_a_worker_is_in_flight(planned):
    case = planned.open(action="launch-campaign", subject_key="acme",
                        needs=["campaign-performance"])

    planned.tick(case.id)   # dispatches the evidence worker; nothing has come back

    assert PLAN_NEED not in needs_of(planned, case.id)
    assert "work is in flight" in planner_for(planned.rules).status(
        view_of(planned, case.id))["why_not"]


def test_no_planning_around_evidence_that_could_not_be_gathered(make_cases, planner, runner,
                                                                clock):
    """The one guard that is about correctness rather than cost.

    A failed request is a hole in the record. Planning past it produces a case that proceeded on
    the evidence it happened to have — dressed up, this time, as initiative.
    """
    cases = make_cases(rules=planner.rules(), policy=CasePolicy(max_attempts=1))
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    for _ in range(3):
        cases.tick(case.id)
        ref = cases.get(case.id).request("campaign-performance").machine_ref
        if ref:
            runner.set_state(ref, RunState.GONE)
        clock.advance(hours=1)

    assert cases.get(case.id).request("campaign-performance").status is RequestStatus.FAILED
    assert PLAN_NEED not in needs_of(cases, case.id)
    assert "blocks authorization" in planner.status(view_of(cases, case.id))["why_not"]


def test_no_planning_while_a_human_is_being_asked(planned, planner):
    case = open_and_gather(planned)
    planned.tick(case.id)                                  # round 1 asked for
    plan_lands(planned, case.id, assessment=ASSESS_READY)
    planned.tick(case.id)                                  # -> human-decision warranted

    assert HUMAN_DECISION in needs_of(planned, case.id)
    before = len([r for r in planned.get(case.id).requests if r.need == PLAN_NEED])
    for _ in range(5):
        planned.tick(case.id)
    after = len([r for r in planned.get(case.id).requests if r.need == PLAN_NEED])

    assert after == before, "a pending human decision is not a standstill to plan through"
    assert "human decision is outstanding" in planner.status(view_of(planned, case.id))["why_not"]


def test_no_planning_once_somebody_has_decided(planned, planner):
    case = open_and_gather(planned)
    planned.policy_decision(case.id, rule="owner", standing=("launch-campaign",), approve=True)

    for _ in range(3):
        planned.tick(case.id)

    assert PLAN_NEED not in needs_of(planned, case.id)
    assert "already decided" in planner.status(view_of(planned, case.id))["why_not"]


# --------------------------------------------------------------------------- adopting a plan


def test_a_plan_becomes_dispatched_work(planned, runner):
    case = open_and_gather(planned)
    planned.tick(case.id)                                  # asks for a plan
    assert planned.get(case.id).request(PLAN_NEED).capability == "planner"

    plan_lands(planned, case.id, proposals=[proposal("deliverability-check")])
    planned.tick(case.id)

    req = planned.get(case.id).request("deliverability-check")
    assert req is not None and req.status is RequestStatus.DISPATCHED
    assert req.warranted_by == "planner:adopt"
    assert req.spec["changes_decision_if"], "decision-relevance is carried onto the request"
    assert req.spec[PLAN_TAG], "every planned request names the plan that asked for it"


def test_the_planner_gets_the_whole_brief_and_none_of_the_credentials(planned, runner):
    case = open_and_gather(planned)
    planned.tick(case.id)

    spec = json.loads(runner.env_of(
        planned.get(case.id).request(PLAN_NEED).machine_ref)["ABEYANCE_SPEC"])

    assert spec["case"]["action"] == "launch-campaign"
    assert spec["case"]["evidence"][0]["payload"]["bounce_pct"] == 1.04
    assert {c["need"] for c in spec["capabilities"]} == {
        "campaign-performance", "deliverability-check", "fit-score", "list-readiness"}
    assert PLAN_NEED not in {c["need"] for c in spec["capabilities"]}, (
        "advertising more planning to the planner is asking for a loop")
    assert "image" not in json.dumps(spec["capabilities"]), (
        "the planner picks questions, not containers")
    assert spec["budget"]["rounds_left"] == 2
    assert "changes_decision_if" in spec["instructions"]


def test_a_plan_is_adopted_once_however_many_ticks_run(planned):
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, proposals=[proposal("deliverability-check")])

    for _ in range(4):
        planned.tick(case.id)

    assert needs_of(planned, case.id).count("deliverability-check") == 1


def test_ready_for_decision_puts_it_in_front_of_a_person_and_spends_nothing(planned):
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, assessment=ASSESS_READY, rationale="1.04% bounce is fine",
               proposals=[proposal("deliverability-check")])   # contradicts itself on purpose

    planned.tick(case.id)

    needs = needs_of(planned, case.id)
    assert HUMAN_DECISION in needs
    assert "deliverability-check" not in needs, (
        "ready-for-decision wins the contradiction — the reading that closes the case")
    req = planned.get(case.id).request(HUMAN_DECISION)
    assert req.capability == "" and req.expects is ContributionKind.DECISION


def test_a_plan_with_nothing_usable_goes_straight_to_a_person(planned):
    """No retry, no second opinion, no burning another round on the same standstill."""
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, proposals=[proposal("deliverability-check", changes="")])

    planned.tick(case.id)

    assert "deliverability-check" not in needs_of(planned, case.id)
    assert HUMAN_DECISION in needs_of(planned, case.id)
    assert "proposed nothing this case can act on" in (
        planned.get(case.id).request(HUMAN_DECISION).spec["because"])


# --------------------------------------------------------------------------- the budget


def test_the_round_budget_is_hard_and_ends_in_a_person(make_cases, registry):
    """The failure this module exists to prevent, run at it directly."""
    planner = Planner(registry, budget=PlanBudget(max_plans=2, max_needs_per_plan=1,
                                                  max_planned_needs=4))
    cases = make_cases(rules=planner.rules())
    case = open_and_gather(cases)

    # Two rounds, each asking for one more piece of work, each answered.
    cases.tick(case.id)
    plan_lands(cases, case.id, proposals=[proposal("deliverability-check")])
    cases.tick(case.id)
    evidence(cases, case.id, "deliverability-check", {"bounce_pct": 0.9}, name="deliverability")
    cases.tick(case.id)
    assert [r.id for r in cases.get(case.id).requests if r.need == PLAN_NEED] == [
        PLAN_NEED, f"{PLAN_NEED}#2"]
    plan_lands(cases, case.id, request_id=f"{PLAN_NEED}#2",
               proposals=[proposal("fit-score")])
    cases.tick(case.id)
    evidence(cases, case.id, "fit-score", {"score": 71}, name="fit-scorer")

    for _ in range(6):
        cases.tick(case.id)

    rounds = [r.id for r in cases.get(case.id).requests if r.need == PLAN_NEED]
    assert len(rounds) == 2, "the third round must never be asked for"
    assert HUMAN_DECISION in needs_of(cases, case.id)
    assert "budget is spent" in cases.get(case.id).request(HUMAN_DECISION).spec["because"]


def test_the_work_budget_is_hard_across_rounds(make_cases, registry):
    planner = Planner(registry, budget=PlanBudget(max_plans=3, max_needs_per_plan=2,
                                                  max_planned_needs=1))
    cases = make_cases(rules=planner.rules())
    case = open_and_gather(cases)
    cases.tick(case.id)

    plan_lands(cases, case.id, proposals=[proposal("deliverability-check"),
                                          proposal("fit-score")])
    cases.tick(case.id)

    planned_needs = [r.need for r in cases.get(case.id).requests
                     if (r.spec or {}).get(PLAN_TAG) and r.capability]
    assert planned_needs == ["deliverability-check"], "one unit of work was all it had"


def test_only_max_needs_per_plan_survive_one_round(planned, planner):
    """Three good proposals, two slots. The third is dropped loudly, with the reason on it."""
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, proposals=[proposal("deliverability-check"),
                                            proposal("fit-score"),
                                            proposal("list-readiness"),
                                            proposal("campaign-performance")])

    review = planner.review(view_of(planned, case.id))
    planned.tick(case.id)

    assert [n.need for n in review.accepted] == ["deliverability-check", "fit-score"]
    assert ("list-readiness", "over-max_needs_per_plan") in review.rejected
    assert ("campaign-performance", "already-requested-on-this-case") in review.rejected
    assert "list-readiness" not in needs_of(planned, case.id)


def test_a_proposal_that_cannot_say_what_it_would_change_is_dropped(planner, planned):
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, proposals=[
        proposal("deliverability-check", changes="   "),
        proposal("fit-score", changes="if the fit is under 40 we do not send at all")])

    planned.tick(case.id)

    needs = needs_of(planned, case.id)
    assert "fit-score" in needs
    assert "deliverability-check" not in needs


def test_the_planner_cannot_propose_more_planning(planner, planned):
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, proposals=[proposal(PLAN_NEED)])

    planned.tick(case.id)

    review = planner.review(view_of(planned, case.id))
    assert ("plan-next-step", "planner-may-not-plan-more-planning") in review.rejected
    assert len([r for r in planned.get(case.id).requests if r.need == PLAN_NEED]) == 1


def test_a_planner_cannot_keep_a_case_open_forever(make_cases, registry, clock):
    """Forty ticks against a planner that always wants one more thing.

    Every round it proposes work, and it never says the case is ready. The budget — not the
    planner's restraint — is what ends it, and what it ends in is a question for a person.
    """
    planner = Planner(registry, budget=PlanBudget(max_plans=2, max_needs_per_plan=2,
                                                  max_planned_needs=3))
    cases = make_cases(rules=planner.rules())
    case = open_and_gather(cases)
    greedy = [proposal("deliverability-check"), proposal("fit-score"),
              proposal("campaign-performance")]

    for _ in range(40):
        cases.tick(case.id)
        current = cases.get(case.id)
        for req in current.requests:
            if req.status is RequestStatus.DISPATCHED and req.capability:
                if req.need == PLAN_NEED:
                    plan_lands(cases, case.id, request_id=req.id, proposals=greedy)
                else:
                    evidence(cases, case.id, req.id, {"ok": True}, name=req.capability)
        clock.advance(minutes=5)

    final = cases.get(case.id)
    assert len([r for r in final.requests if r.need == PLAN_NEED]) == 2
    assert len([r for r in final.requests if (r.spec or {}).get(PLAN_TAG) and r.capability]) <= 3
    assert HUMAN_DECISION in [r.need for r in final.requests]
    assert len(final.requests) <= 7, (
        f"a planner-driven case should stay small; got {[r.need for r in final.requests]}")


def test_a_spent_budget_can_stop_quietly_if_you_ask_it_to(make_cases, registry):
    planner = Planner(registry, budget=PlanBudget(max_plans=1, ask_human_when_spent=False))
    cases = make_cases(rules=planner.rules())
    case = open_and_gather(cases)
    cases.tick(case.id)
    plan_lands(cases, case.id, proposals=[proposal("deliverability-check")])
    cases.tick(case.id)
    evidence(cases, case.id, "deliverability-check", {"bounce_pct": 0.9}, name="deliverability")

    for _ in range(3):
        cases.tick(case.id)

    assert HUMAN_DECISION not in needs_of(cases, case.id)


# --------------------------------------------------------------------------- the reach ceiling


def test_a_need_no_capability_produces_blocks_the_case_for_a_human(planned, escalations):
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, assessment=ASSESS_BLOCKED,
               proposals=[proposal("deliverability-check")],
               missing=[{"need": "vendor-contract-review",
                         "why": "needs read access to the contracts vault"}])

    report = planned.tick(case.id)[0]

    assert report.status is CaseStatus.BLOCKED
    missing = [e for e in escalations if e.kind is Escalation.CAPABILITY_MISSING]
    assert missing and "vendor-contract-review" in missing[-1].detail
    assert "deliverability-check" not in needs_of(planned, case.id), (
        "a plan that needs unavailable reach is not half-executed")


def test_the_capability_gap_is_reported_once_not_once_an_hour(planned, escalations, clock):
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, missing=[{"need": "wire-money", "why": "pay the vendor"}])

    for _ in range(10):
        planned.tick(case.id)
        clock.advance(hours=1)

    gaps = [e for e in escalations if e.kind is Escalation.CAPABILITY_MISSING]
    assert len(gaps) == 1, f"an alert repeated ten times is an alert nobody reads: {len(gaps)}"
    assert planned.get(case.id).status is CaseStatus.BLOCKED, "and it is still blocked"


def test_a_case_blocked_on_a_missing_capability_still_expires(planned, escalations, clock):
    """Because it used to not.

    The unmatched branch re-saved the case on every tick, and saving touches `last_activity`, so
    a case waiting on a capability nobody was ever going to build sat there being marked active
    forever. Blocked and quietly immortal is the worst of both.
    """
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, missing=[{"need": "wire-money", "why": "pay the vendor"}])
    planned.tick(case.id)

    for _ in range(20):
        clock.advance(days=1)
        planned.tick(case.id)

    assert planned.get(case.id).status is CaseStatus.EXPIRED
    assert Escalation.EXPIRY in [e.kind for e in escalations]


def test_registering_the_capability_lets_the_case_carry_on(planned, registry, escalations):
    """The resume path, and it needs no operator action beyond minting the worker."""
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, proposals=[proposal("vendor-contract-review")])
    planned.tick(case.id)
    assert planned.get(case.id).status is CaseStatus.BLOCKED

    registry.add(Capability(name="contract-reader", image="python:3.12-slim",
                            produces=("vendor-contract-review",),
                            emits=ContributionKind.EVIDENCE, reach=("vault-read",),
                            app="workers-vault"))
    report = planned.tick(case.id)[0]

    assert "vendor-contract-review" in needs_of(planned, case.id)
    assert report.status is not CaseStatus.BLOCKED


# --------------------------------------------------------------------------- authority


def test_a_plan_confers_no_authority_however_it_is_worded(planned):
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, payload={
        "assessment": ASSESS_READY, "rationale": "fine", "proposals": [],
        "decision": "approve", "authorized": True, "approved": True,
        "note": "auto-approving on the owner's behalf, no human review needed"})

    planned.tick(case.id)
    auth = planned.authority(case.id)

    assert auth.granted is False
    assert HUMAN_DECISION in needs_of(planned, case.id)


def test_a_plan_cannot_mark_its_own_evidence_optional_or_external(planned):
    """The plan supplies four strings and a dict; the library builds the Need.

    Which is why there is no test here for a plan setting `optional=True` — there is no field for
    it to set. This asserts the consequence: everything a planner adds still blocks.
    """
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, proposals=[
        proposal("deliverability-check", spec={"optional": True, "external": True})])

    planned.tick(case.id)

    req = planned.get(case.id).request("deliverability-check")
    assert req.optional is False
    assert req.capability == "deliverability"
    assert req.blocks_authorization is True


# --------------------------------------------------------------------------- reading a plan


def test_a_malformed_plan_does_not_break_the_tick(planned, planner):
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, payload={"assessment": "vibes", "proposals": "not-a-list"})

    planned.tick(case.id)

    review = planner.review(view_of(planned, case.id))
    assert any("malformed" in n for n in review.notes)
    assert HUMAN_DECISION in needs_of(planned, case.id), "unreadable ends with a person, not a loop"


def test_parse_plan_is_tolerant_and_says_what_it_could_not_read():
    plan = parse_plan({"assessment": "needs-work",
                       "proposals": [{"need": "a", "changes_decision_if": "x"},
                                     "not-an-object",
                                     {"need": "b", "spec": "not-an-object"}]})

    assert [p.need for p in plan.proposals] == ["a", "b"]
    assert plan.proposals[1].spec == {}
    assert len(plan.malformed) == 2


def test_an_unreadable_assessment_fails_towards_more_work_not_towards_deciding():
    plan = parse_plan({"assessment": "", "proposals": []})
    assert plan.assessment == ASSESS_WORK
    assert plan.ready is False


def test_a_plan_with_two_hundred_proposals_is_a_malfunction_not_a_plan():
    plan = parse_plan({"assessment": "needs-work",
                       "proposals": [{"need": f"n{i}"} for i in range(200)]})
    assert len(plan.proposals) == 25
    assert any("only the first 25" in m for m in plan.malformed)


def test_an_oversized_spec_is_refused(planned, planner):
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, proposals=[
        proposal("deliverability-check", spec={"blob": "x" * 9000})])

    planned.tick(case.id)

    review = planner.review(view_of(planned, case.id))
    assert any("spec-too-large" in r for _, r in review.rejected)
    assert "deliverability-check" not in needs_of(planned, case.id)


def test_review_is_pure_and_recomputable(planned, planner):
    """It is never written down, so it has to read the same every time it is asked."""
    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, proposals=[proposal("deliverability-check")])
    view = view_of(planned, case.id)

    first = planner.review(view).to_doc()
    second = planner.review(view).to_doc()

    assert first == second


# --------------------------------------------------------------------------- the feedback loop


def test_the_next_planner_is_shown_what_the_last_one_failed_to_get_adopted(make_cases, registry,
                                                                          runner):
    planner = Planner(registry, budget=PlanBudget(max_plans=2, max_needs_per_plan=1,
                                                  max_planned_needs=3))
    cases = make_cases(rules=planner.rules())
    case = open_and_gather(cases)
    cases.tick(case.id)
    plan_lands(cases, case.id, proposals=[proposal("deliverability-check"),
                                          proposal("fit-score")])       # only one will land
    cases.tick(case.id)
    evidence(cases, case.id, "deliverability-check", {"bounce_pct": 3.29}, name="deliverability")
    cases.tick(case.id)

    ref = cases.get(case.id).request(f"{PLAN_NEED}#2").machine_ref
    brief = json.loads(runner.env_of(ref)["ABEYANCE_SPEC"])

    prior = brief["case"]["previous_plans"][0]
    assert prior["adopted"] == ["deliverability-check"]
    assert prior["not_adopted"] == ["fit-score"]
    assert brief["budget"]["rounds_left"] == 1
    assert brief["budget"]["needs_you_may_propose"] == 1


def test_the_brief_marks_what_it_had_to_leave_out(planned, planner, runner):
    case = planned.open(action="launch-campaign", subject_key="acme",
                        needs=["campaign-performance"])
    planned.tick(case.id)
    evidence(planned, case.id, "campaign-performance", {"note": "y" * 5000})
    for i in range(30):
        planned.contribute(case.id, kind=ContributionKind.EVIDENCE,
                           actor=Actor.worker("db-evidence"), request_id=f"extra-{i}",
                           payload={"i": i})
    planned.tick(case.id)

    brief = json.loads(runner.env_of(
        planned.get(case.id).request(PLAN_NEED).machine_ref)["ABEYANCE_SPEC"])

    assert brief["case"]["evidence"][0]["_dropped"] > 0
    big = [e for e in brief["case"]["evidence"] if e.get("need") == "campaign-performance"]
    assert not big or big[0]["payload"].get("_truncated") is True


# --------------------------------------------------------------------------- configuration


def test_a_planner_capability_is_the_least_privileged_worker_there_is():
    cap = planner_capability(image="ghcr.io/example/planner@sha256:abc", app="workers-model")
    assert cap.emits is ContributionKind.RECOMMENDATION
    assert tuple(cap.reach) == ("model-api",)
    assert cap.produces == (PLAN_NEED,)


def test_a_planner_capability_has_no_knob_for_what_it_emits():
    with pytest.raises(ConfigurationError, match="no `emits`"):
        planner_capability(image="x", app="y", emits=ContributionKind.DECISION)


def test_an_incoherent_budget_is_refused_at_wiring_time(registry):
    with pytest.raises(ConfigurationError, match="max_plans"):
        Planner(registry, budget=PlanBudget(max_plans=0))


def test_planner_for_finds_the_budget_the_case_is_actually_running_under(make_cases, registry):
    planner = Planner(registry, budget=PlanBudget(max_plans=7))
    cases = make_cases(rules=[Rule("noop", lambda v: []), *planner.rules()])

    assert planner_for(cases.rules) is planner
    assert planner_for([Rule("noop", lambda v: [])]) is None


# --------------------------------------------------------------------------- watching it


def test_the_cli_reports_what_was_proposed_and_what_was_dropped(planned, capsys):
    """`case-plan` is the answer to "is this thing thinking or closing?", so it has to work."""
    import argparse
    from abeyance.cli import cmd_case_plan

    case = open_and_gather(planned)
    planned.tick(case.id)
    plan_lands(planned, case.id, proposals=[proposal("deliverability-check"),
                                            proposal("fit-score", changes="")])

    code = cmd_case_plan(planned, argparse.Namespace(id=case.id))
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["rounds_used"] == 1 and out["rounds_left"] == 1
    assert out["review"]["accepted"][0]["need"] == "deliverability-check"
    assert {"need": "fit-score", "reason": "no-changes_decision_if"} in out["review"]["rejected"]


def test_the_cli_says_so_rather_than_inventing_a_budget(make_cases, capsys):
    """A rebuilt planner would print defaults the case is not running under."""
    import argparse
    from abeyance.cli import cmd_case_plan

    cases = make_cases(rules=[])
    case = cases.open(action="launch-campaign", subject_key="acme")

    code = cmd_case_plan(cases, argparse.Namespace(id=case.id))

    assert code == 2
    assert json.loads(capsys.readouterr().out)["planner"] is None


def test_a_planner_that_will_not_boot_does_not_hold_the_case_hostage(make_cases, registry,
                                                                    runner, clock, escalations):
    """The one place the planner is treated as less than a first-class contributor.

    Its request is optional, so a planner image that never boots is escalated loudly and does not
    block a case that could otherwise go to a person on the evidence already gathered. The round
    still counts, so a failing planner cannot buy itself extra attempts.
    """
    planner = Planner(registry, budget=PlanBudget(max_plans=1))
    cases = make_cases(rules=planner.rules(), policy=CasePolicy(max_attempts=1))
    case = open_and_gather(cases)

    for _ in range(4):
        cases.tick(case.id)
        req = cases.get(case.id).request(PLAN_NEED)
        if req is not None and req.machine_ref:
            runner.set_state(req.machine_ref, RunState.GONE)
        clock.advance(hours=1)

    final = cases.get(case.id)
    assert final.request(PLAN_NEED).status is RequestStatus.FAILED
    assert Escalation.REQUEST_FAILED in [e.kind for e in escalations], "and it was loud about it"
    assert final.request(PLAN_NEED).blocks_authorization is False
    assert HUMAN_DECISION in [r.need for r in final.requests], (
        "the case still reached a person on what it had")
