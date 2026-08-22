"""Dynamic activity selection, and the limits that keep it from becoming a rule engine.

The tests that matter here are the negative ones: a rule cannot double-request, cannot retract,
cannot chain within a pass, and cannot invent a worker. Those four limits are the whole reason
this file is small enough to trust.
"""
from __future__ import annotations

import pytest

from abeyance import (Actor, Case, CasePolicy, CaseView, Contribution, ContributionKind,
                      ContributionRequest, Need, RequestStatus, Rule, always, derive,
                      when_payload)


def case(*requests, action="launch-campaign"):
    return Case(id="c1", action=action, subject_key="acme", requests=list(requests))


def satisfied(request_id, need):
    return ContributionRequest(id=request_id, need=need, capability="cap",
                               status=RequestStatus.SATISFIED)


def evidence(request_id, payload, *, epoch=100, need_actor="db-evidence"):
    return Contribution(case_id="c1", request_id=request_id, kind=ContributionKind.EVIDENCE,
                        actor=Actor.worker(need_actor), payload=payload, created_epoch=epoch)


# --------------------------------------------------------------- the basics


def test_a_static_need_is_requested_once(registry):
    rules = [always("campaign-performance", client="acme")]
    c = case()

    d = derive(c, [], rules, registry, CasePolicy())
    assert [r.need for r in d.new_requests] == ["campaign-performance"]
    assert d.new_requests[0].spec == {"client": "acme"}
    assert d.new_requests[0].capability == "db-evidence", "resolved by the registry, not the rule"
    assert d.fired == ["always:campaign-performance"]


def test_derive_is_pure_and_attaches_nothing(registry):
    c = case()
    derive(c, [], [always("campaign-performance")], registry, CasePolicy())
    assert c.requests == [], (
        "derive builds requests and returns them; the caller decides whether to accept, so "
        "'what would this do?' is answerable without side effects")


def test_a_rule_firing_every_tick_cannot_double_request(registry):
    """Structural idempotence. The rule author writes the obvious predicate and cannot create a
    duplicate-dispatch bug by forgetting a guard."""
    forgetful = Rule("forgetful", lambda view: [Need("campaign-performance")])
    c = case()

    first = derive(c, [], [forgetful], registry, CasePolicy())
    c.requests.extend(first.new_requests)

    for _ in range(5):
        again = derive(c, [], [forgetful], registry, CasePolicy())
        assert again.new_requests == []
    assert len(c.requests) == 1


def test_two_rules_wanting_the_same_need_produce_one_request(registry):
    a = Rule("a", lambda v: [Need("fit-score")])
    b = Rule("b", lambda v: [Need("fit-score")])
    d = derive(case(), [], [a, b], registry, CasePolicy())
    assert len(d.new_requests) == 1
    assert d.fired == ["a"], "the first rule to want it owns it, and the record says which"


# --------------------------------------------------------------- the dynamic part


def test_new_evidence_is_what_makes_new_work_warranted(registry):
    """The whole thesis, in one test: the graph was not known when the case opened."""
    rule = when_payload("integrity-deep-check", given="campaign-performance",
                        key="data_problem")
    c = case(satisfied("perf", "campaign-performance"))

    quiet = evidence("perf", {"reply_rate": 3.1, "data_problem": False})
    assert derive(c, [quiet], [rule], registry, CasePolicy()).new_requests == []

    alarming = evidence("perf", {"reply_rate": 0.1, "data_problem": True})
    d = derive(c, [alarming], [rule], registry, CasePolicy())
    assert [r.need for r in d.new_requests] == ["integrity-deep-check"]
    assert d.new_requests[0].warranted_by == "integrity-deep-check-if-campaign-performance.data_problem"
    assert "data_problem=True" in d.new_requests[0].spec["because"], (
        "the request has to carry WHY it exists, or 'why did this happen?' is unanswerable later")


