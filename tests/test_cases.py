"""The case layer end to end, on in-memory adapters.

The test that matters most is `test_the_whole_shape`: a case opened by one caller, evidence
contributed by a worker that knows only a case id, a human decision arriving later through the
approval layer, and a third party executing — with no process alive across any of it.
"""
from __future__ import annotations

import pytest

from abeyance import (Actor, Approver, CaseLoop, CasePolicy, CaseStatus, ContributionKind,
                      ContributionRequest, Escalation, Need, NotAuthorized, RequestStatus,
                      RunState, Rule, always, when_payload)
from abeyance.adapters import MemoryStore
from abeyance.errors import CapabilityMissing, ConfigurationError

STANDING = {"boss@example.com": ("launch-campaign",)}


def worker_contributes(cases, case_id, request_id, payload, *, name="db-evidence",
                       kind=ContributionKind.EVIDENCE, scope=None):
    """What a real worker does: one write, knowing only the ids from its environment."""
    return cases.contribute(case_id, kind=kind, actor=Actor.worker(name),
                            request_id=request_id, payload=payload, scope=scope or {},
                            provenance={"machine": "mem-0001"})


def approve(loop, proposal_id, address="boss@example.com", text="approve 1"):
    loop.transport.receive(proposal_id, address, text)
    inbound = loop.read(proposal_id)
    return [loop.record_from(proposal_id, i) for i in inbound]


# --------------------------------------------------------------- opening


def test_opening_a_case_records_what_it_needs(cases):
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance",
                             Need("fit-score", spec={"model": "v2"}, optional=True)])

    assert case.status is CaseStatus.OPEN
    assert [r.need for r in case.requests] == ["campaign-performance", "fit-score"]
    assert case.request("fit-score").optional is True
    assert case.request("fit-score").spec == {"model": "v2"}
    assert all(r.warranted_by == "opened" for r in case.requests)
    assert cases.get(case.id).to_doc() == case.to_doc(), "it is durable immediately"


def test_a_case_needs_an_action_because_standing_is_checked_against_it(cases):
    with pytest.raises(ConfigurationError, match="needs an `action`"):
        cases.open(action="", subject_key="acme")


def test_opening_a_case_that_needs_an_unreachable_capability_refuses_up_front(cases):
    with pytest.raises(CapabilityMissing, match="wire-money"):
        cases.open(action="pay", needs=["wire-money"])


# --------------------------------------------------------------- the whole shape


def test_the_whole_shape(cases, loop, runner, clock, store):
    """Four independent actors, no shared memory, nothing alive between them."""
    # --- process A: opens the case, asks for evidence, exits ---------------
    case = cases.open(action="launch-campaign", subject_key="acme",
                      title="Re-engage dormant Acme leads",
                      needs=["campaign-performance"])
    case_id = case.id

    # --- process B: a tick starts the worker container --------------------
    fresh = CaseLoop("test", store=store, registry=cases.registry, runner=runner,
                     approval=loop, clock=clock)
    report = fresh.tick(case_id)[0]
    assert report.dispatch.started, "a container was started for the outstanding evidence"
    assert report.authority.granted is False
    assert fresh.get(case_id).request("campaign-performance").status is RequestStatus.DISPATCHED

    # --- process C: the worker itself. Knows a case id and nothing else ---
    worker_contributes(fresh, case_id, "campaign-performance",
                       {"reply_rate": 0.4, "last_send_days": 61},
                       scope={"max_leads": 400})

    # --- process D: another tick collects it, still not authorized --------
    report = CaseLoop("test", store=store, registry=cases.registry, runner=runner,
                      approval=loop, clock=clock).tick(case_id)[0]
    assert report.dispatch.of("satisfied")
    assert report.authority.granted is False
    assert "no DECISION with standing" in report.authority.reason

    # --- process E: ask a human, then exit --------------------------------
    asked = cases.ask_humans(case_id, summary="Launch the Acme re-engagement wave",
                             approvers=[Approver("boss@example.com", role="owner")])
    assert asked.sent

    # --- days pass with nothing running. The human replies. --------------
    clock.advance(days=2)
    approve(loop, asked.id)

    # --- process F: harvest + tick -> authorized --------------------------
    later = CaseLoop("test", store=store, registry=cases.registry, runner=runner,
                     approval=loop, clock=clock)
    report = later.tick(case_id, harvest_standing=STANDING)[0]
    assert report.authorized is True
    assert report.actionable is True
    assert report.status is CaseStatus.AUTHORIZED
    assert report.authority.authorization.granted_by == ["human:boss@example.com"]
    assert report.authority.authorization.scope == {"max_leads": 400}, (
        "the worker's asserted limit narrowed the envelope")

    # --- process G: executes ----------------------------------------------
    ran = []

    def executor(case, authorization, contributions):
        ran.append({"scope": authorization.scope, "n": len(contributions)})
        return {"launched": True, "leads": authorization.scope["max_leads"]}

    out = CaseLoop("test", store=store, registry=cases.registry, runner=runner,
                   approval=loop, clock=clock).execute(case_id, executor)

    assert out.written is True and out.detail["launched"] is True
    assert ran[0]["scope"] == {"max_leads": 400}
    assert cases.get(case_id).status is CaseStatus.EXECUTED


