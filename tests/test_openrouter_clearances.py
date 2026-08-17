"""The routing-allowlist bridge: one allowlist, mapped to kinds by hand, drift caught loudly.

Hermetic by default — every test points `ABEYANCE_ROUTES_JSON` at a path that does not exist, so
the suite exercises the embedded snapshot and needs no machine-local file. The one test that reads
canonical routes.json skips when it is absent, which is the honest shape for a drift guard: it can
only run where the source of truth lives.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from abeyance import ContributionKind
from abeyance.errors import ConfigurationError, NotCleared

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

EVIDENCE = ContributionKind.EVIDENCE
RECOMMENDATION = ContributionKind.RECOMMENDATION
DECISION = ContributionKind.DECISION


@pytest.fixture()
def bridge(monkeypatch, tmp_path):
    """The module loaded against the embedded snapshot, never the developer's home directory."""
    monkeypatch.setenv("ABEYANCE_ROUTES_JSON", str(tmp_path / "absent.json"))
    import openrouter_clearances as mod
    return importlib.reload(mod)


# --------------------------------------------------------------------------- the mapping


def test_every_embedded_mode_has_a_declared_kind(bridge):
    """A routing mode with no entry in KIND_FOR gets no clearance — never a guessed one."""
    assert set(bridge.EMBEDDED_ROUTES["modes"]) == set(bridge.KIND_FOR)


def test_no_mode_is_cleared_for_decision(bridge):
    """The invariant the whole design turns on, asserted over the real allowlist."""
    for kinds in bridge.KIND_FOR.values():
        assert DECISION not in kinds


def test_the_allowlist_is_evidence_shaped_apart_from_the_linter(bridge):
    """Not a style assertion — it is what the source policy already implies.

    The routing policy disqualifies drafting, consequential single-row classification, primary
    review and positioning critique from delegation. What survives is extraction, filtering,
    digest and vision — assertions about the world. `lint-code` is the one judgment-without-
    authority entry, which is exactly what a RECOMMENDATION is.
    """
    report = bridge.build_clearances(include_orchestrator=False).clearance_report()
    assert report["recommendation"] == ["lint-code"]
    assert "extract-accurate" in report["evidence"]
    assert "lint-code" not in report.get("evidence", [])


def test_the_orchestrator_tier_is_declared_separately(bridge):
    """routes.json governs delegation AWAY from the orchestrator, so it holds no entry for it."""
    assert "case-recommendation" not in bridge.EMBEDDED_ROUTES["modes"]
    with_orch = bridge.build_clearances()
    assert with_orch.require("case-recommendation", RECOMMENDATION).model == "claude-opus-5"
    assert "case-recommendation" not in bridge.build_clearances(include_orchestrator=False)


# --------------------------------------------------------------------------- the gate


def test_an_evidence_mode_cannot_form_a_recommendation(bridge):
    reg = bridge.build_clearances()
    reg.require("extract-accurate", EVIDENCE)
    with pytest.raises(NotCleared, match="cleared for"):
        reg.require("extract-accurate", RECOMMENDATION)


def test_the_linter_cannot_be_read_as_fact(bridge):
    """It is judgment. Recording it as EVIDENCE would launder an opinion into an assertion."""
    reg = bridge.build_clearances()
    reg.require("lint-code", RECOMMENDATION)
    with pytest.raises(NotCleared):
        reg.require("lint-code", EVIDENCE)


def test_a_retired_mode_is_refused_with_its_reason(bridge):
    """An unknown mode and a mode that FAILED an eval are different facts.

    Collapsing them into "unknown mode" is how something disqualified for cause gets quietly
    re-added a year later by someone who never saw the eval.
    """
    reg = bridge.build_clearances()
    with pytest.raises(NotCleared, match="was retired"):
        bridge.require(reg, "deepseek", EVIDENCE)
    with pytest.raises(NotCleared, match="SQL injection"):
        bridge.require(reg, "deepseek", EVIDENCE)


def test_retired_modes_never_become_clearances(bridge):
    reg = bridge.build_clearances()
    for mode in bridge.EMBEDDED_ROUTES["retired"]:
        assert mode not in reg


