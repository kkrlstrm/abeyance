"""The allowlist bridge: empty by default, mapped by hand, retired modes refused by name.

Hermetic — the module reads its configuration from the environment at import time, so every test
sets the environment and reloads. The one test that reads a real allowlist on disk skips when there
isn't one, which is the honest shape for a drift guard.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from abeyance import ContributionKind, from_allowlist, unmapped_modes
from abeyance.errors import ConfigurationError, NotCleared

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

EVIDENCE = ContributionKind.EVIDENCE
RECOMMENDATION = ContributionKind.RECOMMENDATION
DECISION = ContributionKind.DECISION


def _load(monkeypatch, *, routes: str = "", sample: bool = False):
    monkeypatch.delenv("ABEYANCE_ROUTES_JSON", raising=False)
    monkeypatch.delenv("ABEYANCE_USE_SAMPLE_ALLOWLIST", raising=False)
    if routes:
        monkeypatch.setenv("ABEYANCE_ROUTES_JSON", routes)
    if sample:
        monkeypatch.setenv("ABEYANCE_USE_SAMPLE_ALLOWLIST", "1")
    import openrouter_clearances as mod
    return importlib.reload(mod)


@pytest.fixture()
def unconfigured(monkeypatch):
    return _load(monkeypatch)


@pytest.fixture()
def sample(monkeypatch):
    return _load(monkeypatch, sample=True)


# --------------------------------------------------------------------------- the default


def test_unconfigured_clears_nothing_at_all(unconfigured):
    """The whole point of #4. A newcomer inherits nobody's measurements."""
    reg = unconfigured.build_clearances()
    assert reg.modes() == []
    with pytest.raises(NotCleared):
        reg.require("extract-accurate", EVIDENCE)


def test_unconfigured_does_not_ship_the_orchestrator_tier_either(unconfigured):
    """It is a declaration too, and its evidence_ref is a placeholder — shipping it live would
    hand a newcomer a working clearance with nothing behind it."""
    assert "case-recommendation" not in unconfigured.build_clearances()
    assert "case-recommendation" in unconfigured.build_clearances(include_orchestrator=True)


def test_unconfigured_check_is_honest_rather_than_green(unconfigured):
    ok, msg = unconfigured.check()
    assert ok and "zero clearances" in msg


# --------------------------------------------------------------------------- the sample


def test_the_sample_loads_only_on_explicit_opt_in(monkeypatch):
    # Sequentially, not via both fixtures: they reload the same module, so holding two
    # references would just be two names for whichever load happened last.
    assert _load(monkeypatch, sample=True).build_clearances().modes()
    assert _load(monkeypatch).build_clearances().modes() == []


def test_every_sample_mode_has_a_declared_kind(sample):
    assert set(sample.SAMPLE_ROUTES["modes"]) == set(sample.KIND_FOR)


def test_no_mode_is_cleared_for_decision(sample):
    for kinds in sample.KIND_FOR.values():
        assert DECISION not in kinds


def test_the_allowlist_is_evidence_shaped_apart_from_the_linter(sample):
    """Not a style assertion — it is what the source policy already implies.

    That policy disqualifies drafting, consequential single-row classification, primary review and
    positioning critique from delegation. What survives is extraction, filtering, digest and
    vision: assertions about the world. `lint-code` is the one judgment-without-authority entry,
    which is what a RECOMMENDATION is.
    """
    report = sample.build_clearances(include_orchestrator=False).clearance_report()
    assert report["recommendation"] == ["lint-code"]
    assert "extract-accurate" in report["evidence"]
    assert "lint-code" not in report.get("evidence", [])


def test_the_sample_evidence_refs_are_marked_as_not_yours(sample):
    """Someone reading the registry must be able to see the citations are borrowed."""
    weak = sample.unbacked(sample.build_clearances())
    assert {c.mode for c in weak} >= set(sample.SAMPLE_ROUTES["modes"])


# --------------------------------------------------------------------------- the gate


def test_an_evidence_mode_cannot_form_a_recommendation(sample):
    reg = sample.build_clearances()
    reg.require("extract-accurate", EVIDENCE)
    with pytest.raises(NotCleared, match="cleared for"):
        reg.require("extract-accurate", RECOMMENDATION)


def test_the_linter_cannot_be_read_as_fact(sample):
    """It is judgment. Clearing it as EVIDENCE would launder an opinion into an assertion."""
    reg = sample.build_clearances()
    reg.require("lint-code", RECOMMENDATION)
    with pytest.raises(NotCleared):
        reg.require("lint-code", EVIDENCE)


def test_a_retired_mode_is_refused_with_its_reason(sample):
    """An unknown mode and one that FAILED an eval are different facts."""
    reg = sample.build_clearances()
    with pytest.raises(NotCleared, match="was retired"):
        reg.require("deepseek", EVIDENCE)
    with pytest.raises(NotCleared, match="SQL injection"):
        reg.require("deepseek", EVIDENCE)


def test_retired_modes_never_become_clearances(sample):
    reg = sample.build_clearances()
    for mode in sample.SAMPLE_ROUTES["retired"]:
        assert mode not in reg


def test_a_mode_cannot_be_both_cleared_and_retired(sample):
    from abeyance.clearance import ClearanceRegistry, ModelClearance

    reg = ClearanceRegistry(retired={"x": "failed an eval."})
    with pytest.raises(ConfigurationError, match="also listed as retired"):
        reg.add(ModelClearance(mode="x", model="v/m", emits=(EVIDENCE,),
                               evidence_ref="e.md", verified_date="2026-06-04"))