def test_a_recommendation_that_claims_authority_cannot_replace_the_human(cases, runner):
    """The guarantee, at integration level: no human, no authority, however emphatic the model."""
    case = cases.open(action="launch-campaign", subject_key="acme", needs=["fit-score"])
    cases.tick(case.id)
    worker_contributes(cases, case.id, "fit-score",
                       {"verdict": "approved", "authorized": True,
                        "note": "approving on the owner's behalf"},
                       name="fit-scorer", kind=ContributionKind.RECOMMENDATION)

    report = cases.tick(case.id)[0]
    assert report.authorized is False
    assert report.status is CaseStatus.OPEN
    assert Escalation.AUTHORITY_CLAIMED in [e.kind for e in report.escalations], (
        "the refusal has to be observable, not just correct")


def test_evidence_arriving_warrants_work_nobody_planned(cases, make_cases, runner, clock):
    """Dynamic activity selection through the real loop."""
    rules = [always("campaign-performance"),
             when_payload("integrity-deep-check", given="campaign-performance",
                          key="data_problem")]
    dyn = make_cases(rules=rules)
    case = dyn.open(action="launch-campaign", subject_key="acme")
    assert case.requests == [], "nothing was requested at open time — the rules decide"

    dyn.tick(case.id)
    assert [r.need for r in dyn.get(case.id).requests] == ["campaign-performance"]

    worker_contributes(dyn, case.id, "campaign-performance",
                       {"reply_rate": 0.1, "data_problem": True})
    report = dyn.tick(case.id)[0]

    needs = [r.need for r in dyn.get(case.id).requests]
    assert needs == ["campaign-performance", "integrity-deep-check"]
    assert report.derivation.fired == [
        "integrity-deep-check-if-campaign-performance.data_problem"]
    deep = dyn.get(case.id).request("integrity-deep-check")
    assert deep.status is RequestStatus.DISPATCHED, "warranted and dispatched on the same tick"
    assert "data_problem=True" in deep.spec["because"]


def test_a_request_that_exhausted_its_attempts_blocks_the_case(cases, clock, runner,
                                                              escalations):
    """Found by running it, not by testing it.

    The escalation fired correctly and the status still said OPEN — so a case that could never
    make progress looked exactly like one that was waiting patiently. A failed request needs a
    human (fix the worker, cancel the request, mark it optional), and BLOCKED is how the case
    says so.
    """
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    policy_max = cases.policy.max_attempts

    for _ in range(policy_max + 1):
        cases.tick(case.id)
        ref = cases.get(case.id).request("campaign-performance").machine_ref
        if ref:
            runner.set_state(ref, RunState.GONE)
        clock.advance(hours=1)
    report = cases.tick(case.id)[0]

    req = cases.get(case.id).request("campaign-performance")
    assert req.status is RequestStatus.FAILED
    assert report.status is CaseStatus.BLOCKED, (
        "a case that can never be authorized must not report as OPEN")
    assert Escalation.REQUEST_FAILED in [e.kind for e in escalations]


