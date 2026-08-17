"""Clearance: an eval clears a model for a KIND, and never for authority.

The two tests that carry the module are `test_decision_can_never_be_cleared` (no eval result
grants authority) and `test_require_refuses_a_kind_the_eval_did_not_cover` (being good at one task
is not evidence about another). The rest keep the declaration honest enough to review.
"""
from __future__ import annotations

import pytest

from abeyance import ContributionKind
from abeyance.clearance import (ClearanceRegistry, ModelClearance, model_capability)
from abeyance.errors import ConfigurationError, NotCleared

EVIDENCE = ContributionKind.EVIDENCE
RECOMMENDATION = ContributionKind.RECOMMENDATION
DECISION = ContributionKind.DECISION


def clearance(**kw) -> ModelClearance:
    base = dict(mode="extract-accurate", model="vendor/cheap-fast", emits=(EVIDENCE,),
                evidence_ref="OBSERVATIONS.md Eval 1b", verified_date="2026-06-04")
    base.update(kw)
    return ModelClearance(**base)


# --------------------------------------------------------------------------- the sharp one


def test_decision_can_never_be_cleared():
    """No score, on any eval, makes a model's output authoritative.

    This is the same guarantee `Capability.__post_init__` enforces for workers, stated where
    somebody might otherwise think a good enough eval is a route around it.
    """
    with pytest.raises(ConfigurationError, match="never"):
        clearance(mode="too-good", emits=(RECOMMENDATION, DECISION))


def test_require_refuses_a_kind_the_eval_did_not_cover():
    reg = ClearanceRegistry([clearance()])
    assert reg.require("extract-accurate", EVIDENCE).model == "vendor/cheap-fast"
    with pytest.raises(NotCleared, match="cleared for"):
        reg.require("extract-accurate", RECOMMENDATION)


def test_clearance_is_not_symmetric_between_kinds():
    """Judgment clearance does not imply extraction clearance either — different skills."""
    reg = ClearanceRegistry([clearance(mode="judge", emits=(RECOMMENDATION,))])
    reg.require("judge", RECOMMENDATION)
    with pytest.raises(NotCleared):
        reg.require("judge", EVIDENCE)


# --------------------------------------------------------------------------- the declaration


def test_a_clearance_needs_recorded_evidence():
    with pytest.raises(ConfigurationError, match="evidence"):
        clearance(evidence_ref="")


def test_a_clearance_needs_a_parseable_verified_date():
    with pytest.raises(ConfigurationError, match="verified_date"):
        clearance(verified_date="")
    with pytest.raises(ConfigurationError, match="ISO"):
        clearance(verified_date="June 2026")


def test_a_clearance_must_cover_at_least_one_kind():
    with pytest.raises(ConfigurationError, match="no contribution kind"):
        clearance(emits=())


def test_unknown_mode_names_the_allowlist():
    reg = ClearanceRegistry([clearance()])
    with pytest.raises(NotCleared) as e:
        reg.require("wire-money", EVIDENCE)
    assert "extract-accurate" in str(e.value)


def test_duplicate_mode_is_refused():
    reg = ClearanceRegistry([clearance()])
    with pytest.raises(ConfigurationError, match="already registered"):
        reg.add(clearance())


# --------------------------------------------------------------------------- staleness


def test_staleness_is_only_checked_when_a_date_is_supplied():
    """The module holds no clock. Omitting `today` trusts the declaration, deliberately."""
    reg = ClearanceRegistry([clearance(verified_date="2020-01-01")])
    reg.require("extract-accurate", EVIDENCE)                      # no clock, no complaint
    with pytest.raises(NotCleared, match="stale"):
        reg.require("extract-accurate", EVIDENCE, today="2026-08-17")


def test_inside_the_window_is_not_stale():
    reg = ClearanceRegistry([clearance(verified_date="2026-08-01", stale_after_days=120)])
    reg.require("extract-accurate", EVIDENCE, today="2026-08-17")


def test_stale_sweep_lists_expired_clearances():
    reg = ClearanceRegistry([
        clearance(mode="fresh", verified_date="2026-08-01"),
        clearance(mode="old", verified_date="2020-01-01"),
    ])
    assert [c.mode for c in reg.stale("2026-08-17")] == ["old"]


def test_stale_after_days_must_be_positive():
    with pytest.raises(ConfigurationError, match="stale on arrival"):
        clearance(stale_after_days=0)


# --------------------------------------------------------------------------- audit + round trip