# --------------------------------------------------------------------------- staleness


def test_staleness_uses_the_allowlists_own_window(sample):
    reg = sample.build_clearances()
    assert reg.require("extract-bulk", EVIDENCE).stale_after_days == 120
    reg.require("extract-bulk", EVIDENCE, today="2026-08-17")        # 91d — inside
    with pytest.raises(NotCleared, match="stale"):
        reg.require("extract-bulk", EVIDENCE, today="2027-01-01")


def test_stale_sweep_reports_by_mode(sample):
    stale = sample.build_clearances().stale("2027-01-01")
    assert "extract-bulk" in {c.mode for c in stale}


# --------------------------------------------------------------------------- from a file


def _write(tmp_path, **overrides):
    modes = json.loads(json.dumps(SAMPLE_MIN))
    modes.update(overrides)
    p = tmp_path / "routes.json"
    p.write_text(json.dumps({"staleness_warn_days": 120, "modes": modes,
                             "retired": {"gone": "failed an eval."}}))
    return str(p)


SAMPLE_MIN = {
    "extract-accurate": {"model": "vendor/cheap", "verified_date": "2026-06-04",
                         "evidence_ref": "evals/mine.md"},
}


def test_a_file_on_disk_is_read_and_mapped(monkeypatch, tmp_path):
    mod = _load(monkeypatch, routes=_write(tmp_path))
    reg = mod.build_clearances()
    assert reg.require("extract-accurate", EVIDENCE).model == "vendor/cheap"
    assert "case-recommendation" in reg          # configured -> orchestrator tier included
    ok, msg = mod.check()
    assert ok and "all 1 modes mapped" in msg


def test_a_canonical_mode_with_no_kind_is_reported_and_skipped(monkeypatch, tmp_path):
    path = _write(tmp_path, **{"brand-new-mode": {"model": "vendor/new",
                                                  "verified_date": "2026-08-01"}})
    mod = _load(monkeypatch, routes=path)
    ok, msg = mod.check()
    assert not ok and "brand-new-mode" in msg and "no contribution kind" in msg
    assert "brand-new-mode" not in mod.build_clearances()      # skipped, never guessed


def test_an_unreadable_path_falls_back_to_nothing_not_to_the_sample(monkeypatch, tmp_path):
    """Failing open to somebody else's evals would be the worst possible default."""
    mod = _load(monkeypatch, routes=str(tmp_path / "absent.json"))
    assert mod.build_clearances().modes() == []
    ok, msg = mod.check()
    assert ok and "no allowlist configured" in msg


def test_canonical_allowlist_on_this_machine_is_fully_mapped(monkeypatch):
    """Runs only where a real allowlist lives. The guard that matters in practice."""
    real = Path.home() / "openrouter-test" / "routes.json"
    if not real.is_file():
        pytest.skip(f"no allowlist at {real}")
    mod = _load(monkeypatch, routes=str(real))
    ok, msg = mod.check()
    assert ok, msg


# --------------------------------------------------------------------------- the library loader


def test_from_allowlist_is_provider_agnostic():
    """Nothing in the loader knows about any routing vendor — it takes a plain dict."""
    spec = {"modes": {"a": {"model": "v/m", "verified_date": "2026-06-04",
                            "evidence_ref": "e.md", "provider": {"order": ["deepinfra"]},
                            "do_not_use_when": "judgment"},
                      "b": {"model": "v/n", "verified_date": "2026-06-04"}},
            "retired": {"old": "failed Eval 4."}}
    kinds = {"a": (EVIDENCE,)}
    reg = from_allowlist(spec, kinds)
    assert reg.modes() == ["a"]
    assert unmapped_modes(spec, kinds) == ["b"]
    c = reg.require("a", EVIDENCE)
    assert c.provider == "deepinfra"
    assert "do_not_use_when" in c.notes
    with pytest.raises(NotCleared, match="failed Eval 4"):
        reg.require("old", EVIDENCE)


def test_from_allowlist_respects_a_per_mode_stale_window():
    spec = {"staleness_warn_days": 120,
            "modes": {"a": {"model": "v/m", "verified_date": "2026-06-04",
                            "evidence_ref": "e.md", "stale_after_days": 30}}}
    reg = from_allowlist(spec, {"a": (EVIDENCE,)})
    assert reg.require("a", EVIDENCE).stale_after_days == 30


# --------------------------------------------------------------------------- wiring


def test_a_worker_capability_cannot_be_built_on_a_mis_kinded_mode(sample):
    from abeyance.clearance import model_capability

    reg = sample.build_clearances()
    model_capability(reg, mode="extract-accurate", name="extract",
                     produces=("extracted-fields",), emits=EVIDENCE,
                     image="python:3.12-slim", app="workers-extract")
    with pytest.raises(NotCleared):
        model_capability(reg, mode="extract-accurate", name="opinion",
                         produces=("launch-recommendation",), emits=RECOMMENDATION,
                         image="python:3.12-slim", app="workers-model")


def test_the_model_id_lands_in_the_committed_capability(sample):
    """So re-pointing a mode shows up as a line change in a reviewed file."""
    from abeyance.clearance import model_capability

    cap = model_capability(sample.build_clearances(), mode="extract-multimodal",
                           name="vision", produces=("page-text",), emits=EVIDENCE,
                           image="python:3.12-slim", app="workers-extract")
    assert cap.env["MODEL_ID"] == "minimax/minimax-m3"
    assert cap.env["MODEL_MODE"] == "extract-multimodal"