def test_cancelling_a_failed_request_lets_the_case_flow_again(cases, clock, runner):
    """The recovery path a human actually has. BLOCKED must not be a one-way door."""
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    for _ in range(cases.policy.max_attempts + 1):
        cases.tick(case.id)
        ref = cases.get(case.id).request("campaign-performance").machine_ref
        if ref:
            runner.set_state(ref, RunState.GONE)
        clock.advance(hours=1)
    assert cases.tick(case.id)[0].status is CaseStatus.BLOCKED

    stuck = cases.get(case.id)
    stuck.request("campaign-performance").status = RequestStatus.CANCELLED
    cases.save(stuck)
    cases.policy_decision(case.id, rule="pre-cleared", standing=("launch-campaign",))

    assert cases.tick(case.id)[0].status is CaseStatus.AUTHORIZED


def test_an_optional_request_that_failed_does_not_block_the_case(make_cases, clock, runner):
    cases = make_cases()
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=[Need("fit-score", optional=True)])
    for _ in range(cases.policy.max_attempts + 1):
        cases.tick(case.id)
        ref = cases.get(case.id).request("fit-score").machine_ref
        if ref:
            runner.set_state(ref, RunState.GONE)
        clock.advance(hours=1)
    cases.tick(case.id)
    assert cases.get(case.id).request("fit-score").status is RequestStatus.FAILED

    cases.policy_decision(case.id, rule="pre-cleared", standing=("launch-campaign",))
    assert cases.tick(case.id)[0].status is CaseStatus.AUTHORIZED


def test_a_rule_wanting_unreachable_reach_blocks_the_case(make_cases, escalations):
    rogue = make_cases(rules=[Rule("rogue", lambda v: [Need("wire-money")])])
    case = rogue.open(action="launch-campaign", subject_key="acme")
    report = rogue.tick(case.id)[0]

    assert report.status is CaseStatus.BLOCKED
    assert Escalation.CAPABILITY_MISSING in [e.kind for e in report.escalations]
    assert "human decision" in [e for e in report.escalations
                                if e.kind is Escalation.CAPABILITY_MISSING][0].detail


def test_a_capability_gap_is_a_standing_condition_not_an_hourly_alarm(make_cases, escalations,
                                                                     clock):
    """Nobody mints a worker by waiting, so the rule re-emits and the gap re-derives forever.

    Two consequences, both of which used to be wrong. The alert repeated on every tick, which is
    how a channel that exists to make a guarantee visible becomes the one people filter. And the
    unchanged case was re-saved every tick, which marks it active — so a case blocked on a
    capability nobody was going to build sat there looking tended and never expired.
    """
    rogue = make_cases(rules=[Rule("rogue", lambda v: [Need("wire-money")])])
    case = rogue.open(action="launch-campaign", subject_key="acme")

    for _ in range(8):
        rogue.tick(case.id)
        clock.advance(hours=6)

    assert len([e for e in escalations if e.kind is Escalation.CAPABILITY_MISSING]) == 1
    assert rogue.get(case.id).status is CaseStatus.BLOCKED, "and it is still, truthfully, blocked"

    for _ in range(20):
        clock.advance(days=1)
        rogue.tick(case.id)
    assert rogue.get(case.id).status is CaseStatus.EXPIRED
    assert Escalation.EXPIRY in [e.kind for e in escalations]


# --------------------------------------------------------------- contributions as rows


