"""Re-probing: asking a fact again, days later, without erasing the answer you had.

The case layer's stated principle is "poll for artifacts; ask only for authority" — a case waits
on a list filling in a vendor's UI, a document being written, an invoice clearing, and the honest
way to learn it finished is to look again. Rules cannot express that (a rule may only ADD a need,
and re-asking is the same need at a later time), so it is an explicit act by whatever drives the
clock.

Every test here is a way that could go quietly wrong. The first one is the whole reason
`armed_at` exists: without it, re-arming a request is a no-op that *looks* like a fresh reading.
"""
from __future__ import annotations

import pytest

from abeyance import (Actor, CaseStatus, ContributionKind, ConfigurationError, Escalation,
                      Need, RequestStatus, Rule)

from test_cases import worker_contributes


def open_probed(cases, *, payload=None):
    """A case with one satisfied evidence request, ready to be asked again."""
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    cases.tick(case.id)
    first = worker_contributes(cases, case.id, "campaign-performance",
                              payload or {"rows": 10, "ready": False})
    cases.tick(case.id)
    assert cases.get(case.id).request("campaign-performance").status is RequestStatus.SATISFIED
    return case, first


# --------------------------------------------------------------- the failure it prevents


def test_the_old_contribution_does_not_settle_a_rearmed_request(cases, clock, runner):
    """Without `armed_at` this is the silent bug: the previous run's row is still in the store
    under the same request id, so the next tick reads it as an answer, marks the request
    satisfied, and never starts a container. The case then reports a reading it never took."""
    case, _ = open_probed(cases)
    started_before = len(runner.started)

    clock.advance(hours=1)
    cases.reprobe(case.id, "campaign-performance", because="the list may have filled")

    report = cases.tick(case.id)[0]
    assert [r.action for r in report.dispatch.records] == ["dispatched"], (
        "the stale row must not count as this probe's answer")
    assert len(runner.started) == started_before + 1, "a container actually ran again"
    assert cases.get(case.id).request("campaign-performance").status is RequestStatus.DISPATCHED


def test_a_fresh_contribution_settles_it_and_the_old_reading_survives(cases, clock):
    case, first = open_probed(cases, payload={"rows": 10, "ready": False})
    clock.advance(hours=1)
    cases.reprobe(case.id, "campaign-performance")
    cases.tick(case.id)

    cases.contribute(case.id, kind=ContributionKind.EVIDENCE, actor=Actor.worker("db-evidence"),
                     request_id="campaign-performance", payload={"rows": 300, "ready": True},
                     supersedes=first.id, revision="probe-2")
    report = cases.tick(case.id)[0]

    assert cases.get(case.id).request("campaign-performance").status is RequestStatus.SATISFIED
    ids = [c.id for c in cases.contributions(case.id)]
    assert first.id in ids, "re-probing must not delete the reading a human may have been shown"
    assert len(ids) == 2


def test_a_probe_is_not_answered_by_a_contribution_that_predates_it(cases, clock):
    """The boundary, stated as a fact about time rather than about ids."""
    case, _ = open_probed(cases)
    clock.advance(hours=2)
    cases.reprobe(case.id, "campaign-performance")
    armed = cases.get(case.id).request("campaign-performance").armed_at

    # A row written a minute BEFORE the probe was armed — a slow worker from the last round.
    clock.advance(minutes=-1)
    cases.contribute(case.id, kind=ContributionKind.EVIDENCE, actor=Actor.worker("db-evidence"),
                     request_id="campaign-performance", payload={"late": True},
                     supersedes="x", revision="late")
    cases.tick(case.id)
    assert cases.get(case.id).request("campaign-performance").status is not RequestStatus.SATISFIED

    clock.advance(minutes=5)
    cases.contribute(case.id, kind=ContributionKind.EVIDENCE, actor=Actor.worker("db-evidence"),
                     request_id="campaign-performance", payload={"now": True},
                     supersedes="x", revision="ontime")
    cases.tick(case.id)
    req = cases.get(case.id).request("campaign-performance")
    assert req.status is RequestStatus.SATISFIED and req.armed_at == armed


# --------------------------------------------------------------- what it refuses