def test_require_still_passes_a_live_mode_through(bridge):
    reg = bridge.build_clearances()
    assert bridge.require(reg, "extract-bulk", EVIDENCE).model.startswith("qwen/")


# --------------------------------------------------------------------------- staleness


def test_staleness_uses_the_allowlists_own_window(bridge):
    reg = bridge.build_clearances()
    assert reg.require("extract-bulk", EVIDENCE).stale_after_days == 120
    reg.require("extract-bulk", EVIDENCE, today="2026-08-17")          # 91d — inside
    with pytest.raises(NotCleared, match="stale"):
        reg.require("extract-bulk", EVIDENCE, today="2027-01-01")


def test_stale_sweep_reports_by_mode(bridge):
    stale = bridge.build_clearances().stale("2027-01-01")
    assert "extract-bulk" in {c.mode for c in stale}


# --------------------------------------------------------------------------- drift + fallback


def test_consistency_passes_when_only_the_embedded_snapshot_exists(bridge):
    ok, msg = bridge.routes_consistency()
    assert ok and "embedded is authoritative" in msg


def test_drift_against_canonical_is_reported_not_tolerated(bridge, tmp_path, monkeypatch):
    """A fallback that can silently disagree fails toward permitting a re-pointed model."""
    fake = tmp_path / "routes.json"
    modes = json.loads(json.dumps(bridge.EMBEDDED_ROUTES["modes"]))
    modes["extract-bulk"]["model"] = "vendor/something-else"
    fake.write_text(json.dumps({"staleness_warn_days": 120, "modes": modes}))
    monkeypatch.setenv("ABEYANCE_ROUTES_JSON", str(fake))
    mod = importlib.reload(bridge)
    ok, msg = mod.routes_consistency()
    assert not ok and "changed=" in msg and "extract-bulk" in msg


def test_a_canonical_mode_with_no_kind_is_reported(bridge, tmp_path, monkeypatch):
    fake = tmp_path / "routes.json"
    modes = json.loads(json.dumps(bridge.EMBEDDED_ROUTES["modes"]))
    modes["brand-new-mode"] = {"model": "vendor/new", "verified_date": "2026-08-01"}
    fake.write_text(json.dumps({"staleness_warn_days": 120, "modes": modes}))
    monkeypatch.setenv("ABEYANCE_ROUTES_JSON", str(fake))
    mod = importlib.reload(bridge)
    ok, msg = mod.routes_consistency()
    assert not ok and "no contribution kind" in msg
    # ...and it is skipped rather than guessed into a kind.
    assert "brand-new-mode" not in mod.build_clearances()


def test_canonical_routes_json_has_not_drifted(bridge, monkeypatch):
    """Runs only where the source of truth lives. This is the guard that matters in practice."""
    monkeypatch.delenv("ABEYANCE_ROUTES_JSON", raising=False)
    mod = importlib.reload(bridge)
    if not mod.ROUTES_PATH.is_file():
        pytest.skip(f"no canonical allowlist at {mod.ROUTES_PATH}")
    ok, msg = mod.routes_consistency()
    assert ok, msg


# --------------------------------------------------------------------------- wiring


def test_a_worker_capability_cannot_be_built_on_a_mis_kinded_mode(bridge):
    from abeyance.clearance import model_capability

    reg = bridge.build_clearances()
    model_capability(reg, mode="extract-accurate", name="extract",
                     produces=("extracted-fields",), emits=EVIDENCE,
                     image="python:3.12-slim", app="workers-extract")
    with pytest.raises(NotCleared):
        model_capability(reg, mode="extract-accurate", name="opinion",
                         produces=("launch-recommendation",), emits=RECOMMENDATION,
                         image="python:3.12-slim", app="workers-model")


def test_the_model_id_lands_in_the_committed_capability(bridge):
    """So re-pointing a mode shows up as a line change in a reviewed file."""
    from abeyance.clearance import model_capability

    cap = model_capability(bridge.build_clearances(), mode="extract-multimodal",
                           name="vision", produces=("page-text",), emits=EVIDENCE,
                           image="python:3.12-slim", app="workers-extract")
    assert cap.env["MODEL_ID"] == "minimax/minimax-m3"
    assert cap.env["MODEL_MODE"] == "extract-multimodal"