def test_contributions_are_separate_rows_so_concurrent_workers_cannot_lose_each_other(cases,
                                                                                     store):
    """The concurrency decision, asserted. Three workers writing at once, none lost."""
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance", "fit-score", "integrity-deep-check"])
    for req, name in [("campaign-performance", "db-evidence"), ("fit-score", "fit-scorer"),
                      ("integrity-deep-check", "deep-check")]:
        kind = (ContributionKind.RECOMMENDATION if name == "fit-scorer"
                else ContributionKind.EVIDENCE)
        worker_contributes(cases, case.id, req, {"from": name}, name=name, kind=kind)

    assert len(cases.contributions(case.id)) == 3
    assert len(store.items(cases.contribution_kind)) == 3
    assert "contributions" not in cases.get(case.id).to_doc(), (
        "the case row must not carry them, or a concurrent write would clobber")


def test_a_worker_never_has_to_touch_the_case_row(cases, store):
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    before = store.get(cases.kind, case.id)
    worker_contributes(cases, case.id, "campaign-performance", {"ok": True})
    assert store.get(cases.kind, case.id) == before, (
        "contribute() writes only the contribution row — that is what makes it safe to run "
        "many workers at once, and it means a worker needs no read access to the case")


def test_contribute_refuses_a_decision(cases):
    case = cases.open(action="launch-campaign", subject_key="acme")
    with pytest.raises(ConfigurationError, match="do not enter through contribute"):
        cases.contribute(case.id, kind=ContributionKind.DECISION,
                         actor=Actor.human("boss@example.com", standing=("launch-campaign",)),
                         payload={"decision": "approve"})


def test_a_delegated_policy_decision_is_recorded_as_a_delegation(cases):
    case = cases.open(action="launch-campaign", subject_key="acme")
    cases.policy_decision(case.id, rule="small-spend", standing=("launch-campaign",),
                          summary="under the pre-cleared threshold")
    report = cases.tick(case.id)[0]

    assert report.authorized is True
    assert report.authority.authorization.granted_by == ["policy:small-spend"]
    contribution = cases.contributions(case.id)[0]
    assert contribution.provenance["delegated"] is True


# --------------------------------------------------------------- humans


def test_a_reply_from_someone_with_no_declared_standing_counts_for_nothing(cases, loop,
                                                                          escalations):
    case = cases.open(action="launch-campaign", subject_key="acme")
    asked = cases.ask_humans(case.id, summary="go?",
                             approvers=[Approver("stranger@example.com", role="?")])
    approve(loop, asked.id, address="stranger@example.com")

    got = cases.harvest(case.id, standing={})
    assert got == []
    assert Escalation.AUTHORITY_CLAIMED in [e.kind for e in escalations]
    assert cases.authority(case.id).granted is False


def test_harvest_is_idempotent_across_ticks(cases, loop):
    case = cases.open(action="launch-campaign", subject_key="acme")
    asked = cases.ask_humans(case.id, summary="go?",
                            approvers=[Approver("boss@example.com", role="owner")])
    approve(loop, asked.id)

    for _ in range(4):
        cases.harvest(case.id, standing=STANDING)
    decisions = [c for c in cases.contributions(case.id)
                 if c.kind is ContributionKind.DECISION]
    assert len(decisions) == 1, "running on every tick must converge, not stack votes"


def test_a_rejection_from_the_human_is_a_decision_not_a_wait(cases, loop):
    case = cases.open(action="launch-campaign", subject_key="acme")
    asked = cases.ask_humans(case.id, summary="go?",
                             approvers=[Approver("boss@example.com", role="owner")])
    approve(loop, asked.id, text="reject 1")
    cases.harvest(case.id, standing=STANDING)

    auth = cases.authority(case.id)
    assert auth.granted is False and "decided against" in auth.reason
    assert auth.missing_requests == [], "the question was answered; the answer was no"


