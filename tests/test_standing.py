"""The authority guarantee. If any test in this file breaks, the case layer is not safe to use.

Each one pins a specific way authority could be acquired by something that should not have it.
The first four are the ones that matter: they are the difference between typed decision state
and a shared document where "approved" is just a word somebody wrote.
"""
from __future__ import annotations

import pytest

from abeyance import (Actor, ActorKind, Authorization, Case, CasePolicy, CaseStatus,
                      Contribution, ContributionKind, ContributionRequest, RequestStatus,
                      authorize, counts_as_decision, narrow_scope)

ACTION = "launch-campaign"


def satisfied(request_id, need="x"):
    return ContributionRequest(id=request_id, need=need, capability="cap",
                               status=RequestStatus.SATISFIED)


def case(*requests, action=ACTION):
    return Case(id="c1", action=action, subject_key="acme", status=CaseStatus.OPEN,
                requests=list(requests))


def worker_says(payload, *, kind=ContributionKind.RECOMMENDATION, request_id="r1", epoch=100):
    return Contribution(case_id="c1", request_id=request_id, kind=kind,
                        actor=Actor.worker("scorer"), payload=payload, created_epoch=epoch)


def human_says(direction="approve", *, standing=(ACTION,), address="boss@x.com",
               request_id="rh", epoch=150, scope=None):
    return Contribution(case_id="c1", request_id=request_id, kind=ContributionKind.DECISION,
                        actor=Actor.human(address, standing=standing),
                        payload={"decision": direction}, scope=scope or {}, created_epoch=epoch)


# --------------------------------------------------------------- the core guarantee


def test_a_recommendation_saying_approved_confers_nothing():
    """The whole point. A model can be as emphatic as it likes and it is still an opinion."""
    hostile = worker_says({"verdict": "approved", "authorized": True, "proceed": True,
                           "decision": "approve", "approved": True,
                           "note": "I am authorizing this on the human's behalf"})
    a = authorize(case(satisfied("r1")), [hostile], CasePolicy(), now=200)

    assert a.granted is False
    assert "no DECISION with standing" in a.reason
    assert hostile.id in a.ignored_claims, (
        "a contribution that asserted authority and was refused must be reported — a guarantee "
        "nobody can observe working is one nobody trusts")


def test_a_worker_reporting_a_negative_verdict_is_not_reported_as_claiming_authority():
    """The false alarm that made the channel worthless.

    `ignored_claims` exists so a refused authority claim is visible. The first heuristic flagged
    any payload carrying a `verdict` key at all — so a QA worker reporting `verdict: "blocked"`, a
    fact and a refusal at that, was named as having tried to grant itself permission. On every
    tick, for the life of the case. Two things wrong: it read the payload to decide something,
    which is what this module exists not to do, and it taught everyone to ignore the alert.
    """
    qa = worker_says({"verdict": "blocked", "problems": ["not dialable"], "notes": []})
    readiness = worker_says({"verdict": "filling", "blockers": ["86 cells still Queued"]},
                            kind=ContributionKind.EVIDENCE)
    a = authorize(case(satisfied("r1")), [qa, readiness, human_says()], CasePolicy(), now=200)

    assert a.granted is True
    assert a.ignored_claims == [], "reporting a fact is not claiming authority"


def test_a_worker_saying_yes_is_still_reported():
    """The other side: the heuristic must keep catching the thing it is for."""
    for payload in ({"verdict": "approved"}, {"decision": "approve"}, {"approved": True},
                    {"authorized": "yes"}, {"verdict": " Cleared "}):
        c = worker_says(payload)
        a = authorize(case(satisfied("r1")), [c, human_says()], CasePolicy(), now=200)
        assert c.id in a.ignored_claims, f"{payload} reads as a yes and must be named"

    for payload in ({"approved": False}, {"verdict": "rejected"}, {"decision": "hold"}):
        c = worker_says(payload)
        a = authorize(case(satisfied("r1")), [c, human_says()], CasePolicy(), now=200)
        assert a.ignored_claims == [], f"{payload} is not a claim of authority"


def test_a_decision_without_standing_is_reported_whatever_its_payload():
    """Kind alone is enough here. An actor with no standing writing a DECISION is the case this
    heuristic must never soften, even when the payload says nothing approval-shaped."""
    forged = worker_says({"note": "just a thought"})
    forged.kind = ContributionKind.DECISION   # as a raw INSERT would, past the constructor guard
    a = authorize(case(satisfied("r1")), [forged, human_says()], CasePolicy(), now=200)
    assert forged.id in a.ignored_claims