def test_a_decision_cannot_be_reprobed(cases):
    """A machine re-arming a human's yes would discard it silently. Superseding the evidence it
    rested on is the honest path: the decision stays on the record and visibly stops counting."""
    case = cases.open(action="launch-campaign", subject_key="acme")
    cases.request(case.id, Need("sign-off", external=True, expects=ContributionKind.DECISION))

    with pytest.raises(ConfigurationError) as e:
        cases.reprobe(case.id, "sign-off")
    assert "DECISION" in str(e.value)


def test_an_external_need_cannot_be_reprobed(cases):
    """An external need has no worker to re-run. It is a wait, not a probe, and conflating the
    two would look like polling while nothing was ever checked."""
    case = cases.open(action="launch-campaign", subject_key="acme")
    cases.request(case.id, Need("list-ready", external=True,
                                expects=ContributionKind.EVIDENCE))

    with pytest.raises(ConfigurationError) as e:
        cases.reprobe(case.id, "list-ready")
    assert "out of band" in str(e.value)


def test_reprobing_a_need_the_case_never_asked_for_is_an_error(cases):
    case = cases.open(action="launch-campaign", subject_key="acme")
    with pytest.raises(ConfigurationError):
        cases.reprobe(case.id, "never-requested")


# --------------------------------------------------------------- how it composes


def test_a_probe_resets_attempts_so_a_previously_failed_check_gets_its_allowance_back(
        cases, make_cases, clock):
    """The old failure was about the old world. But note the corollary recorded in `reprobe`'s
    docstring: cadence is the caller's to bound, because each probe costs a dispatch."""
    from abeyance import CasePolicy
    cases = make_cases(policy=CasePolicy(max_attempts=1))
    case = cases.open(action="launch-campaign", subject_key="acme",
                      needs=["campaign-performance"])
    cases.tick(case.id)
    clock.advance(hours=3)
    cases.tick(case.id)  # lease blown, attempts exhausted
    assert cases.get(case.id).request("campaign-performance").status is RequestStatus.FAILED
    assert cases.get(case.id).status is CaseStatus.BLOCKED

    cases.reprobe(case.id, "campaign-performance", because="worker fixed")
    report = cases.tick(case.id)[0]
    assert [r.action for r in report.dispatch.records] == ["dispatched"]
    assert report.status is CaseStatus.OPEN, "a re-probed case is no longer stuck"


def test_the_new_spec_reaches_the_container(cases, clock, runner):
    """Carrying the previous reading forward is how quiescence is measured — this probe has to be
    able to say "last time I saw 10 rows". A probe that could not be told anything new would be
    unable to compare."""
    import json
    case, _ = open_probed(cases)
    clock.advance(hours=1)
    cases.reprobe(case.id, "campaign-performance",
                  spec={"client": "acme", "previous": {"rows": 10}, "check": 2})
    cases.tick(case.id)

    spec = json.loads(runner.env_of(runner.last["ref"])["ABEYANCE_SPEC"])
    assert spec["previous"] == {"rows": 10} and spec["check"] == 2


def test_a_reprobe_is_on_the_record_with_its_reason(cases, clock):
    case, _ = open_probed(cases)
    clock.advance(hours=1)
    cases.reprobe(case.id, "campaign-performance", because="engineer said the bridge is built")

    entry = [h for h in cases.get(case.id).history if h.get("event") == "reprobed"]
    assert len(entry) == 1
    assert entry[0]["was"] == "satisfied"
    assert "engineer said" in entry[0]["because"]