def test_a_vague_reply_never_becomes_a_decision(cases, loop, escalations):
    """The approval layer's "a parser is not an authority" rule, still holding at case level.

    "no, do not send this" is obviously a refusal to a human and is not confidently parseable.
    The right outcome is an escalation and an unanswered request — not a guessed rejection, and
    emphatically not a guessed approval.
    """
    case = cases.open(action="launch-campaign", subject_key="acme")
    asked = cases.ask_humans(case.id, summary="go?",
                             approvers=[Approver("boss@example.com", role="owner")])
    approve(loop, asked.id, text="no, do not send this")

    assert cases.harvest(case.id, standing=STANDING) == []
    assert Escalation.AMBIGUOUS_REPLY in [e.kind for e in escalations]
    auth = cases.authority(case.id)
    assert auth.granted is False and auth.missing_requests == ["human-decision"]


def test_the_human_decision_request_blocks_until_it_is_answered(cases, loop):
    case = cases.open(action="launch-campaign", subject_key="acme")
    cases.ask_humans(case.id, summary="go?",
                     approvers=[Approver("boss@example.com", role="owner")])
    report = cases.tick(case.id)[0]

    assert report.authority.missing_requests == ["human-decision"], (
        "'we never asked anyone' and 'we asked and nobody answered' must both block")
    assert report.dispatch.of("external"), "never dispatched to a container"


def test_asking_humans_without_an_approval_loop_is_a_configuration_error(make_cases):
    cases = make_cases(with_approval=False)
    case = cases.open(action="launch-campaign", subject_key="acme")
    with pytest.raises(ConfigurationError, match="no ApprovalLoop"):
        cases.ask_humans(case.id, summary="go?",
                         approvers=[Approver("boss@example.com")])


# --------------------------------------------------------------- executing


def test_execute_is_blocked_rather_than_raising_on_a_normal_not_yet(cases):
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    out = cases.execute(case.id, lambda *a: {"ran": True})
    assert out.written is False and out.blocked
    assert cases.get(case.id).status is not CaseStatus.EXECUTED


def test_execute_raises_in_strict_mode(cases):
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    with pytest.raises(NotAuthorized):
        cases.execute(case.id, lambda *a: None, strict=True)


def test_execute_runs_once(cases):
    case = cases.open(action="launch-campaign", subject_key="acme")
    cases.policy_decision(case.id, rule="pre-cleared", standing=("launch-campaign",))
    cases.tick(case.id)

    calls = []
    first = cases.execute(case.id, lambda *a: calls.append(1))
    second = cases.execute(case.id, lambda *a: calls.append(2))

    assert first.written is True
    assert second.written is False and second.blocked == "already executed"
    assert calls == [1], "an hourly tick sees a settled case repeatedly and must not double-write"


def test_a_dry_run_reports_the_scope_it_would_act_under(cases):
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    cases.tick(case.id)
    worker_contributes(cases, case.id, "campaign-performance", {"ok": True},
                       scope={"max_leads": 250})
    cases.policy_decision(case.id, rule="pre-cleared", standing=("launch-campaign",))
    cases.tick(case.id)

    out = cases.execute(case.id, lambda *a: pytest.fail("must not run"), dry_run=True)
    assert out.authorized is True and out.written is False
    assert out.detail["scope"] == {"max_leads": 250}


def test_an_executor_that_refuses_leaves_the_case_retryable_and_escalates(cases, escalations):
    case = cases.open(action="launch-campaign", subject_key="acme")
    cases.policy_decision(case.id, rule="pre-cleared", standing=("launch-campaign",))
    cases.tick(case.id)

    def broken(case, auth, contributions):
        raise RuntimeError("clickup returned 500")

    out = cases.execute(case.id, broken)
    assert out.written is False and "clickup returned 500" in out.error
    assert cases.get(case.id).status is not CaseStatus.EXECUTED, "retryable once fixed"
    assert Escalation.EXECUTION_REFUSED in [e.kind for e in escalations]

    assert cases.execute(case.id, lambda *a: {"ok": True}).written is True