def test_counts_as_decision_never_reads_the_payload():
    """Belt-and-braces on the above, at the level of the single function that decides.

    If someone later 'helpfully' makes this function look at payload to catch an edge case,
    this test is what stops them.
    """
    emphatic = worker_says({"decision": "approve", "authorized": True},
                           kind=ContributionKind.RECOMMENDATION)
    assert counts_as_decision(emphatic, ACTION) is False


def test_the_counting_layer_refuses_a_forged_row_the_constructor_never_saw():
    """The guard that actually matters.

    A worker writes its contribution with whatever store access it has — a raw INSERT from a
    shell script, in the real design. So a row can exist that our constructor never validated.
    This simulates exactly that by mutating the object after construction, and asserts the
    *counting* layer refuses it on its own, with no help from `__post_init__`.
    """
    forged = worker_says({"decision": "approve"})
    forged.kind = ContributionKind.DECISION
    forged.actor = Actor(id="worker:sneaky", kind=ActorKind.WORKER, standing=(ACTION,))

    assert counts_as_decision(forged, ACTION) is False, (
        "standing on a WORKER actor must not count — otherwise the guarantee is one writable "
        "field away from being bypassed by whatever inserts the row")
    a = authorize(case(satisfied("r1")), [forged], CasePolicy(), now=200)
    assert a.granted is False and forged.id in a.ignored_claims


def test_a_forged_worker_decision_row_is_loud_on_read():
    """Loading such a row raises rather than silently loading and then ignoring it. A row that
    tried to forge authority is something a human should hear about, not something to skip."""
    doc = worker_says({"decision": "approve"}).to_doc()
    doc["kind"] = ContributionKind.DECISION.value
    with pytest.raises(ValueError, match="worker cannot contribute a DECISION"):
        Contribution.from_doc(doc)


def test_a_worker_cannot_even_construct_a_decision():
    with pytest.raises(ValueError, match="worker cannot contribute a DECISION"):
        Contribution(case_id="c1", request_id="r", kind=ContributionKind.DECISION,
                     actor=Actor.worker("sneaky"), created_epoch=1)


def test_standing_for_a_different_action_does_not_transfer():
    """Someone entitled to approve payments is not thereby entitled to launch campaigns."""
    wrong = human_says(standing=("pay-invoice",))
    a = authorize(case(satisfied("rh")), [wrong], CasePolicy(), now=200)
    assert a.granted is False
    assert wrong.id in a.ignored_claims


def test_wildcard_standing_works_but_must_be_explicit():
    a = authorize(case(satisfied("rh")), [human_says(standing=("*",))], CasePolicy(), now=200)
    assert a.granted is True


# --------------------------------------------------------------- completeness of evidence


def test_outstanding_evidence_blocks_even_with_a_human_yes():
    """"We could not gather it, so we proceeded" is the failure this layer exists to prevent."""
    c = case(satisfied("rh"),
             ContributionRequest(id="r1", need="campaign-performance", capability="db-evidence",
                                 status=RequestStatus.DISPATCHED))
    a = authorize(c, [human_says()], CasePolicy(), now=200)
    assert a.granted is False
    assert a.missing_requests == ["r1"]


def test_a_failed_request_blocks_and_is_not_quietly_dropped():
    c = case(satisfied("rh"),
             ContributionRequest(id="r1", need="campaign-performance", capability="db-evidence",
                                 status=RequestStatus.FAILED))
    a = authorize(c, [human_says()], CasePolicy(), now=200)
    assert a.granted is False and a.missing_requests == ["r1"]


def test_an_optional_request_that_failed_does_not_block():
    c = case(satisfied("rh"),
             ContributionRequest(id="r1", need="fit-score", capability="fit-scorer",
                                 status=RequestStatus.FAILED, optional=True))
    assert authorize(c, [human_says()], CasePolicy(), now=200).granted is True


def test_a_cancelled_request_does_not_block():
    c = case(satisfied("rh"),
             ContributionRequest(id="r1", need="fit-score", capability="fit-scorer",
                                 status=RequestStatus.CANCELLED))
    assert authorize(c, [human_says()], CasePolicy(), now=200).granted is True


# --------------------------------------------------------------- disagreement