def test_carry_threads_context_into_the_next_workers_spec(registry):
    """The quiet failure this exists to prevent.

    The next worker runs in a fresh container and knows nothing but its spec. A rule that
    warrants "check deliverability" without saying *which client* produces a worker that
    dutifully queries nothing and reports zeros — request satisfied, evidence present, evidence
    empty. Found exactly this way in the first live run.
    """
    rule = when_payload("integrity-deep-check", given="campaign-performance",
                        key="data_problem", carry=("client", "days_since_last_send"))
    c = case(satisfied("perf", "campaign-performance"))
    ev = evidence("perf", {"data_problem": True, "client": "Northwind",
                           "days_since_last_send": 17, "irrelevant": "dropped"})

    spec = derive(c, [ev], [rule], registry, CasePolicy()).new_requests[0].spec
    assert spec["client"] == "Northwind"
    assert spec["days_since_last_send"] == 17
    assert "irrelevant" not in spec, "carry is an allowlist, not a payload dump"


def test_carry_ignores_keys_the_payload_does_not_have(registry):
    rule = when_payload("integrity-deep-check", given="campaign-performance",
                        key="data_problem", carry=("client", "missing"))
    c = case(satisfied("perf", "campaign-performance"))
    spec = derive(c, [evidence("perf", {"data_problem": True, "client": "Acme"})],
                  [rule], registry, CasePolicy()).new_requests[0].spec
    assert spec["client"] == "Acme" and "missing" not in spec


def test_an_explicit_spec_kwarg_beats_a_carried_key(registry):
    rule = when_payload("integrity-deep-check", given="campaign-performance",
                        key="data_problem", carry=("client",), client="override")
    c = case(satisfied("perf", "campaign-performance"))
    spec = derive(c, [evidence("perf", {"data_problem": True, "client": "carried"})],
                  [rule], registry, CasePolicy()).new_requests[0].spec
    assert spec["client"] == "override"


def test_a_dynamic_rule_waits_for_its_input_to_be_satisfied(registry):
    """A rule must not fire off a half-arrived answer."""
    rule = when_payload("integrity-deep-check", given="campaign-performance", key="data_problem")
    dispatched = ContributionRequest(id="perf", need="campaign-performance",
                                     capability="db-evidence",
                                     status=RequestStatus.DISPATCHED)
    d = derive(case(dispatched), [evidence("perf", {"data_problem": True})], [rule], registry,
               CasePolicy())
    assert d.new_requests == []


def test_chaining_happens_across_ticks_not_within_one(registry):
    """A → B → C, one step per tick. No fixpoint loop, no agenda, and each step is visible."""
    a_to_b = when_payload("fit-score", given="campaign-performance", key="worth_scoring")
    b_to_c = when_payload("integrity-deep-check", given="fit-score", key="suspicious")
    c = case(satisfied("perf", "campaign-performance"))
    contributions = [evidence("perf", {"worth_scoring": True})]

    tick1 = derive(c, contributions, [a_to_b, b_to_c], registry, CasePolicy())
    assert [r.need for r in tick1.new_requests] == ["fit-score"], "only one step per pass"
    c.requests.extend(tick1.new_requests)

    # B has been requested but not answered — C is still not warranted.
    assert derive(c, contributions, [a_to_b, b_to_c], registry, CasePolicy()).new_requests == []

    c.request("fit-score").status = RequestStatus.SATISFIED
    contributions.append(evidence("fit-score", {"suspicious": True}, epoch=200))
    tick3 = derive(c, contributions, [a_to_b, b_to_c], registry, CasePolicy())
    assert [r.need for r in tick3.new_requests] == ["integrity-deep-check"]


# --------------------------------------------------------------- the limits


def test_a_need_no_capability_produces_is_reported_never_invented(registry):
    rogue = Rule("rogue", lambda v: [Need("wire-money", spec={"amount": 10_000})])
    d = derive(case(), [], [rogue], registry, CasePolicy())

    assert d.new_requests == []
    assert d.unmatched == ["wire-money"]
    assert d.anything is True, "the caller must see this and block the case"


def test_an_unmatched_need_is_reported_once_not_once_per_rule(registry):
    a = Rule("a", lambda v: [Need("wire-money")])
    b = Rule("b", lambda v: [Need("wire-money")])
    d = derive(case(), [], [a, b], registry, CasePolicy())
    assert d.unmatched == ["wire-money"]


def test_the_request_cap_stops_two_rules_warranting_each_other(registry):
    """The runaway guard. Hitting it is reported, never silently truncated — a truncated
    investigation looks like a thorough one."""
    greedy = Rule("greedy", lambda v: [Need("campaign-performance"), Need("fit-score"),
                                       Need("integrity-deep-check")])
    d = derive(case(), [], [greedy], registry, CasePolicy(max_derived_requests=2))

    assert len(d.new_requests) == 2
    assert d.capped == ["integrity-deep-check"]