def test_authority_is_rechecked_at_commit_time_not_trusted_from_the_row(cases, clock,
                                                                       escalations):
    """The Tuesday-yes / Thursday-evidence failure, caught at the last possible moment."""
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    cases.tick(case.id)
    first = worker_contributes(cases, case.id, "campaign-performance",
                              {"reply_rate": 4.0, "safe": True})
    cases.policy_decision(case.id, rule="pre-cleared", standing=("launch-campaign",))
    assert cases.tick(case.id)[0].authorized is True

    # The world changed under the decision.
    clock.advance(hours=6)
    cases.contribute(case.id, kind=ContributionKind.EVIDENCE,
                     actor=Actor.worker("db-evidence"), request_id="campaign-performance-v2",
                     payload={"reply_rate": 0.05, "safe": False}, supersedes=first.id)

    out = cases.execute(case.id, lambda *a: pytest.fail("must not act on stale authority"))
    assert out.written is False
    assert "superseded" in out.blocked
    assert cases.get(case.id).status is CaseStatus.BLOCKED
    assert Escalation.STALE_AUTHORITY in [e.kind for e in escalations]


def test_a_blocked_case_recovers_on_the_next_tick_once_state_is_consistent(cases, clock):
    """Self-healing: the stale envelope is re-derived rather than needing a human to unstick it."""
    case = cases.open(action="launch-campaign", subject_key="acme")
    cases.policy_decision(case.id, rule="pre-cleared", standing=("launch-campaign",))
    cases.tick(case.id)
    first = cases.contributions(case.id)[0]

    cases.contribute(case.id, kind=ContributionKind.EVIDENCE, actor=Actor.worker("db-evidence"),
                     request_id="volunteered", payload={"note": "fyi"})
    cases.execute(case.id, lambda *a: None)  # trips the stale check, blocks

    clock.advance(minutes=5)
    report = cases.tick(case.id)[0]
    assert report.status is CaseStatus.AUTHORIZED
    assert cases.execute(case.id, lambda *a: {"ok": True}).written is True


# --------------------------------------------------------------- housekeeping


def test_a_case_nobody_contributes_to_expires_loudly(cases, clock, escalations):
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    clock.advance(days=15)

    assert cases.sweep()["expired"] == [case.id]
    assert cases.get(case.id).status is CaseStatus.EXPIRED
    assert Escalation.EXPIRY in [e.kind for e in escalations]


def test_an_expired_case_is_reported_by_tick_and_does_nothing_else(cases, clock, runner):
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    clock.advance(days=15)
    report = cases.tick(case.id)[0]

    assert report.expired is True
    assert report.dispatch is None and runner.started == []


def test_activity_keeps_a_case_alive(cases, clock):
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    clock.advance(days=10)
    worker_contributes(cases, case.id, "campaign-performance", {"ok": True})
    cases.tick(case.id)  # collects, touches
    clock.advance(days=10)

    assert cases.sweep()["expired"] == [], (
        "expiry runs off last activity, so work in progress does not die at a fixed deadline")


def test_a_quiet_tick_does_not_rewrite_the_row(cases, clock, store):
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    cases.tick(case.id)
    snapshot = store.get(cases.kind, case.id)

    clock.advance(seconds=10)
    report = cases.tick(case.id)[0]
    assert report.saved is False
    assert store.get(cases.kind, case.id) == snapshot


def test_tick_over_no_open_cases_is_free(cases):
    assert cases.tick() == []


def test_the_summary_is_printable_and_names_the_deciders(cases):
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    cases.policy_decision(case.id, rule="pre-cleared", standing=("launch-campaign",))
    got = cases.summary(case.id)

    assert got["action"] == "launch-campaign"
    assert got["blocking"] == ["campaign-performance"]
    assert got["deciders"] == ["policy:pre-cleared"]


def test_two_case_loops_over_one_store_keep_their_state_apart(store, registry, clock):
    a = CaseLoop("alpha", store=store, registry=registry, clock=clock)
    b = CaseLoop("beta", store=store, registry=registry, clock=clock)
    a.open(action="x", subject_key="s")
    assert len(a.all()) == 1 and b.all() == []