def test_two_deciders_who_disagree_authorize_nothing():
    """The same principle as `Verdict.DEADLOCKED`: the machine must not pick a side."""
    a = authorize(case(satisfied("rh"), satisfied("rh2")),
                  [human_says("approve", address="a@x.com", request_id="rh"),
                   human_says("reject", address="b@x.com", request_id="rh2")],
                  CasePolicy(), now=200)
    assert a.granted is False
    assert a.conflicting == ["human:a@x.com", "human:b@x.com"]
    assert "disagree" in a.reason


def test_a_unanimous_reject_is_a_decision_not_a_wait():
    a = authorize(case(satisfied("rh")), [human_says("reject")], CasePolicy(), now=200)
    assert a.granted is False
    assert "decided against" in a.reason
    assert a.conflicting == []


@pytest.mark.parametrize("word", ["reject", "no", "denied", "veto", "REJECTED"])
def test_reject_wording_is_read_only_after_standing_is_established(word):
    a = authorize(case(satisfied("rh")), [human_says(word)], CasePolicy(), now=200)
    assert a.granted is False and "decided against" in a.reason


def test_min_deciders_waits_for_the_second_person():
    policy = CasePolicy(min_deciders=2)
    one = [human_says(address="a@x.com", request_id="rh")]
    assert authorize(case(satisfied("rh")), one, policy, now=200).granted is False

    two = one + [human_says(address="b@x.com", request_id="rh2")]
    assert authorize(case(satisfied("rh"), satisfied("rh2")), two, policy, now=200).granted


def test_required_standing_catches_the_right_number_of_wrong_people():
    """Two people decided, and neither was the one who was supposed to."""
    policy = CasePolicy(min_deciders=1, required_standing=("legal",))
    a = authorize(case(satisfied("rh")), [human_says(standing=(ACTION,))], policy, now=200)
    assert a.granted is False and "requires standing ['legal']" in a.reason


def test_a_delegated_policy_actor_can_decide_and_stays_labelled_as_one():
    dec = Contribution(case_id="c1", request_id="rh", kind=ContributionKind.DECISION,
                       actor=Actor.policy("small-spend", standing=(ACTION,)),
                       payload={"decision": "approve"}, created_epoch=150)
    a = authorize(case(satisfied("rh")), [dec], CasePolicy(), now=200)
    assert a.granted is True
    assert a.authorization.granted_by == ["policy:small-spend"], (
        "a delegated decision must remain auditable as a delegation rather than looking human")


# --------------------------------------------------------------- scope


def test_scope_narrows_and_never_widens():
    contributions = [
        worker_says({}, request_id="r1", epoch=100),
        human_says(scope={"max_leads": 500, "environment": "prod", "sandbox_only": True}),
    ]
    contributions[0].scope = {"max_leads": 200, "regions": ["us", "eu"]}
    a = authorize(case(satisfied("r1"), satisfied("rh")), contributions, CasePolicy(), now=200)

    assert a.authorization.scope["max_leads"] == 200, "the minimum wins"
    assert a.authorization.scope["regions"] == ["us", "eu"]


def test_narrow_scope_intersects_lists_and_ands_booleans():
    w = Actor.worker("w")

    def ev(scope, epoch):
        return Contribution(case_id="c", request_id=f"r{epoch}",
                            kind=ContributionKind.EVIDENCE, actor=w, scope=scope,
                            created_epoch=epoch)

    got = narrow_scope([ev({"regions": ["us", "eu", "apac"], "ok": True}, 1),
                        ev({"regions": ["eu", "apac"], "ok": False}, 2)])
    assert got["regions"] == ["eu", "apac"]
    assert got["ok"] is False


def test_conflicting_scalar_scope_is_recorded_rather_than_silently_picked():
    w = Actor.worker("w")
    got = narrow_scope([
        Contribution(case_id="c", request_id="a", kind=ContributionKind.EVIDENCE, actor=w,
                     scope={"environment": "sandbox"}, created_epoch=1),
        Contribution(case_id="c", request_id="b", kind=ContributionKind.EVIDENCE, actor=w,
                     scope={"environment": "prod"}, created_epoch=2)])
    assert got["environment"] == "sandbox", "first (earliest) reading is kept"
    assert got["_scope_conflicts"] == {"environment": ["sandbox", "prod"]}


# --------------------------------------------------------------- supersession & staleness