def test_the_cap_counts_requests_the_case_already_has(registry):
    existing = [satisfied(f"r{i}", f"need{i}") for i in range(3)]
    greedy = Rule("greedy", lambda v: [Need("fit-score")])
    d = derive(case(*existing), [], [greedy], registry, CasePolicy(max_derived_requests=3))
    assert d.new_requests == [] and d.capped == ["fit-score"]


def test_a_rule_has_no_way_to_retract_or_reprioritise(registry):
    """Not a behavioural test — an API-surface one. `Derivation` can only add.

    If a future change gives rules a way to cancel or reorder, this is the test that should
    have to be deleted deliberately, with someone noticing.
    """
    d = derive(case(satisfied("perf", "campaign-performance")), [], [], registry, CasePolicy())
    assert set(vars(d)) == {"new_requests", "unmatched", "capped", "fired"}
    assert not hasattr(d, "cancelled") and not hasattr(d, "priority")


def test_a_rule_returning_a_bare_need_is_tolerated(registry):
    """The common shape. Requiring a list for a single need is a footgun that costs nothing to
    remove — a rule that returns `Need(...)` instead of `[Need(...)]` would otherwise iterate
    over the dataclass fields."""
    d = derive(case(), [], [Rule("bare", lambda v: Need("fit-score"))], registry, CasePolicy())
    assert [r.need for r in d.new_requests] == ["fit-score"]


def test_a_rule_returning_none_is_tolerated(registry):
    d = derive(case(), [], [Rule("quiet", lambda v: None)], registry, CasePolicy())
    assert d.new_requests == [] and d.fired == []


# --------------------------------------------------------------- the view


def test_the_view_reads_the_newest_contribution_for_a_need():
    c = case(satisfied("perf", "campaign-performance"))
    view = CaseView(c, [evidence("perf", {"reply_rate": 1.0}, epoch=100),
                        evidence("perf", {"reply_rate": 9.9}, epoch=200)])
    assert view.payload("campaign-performance")["reply_rate"] == 9.9


def test_the_view_hides_superseded_contributions():
    old = evidence("perf", {"reply_rate": 1.0}, epoch=100)
    new = Contribution(case_id="c1", request_id="perf2", kind=ContributionKind.EVIDENCE,
                       actor=Actor.worker("db"), payload={"reply_rate": 9.9},
                       supersedes=old.id, created_epoch=200)
    view = CaseView(case(satisfied("perf", "campaign-performance")), [old, new])
    assert len(view.contributions) == 1
    assert view.evidence[0].payload["reply_rate"] == 9.9


def test_a_missing_payload_reads_as_empty_rather_than_none():
    """So a rule writes `view.payload("x").get("y")` without a guard — a missing contribution
    and a useless one warrant the same next step, which is nothing."""
    view = CaseView(case(), [])
    assert view.payload("anything") == {}


def test_the_view_answers_decided_without_judging_standing():
    """A rule asking "has a human weighed in" is scheduling. Whether it carries authority is
    `standing.authorize()`'s job, and mixing the two would put authority logic in rule code."""
    dec = Contribution(case_id="c1", request_id="rh", kind=ContributionKind.DECISION,
                       actor=Actor.human("nobody@x.com", standing=()),
                       payload={"decision": "approve"}, created_epoch=1)
    assert CaseView(case(), [dec]).decided is True


def test_the_view_exposes_case_context():
    c = case()
    c.context = {"client": "acme", "sector": "k12"}
    assert CaseView(c, []).context("sector") == "k12"
    assert CaseView(c, []).context("missing", "fallback") == "fallback"


def test_view_request_state_helpers():
    c = case(ContributionRequest(id="a", need="campaign-performance", capability="x",
                                 status=RequestStatus.DISPATCHED),
             ContributionRequest(id="b", need="fit-score", capability="y",
                                 status=RequestStatus.FAILED))
    view = CaseView(c, [])
    assert view.requested("campaign-performance") and view.outstanding("campaign-performance")
    assert view.failed("fit-score") and not view.satisfied("fit-score")
    assert not view.requested("integrity-deep-check")