def test_re_probing_withdraws_authority_before_the_new_reading_even_arrives(cases, clock,
                                                                           escalations):
    """Two beats, and the first is the one worth having.

    The moment a fact is re-opened the case is *no longer* authorized — not because the new
    reading disagreed, but because there is no current reading at all. So the window between
    "we decided to look again" and "we know what we saw" is closed rather than optimistic, and
    nothing can be spent inside it.
    """
    case, first = open_probed(cases, payload={"rows": 300, "ready": True})
    cases.policy_decision(case.id, rule="pre-cleared", standing=("launch-campaign",))
    assert cases.tick(case.id)[0].authorized is True

    clock.advance(hours=6)
    cases.reprobe(case.id, "campaign-performance", because="re-check before spending")
    report = cases.tick(case.id)[0]
    assert report.authorized is False
    assert "not satisfied" in report.authority.reason
    assert report.status is CaseStatus.OPEN, "re-opened, not broken — the probe is in flight"

    out = cases.execute(case.id, lambda *a: pytest.fail("must not act while a probe is open"))
    assert out.written is False

    # Second beat: the reversed reading lands and supersedes what the yes rested on.
    cases.contribute(case.id, kind=ContributionKind.EVIDENCE, actor=Actor.worker("db-evidence"),
                     request_id="campaign-performance",
                     payload={"rows": 280, "ready": False, "changed": True},
                     supersedes=first.id, revision="probe-2")
    report = cases.tick(case.id)[0]
    assert report.authorized is False, "the yes was about 300 rows that are no longer there"
    assert report.authority.stale_decisions
    # OPEN rather than BLOCKED, deliberately: a stale yes is recoverable by asking again, and a
    # rule is free to warrant that fresh decision. What must never be quiet is the fact that a
    # real approval stopped applying, so that is an escalation, not a status.
    assert cases.get(case.id).status is CaseStatus.OPEN
    assert Escalation.STALE_AUTHORITY in [e.kind for e in escalations]

    out = cases.execute(case.id, lambda *a: pytest.fail("must not act on the stale yes"))
    assert out.written is False and "superseded" in out.blocked


def test_a_delegated_decision_goes_stale_too(cases, clock):
    """Found by writing the test above: `policy_decision` stamped no `dependencies`, so a
    delegated yes could never go stale. "Pre-cleared because suppression is verified" would keep
    clearing after suppression stopped being verified — the rule fired once, on one reading, and
    nothing re-evaluates it. A delegation is a standing instruction, not a standing exemption."""
    case, first = open_probed(cases, payload={"verified": True})
    d = cases.policy_decision(case.id, rule="cleared-if-verified", standing=("launch-campaign",))
    assert d.dependencies == [first.id], "a delegated yes records what it rested on"
    assert cases.tick(case.id)[0].authorized is True

    clock.advance(hours=1)
    cases.contribute(case.id, kind=ContributionKind.EVIDENCE, actor=Actor.worker("db-evidence"),
                     request_id="campaign-performance", payload={"verified": False},
                     supersedes=first.id, revision="r2")
    assert cases.tick(case.id)[0].authorized is False


def test_a_refusal_at_commit_time_stops_the_row_claiming_authority(cases, clock):
    """Also found above: the stale-decision refusal returned without saving, so a case that had
    reached AUTHORIZED kept the label until something ticked it again. If nothing did, every
    reader of the store saw a case cleared to act that `execute()` would refuse."""
    case, first = open_probed(cases, payload={"safe": True})
    cases.policy_decision(case.id, rule="pre-cleared", standing=("launch-campaign",))
    assert cases.tick(case.id)[0].authorized is True
    assert cases.get(case.id).status is CaseStatus.AUTHORIZED

    clock.advance(hours=2)
    cases.contribute(case.id, kind=ContributionKind.EVIDENCE, actor=Actor.worker("db-evidence"),
                     request_id="campaign-performance", payload={"safe": False},
                     supersedes=first.id, revision="r2")

    out = cases.execute(case.id, lambda *a: pytest.fail("must not act"))
    assert out.written is False
    assert cases.get(case.id).status is CaseStatus.BLOCKED, (
        "the label has to match what execute() would actually do")


def test_rules_still_cannot_re_ask_which_is_why_this_exists(cases, make_cases, clock):
    """The constraint that forces re-probing to be explicit: `derive()` drops any need that
    already has a request, so a rule firing every tick is a no-op after the first."""
    fired = []

    def always_wants_it(view):
        fired.append(1)
        return [Need("campaign-performance", spec={"n": len(fired)})]

    cases = make_cases(rules=[Rule("wants-it", always_wants_it)])
    case = cases.open(action="launch-campaign", subject_key="acme")
    cases.tick(case.id)
    worker_contributes(cases, case.id, "campaign-performance", {"rows": 1})
    clock.advance(hours=1)
    cases.tick(case.id)
    clock.advance(hours=1)
    cases.tick(case.id)

    assert len(fired) >= 3, "the rule kept firing"
    assert len([r for r in cases.get(case.id).requests
                if r.need == "campaign-performance"]) == 1, "and could never re-ask"