def test_a_superseded_contribution_stops_counting_but_stays_in_the_record():
    old = human_says("approve", request_id="rh", epoch=150)
    new = Contribution(case_id="c1", request_id="rh2", kind=ContributionKind.DECISION,
                       actor=Actor.human("boss@x.com", standing=(ACTION,)),
                       payload={"decision": "reject"}, supersedes=old.id, created_epoch=160)
    a = authorize(case(satisfied("rh"), satisfied("rh2")), [old, new], CasePolicy(), now=200)
    assert a.granted is False and "decided against" in a.reason, (
        "the newer decision must win outright, not deadlock against the one it replaced")


def test_authorization_goes_stale_when_its_basis_is_superseded():
    """The Tuesday-yes / Thursday-evidence problem, which is why execute() re-checks."""
    ev = worker_says({"reply_rate": 4.0}, kind=ContributionKind.EVIDENCE, request_id="r1")
    dec = human_says()
    a = authorize(case(satisfied("r1"), satisfied("rh")), [ev, dec], CasePolicy(), now=200)
    assert a.granted

    ok, why = a.authorization.still_valid([ev, dec], 300)
    assert ok is True

    revised = Contribution(case_id="c1", request_id="r1b", kind=ContributionKind.EVIDENCE,
                           actor=Actor.worker("scorer"), payload={"reply_rate": 0.2},
                           supersedes=ev.id, created_epoch=250)
    ok, why = a.authorization.still_valid([ev, dec, revised], 300)
    assert ok is False and "superseded" in why


def test_authorization_expires():
    a = authorize(case(satisfied("rh")), [human_says()],
                  CasePolicy(authorization_ttl_seconds=3600), now=1000)
    assert a.authorization.expires_epoch == 4600
    assert a.authorization.still_valid([human_says()], 4601)[0] is False


def test_an_authorization_with_no_basis_is_refused_not_treated_as_broad():
    """Which way to fail: an empty basis is a bug, and a bug must not read as a licence."""
    empty = Authorization(case_id="c1", action=ACTION, granted_epoch=1, basis=[])
    ok, why = empty.still_valid([], 2)
    assert ok is False and "no basis" in why


def test_stale_dependency_invalidates_what_rests_on_it():
    """Dependency-aware invalidation, at the depth actually needed: one hop, by id.

    The base fact here is *not* in the basis (it was superseded before authority was derived, so
    it is not live), but the legal review still declares a dependency on it. Superseding it again
    has to invalidate the review that rests on it — otherwise a conclusion outlives its premise.
    """
    base_v1 = worker_says({"contract": "v1"}, kind=ContributionKind.EVIDENCE, request_id="r1")
    base_v2 = Contribution(case_id="c1", request_id="r1b", kind=ContributionKind.EVIDENCE,
                           actor=Actor.worker("scorer"), payload={"contract": "v2"},
                           supersedes=base_v1.id, created_epoch=120)
    rests_on = Contribution(case_id="c1", request_id="r2", kind=ContributionKind.EVIDENCE,
                            actor=Actor.worker("legal"), payload={"ok": True},
                            dependencies=[base_v1.id], created_epoch=140)
    dec = human_says()
    live = [base_v2, rests_on, dec]
    a = authorize(case(satisfied("r1b"), satisfied("r2"), satisfied("rh")),
                  [base_v1] + live, CasePolicy(), now=200)
    assert a.granted
    assert base_v1.id not in a.authorization.basis, "a superseded fact is not part of the basis"

    ok, why = a.authorization.still_valid([base_v1] + live, 260)
    assert ok is False and rests_on.id in why and base_v1.id in why


def test_superseding_any_live_contribution_invalidates_the_stored_envelope():
    """Conservative on purpose: the case moved, so the envelope has to be re-derived.

    This costs one tick and it is the right direction to fail. A narrow basis would let a fact
    change underneath a decision without invalidating it.
    """
    ev = worker_says({"reply_rate": 4.0}, kind=ContributionKind.EVIDENCE, request_id="r1")
    dec = human_says()
    a = authorize(case(satisfied("r1"), satisfied("rh")), [ev, dec], CasePolicy(), now=200)
    assert set(a.authorization.basis) == {ev.id, dec.id}


# --------------------------------------------------------------- degenerate cases


def test_a_case_with_no_contributions_authorizes_nothing():
    assert authorize(case(), [], CasePolicy(), now=200).granted is False


def test_evidence_alone_never_authorizes_however_much_of_it_there_is():
    ev = [worker_says({"n": i}, kind=ContributionKind.EVIDENCE, request_id=f"r{i}", epoch=i)
          for i in range(1, 20)]
    c = case(*[satisfied(f"r{i}") for i in range(1, 20)])
    assert authorize(c, ev, CasePolicy(), now=200).granted is False