def test_clearance_report_groups_modes_by_kind():
    reg = ClearanceRegistry([
        clearance(mode="extract-accurate", emits=(EVIDENCE,)),
        clearance(mode="digest-longcontext", emits=(EVIDENCE,)),
        clearance(mode="case-recommendation", model="claude-opus-5", emits=(RECOMMENDATION,)),
    ])
    assert reg.clearance_report() == {
        "evidence": ["digest-longcontext", "extract-accurate"],
        "recommendation": ["case-recommendation"],
    }


def test_doc_round_trip():
    c = clearance(provider="deepinfra", notes="not for judgment", stale_after_days=90)
    assert ModelClearance.from_doc(c.to_doc()) == c


# --------------------------------------------------------------------------- the wiring


def test_model_capability_refuses_a_mis_kinded_capability_at_build_time():
    """The gate fires when the registry is built, not when the container is already running."""
    reg = ClearanceRegistry([clearance()])          # cleared for EVIDENCE only
    with pytest.raises(NotCleared):
        model_capability(reg, mode="extract-accurate", name="opinion",
                         produces=("fit-score",), emits=RECOMMENDATION,
                         image="python:3.12-slim", app="workers-model")


def test_model_capability_injects_model_and_provider_into_the_registry():
    """So a silent model swap becomes a reviewable line change in the committed registry."""
    reg = ClearanceRegistry([clearance(provider="deepinfra")])
    cap = model_capability(reg, mode="extract-accurate", name="extract",
                           produces=("extracted-fields",), emits=EVIDENCE,
                           image="python:3.12-slim", app="workers-extract",
                           reach=("openrouter",), env={"WORKER_B64": "..."})
    assert cap.env["MODEL_MODE"] == "extract-accurate"
    assert cap.env["MODEL_ID"] == "vendor/cheap-fast"
    assert cap.env["MODEL_PROVIDER"] == "deepinfra"
    assert cap.env["WORKER_B64"] == "..."          # caller's env survives the merge
    assert cap.emits is EVIDENCE
    assert "MODEL_ID" in cap.to_doc()["env"]      # therefore in the committed file


def test_decision_is_refused_by_both_guards_independently():
    """Two guards, and neither depends on the other.

    Through `model_capability` the clearance gate is reached first, so the error is `NotCleared`
    (no eval covers DECISION — `ModelClearance` cannot even declare it). Construct a `Capability`
    directly and its own `__post_init__` refuses the same thing with `ConfigurationError`. Either
    guard alone would stop it; the weaker-looking one is not load-bearing for the other.
    """
    from abeyance import Capability

    reg = ClearanceRegistry([clearance(mode="judge", emits=(RECOMMENDATION,))])
    with pytest.raises(NotCleared):
        model_capability(reg, mode="judge", name="decider", produces=("x",), emits=DECISION,
                         image="python:3.12-slim", app="a")

    with pytest.raises(ConfigurationError, match="cannot be given"):
        Capability(name="decider", image="python:3.12-slim", produces=("x",), emits=DECISION)


def test_model_capability_enforces_staleness_when_asked():
    reg = ClearanceRegistry([clearance(verified_date="2020-01-01")])
    with pytest.raises(NotCleared, match="stale"):
        model_capability(reg, mode="extract-accurate", name="extract",
                         produces=("extracted-fields",), emits=EVIDENCE,
                         image="python:3.12-slim", app="workers-extract", today="2026-08-17")


# --------------------------------------------------------------------------- the env contract


def test_the_worker_contract_separates_store_kind_from_contribution_kind():
    """Regression: these two are easy to swap, and swapping them fails silently.

    `ABEYANCE_CONTRIBUTION_KIND` is the STORE kind to write under (`<loop>:contribution`);
    `ABEYANCE_EXPECTS` is the contribution kind (`evidence` | `recommendation`). A worker that
    writes its row under the wrong store kind produces no error anywhere — the request stays
    in-flight until its lease expires and is then declared lost, which looks exactly like a worker
    that never booted. Both shipped examples got this backwards once; nothing failed loudly.
    """
    from abeyance import CaseLoop
    from abeyance.adapters import MemoryStore

    loop = CaseLoop("launches", store=MemoryStore())
    kind_values = {k.value for k in ContributionKind}

    assert loop.contribution_kind == "launches:contribution"
    assert loop.kind == "launches:case"
    # The invariant: the store kind is namespaced and is never a ContributionKind value, so the
    # two can never be used interchangeably by accident without this assertion firing.
    assert loop.contribution_kind not in kind_values
    assert kind_values == {"evidence", "recommendation", "decision"}
