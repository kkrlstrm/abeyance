"""Point abeyance at an eval-gated routing allowlist you already maintain.

`abeyance.clearance.from_allowlist()` does the work; this file is the thin glue plus a worked
example of the one thing you have to write yourself — the **kind map**.

    export ABEYANCE_ROUTES_JSON=~/openrouter-test/routes.json
    python examples/openrouter_clearances.py

## The default is empty, and that is deliberate

With no allowlist configured you get **zero clearances**, and every `require()` raises
`NotCleared`. That failure is the product: a clearance asserts *"a recorded eval says this model is
good enough at this task"*, and nobody can make that claim on your behalf. A library that shipped a
populated registry would be handing you somebody else's measurements to rely on.

`SAMPLE_ROUTES` below is a real allowlist, and it is **not yours**. Its `evidence_ref` values point
at eval runs in a private repo you cannot open. It exists to show the shape and to let the tests
run hermetically. Reading it is useful; inheriting it is not — so it loads only when you ask:

    ABEYANCE_USE_SAMPLE_ALLOWLIST=1 python examples/openrouter_clearances.py

## What you write: the kind map

One line per mode, plus why. An eval clears a model for a *task*; the task decides whether the
output is a fact or an opinion, and only you know which. A mode missing from the map gets no
clearance at all — adding a routing mode must not silently grant it a contribution kind.

The sample map below has a finding baked into it worth noticing: **every mode is EVIDENCE except
the linter.** That is not a coincidence — the policy behind that allowlist already disqualifies
drafting, consequential single-row classification, primary review and positioning critique from
delegation, so what survives is extraction, filtering, digest and vision: assertions about the
world. It had been an evidence allowlist all along and nothing had a word for it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from abeyance import ContributionKind, from_allowlist, unmapped_modes
from abeyance.clearance import ClearanceRegistry, ModelClearance

EVIDENCE = ContributionKind.EVIDENCE
RECOMMENDATION = ContributionKind.RECOMMENDATION

ROUTES_PATH = Path(os.environ["ABEYANCE_ROUTES_JSON"]) if os.environ.get(
    "ABEYANCE_ROUTES_JSON") else None

USE_SAMPLE = os.environ.get("ABEYANCE_USE_SAMPLE_ALLOWLIST") == "1"

# --------------------------------------------------------------------------- the kind map

KIND_FOR: Dict[str, Sequence[ContributionKind]] = {
    # Assertions about the world: what a field says, what a document contains, what a page shows.
    # None of these forms an opinion about what to *do*, so none can be a RECOMMENDATION.
    "extract-bulk":        (EVIDENCE,),
    "extract-accurate":    (EVIDENCE,),
    "extract-multimodal":  (EVIDENCE,),
    "digest-longcontext":  (EVIDENCE,),

    # A binary autoresponder/OOO call is still an assertion about a message, not a judgment about
    # an action — and its own do_not_use_when bars multi-class and intent detection.
    "filter-auto-reply":   (EVIDENCE,),

    # The one RECOMMENDATION. A supplemental linter is judgment without authority — CASES.md names
    # "a security review" as exactly that — and its routing entry says findings MUST be
    # re-evaluated before action and it is never the primary review. Clearing it as EVIDENCE would
    # launder an opinion into a fact.
    "lint-code":           (RECOMMENDATION,),
}

# --------------------------------------------------------------------------- SAMPLE — not yours

SAMPLE_ROUTES: Dict[str, Any] = {
    "staleness_warn_days": 120,
    "modes": {
        "extract-bulk": {
            "model": "qwen/qwen3-235b-a22b-2507", "verified_date": "2026-05-18",
            "evidence_ref": "SAMPLE — private OBSERVATIONS.md 'Eval 1'",
            "do_not_use_when": "An operator reads a single output row and decides from it; ~20% "
                               "disagreement with the orchestrator on judgment-laden fields.",
        },
        "filter-auto-reply": {
            "model": "google/gemini-2.5-flash-lite", "verified_date": "2026-05-18",
            "evidence_ref": "SAMPLE — private OBSERVATIONS.md 'Eval 6'",
            "do_not_use_when": "Any multi-class classification, intent detection, or judgment.",
        },
        "lint-code": {
            "model": "mistralai/codestral-2508", "verified_date": "2026-05-18",
            "evidence_ref": "SAMPLE — private OBSERVATIONS.md 'Eval 4'",
            "do_not_use_when": "The primary architecture/security review — it missed a SQL "
                               "injection the orchestrator caught in real production code.",
        },
        "digest-longcontext": {
            "model": "deepseek/deepseek-v4-flash", "verified_date": "2026-06-04",
            "evidence_ref": "SAMPLE — private OBSERVATIONS.md 'Eval 2b'",
            "do_not_use_when": "A high-stakes irreversible decision with no human review.",
        },
        "extract-accurate": {
            "model": "deepseek/deepseek-v4-flash", "verified_date": "2026-06-04",
            "evidence_ref": "SAMPLE — private OBSERVATIONS.md 'Eval 1b'",
            "do_not_use_when": "Maximum-throughput synchronous batches, and single-row decisions "
                               "acted on directly.",
        },
        "extract-multimodal": {
            "model": "minimax/minimax-m3", "verified_date": "2026-06-25",
            "evidence_ref": "SAMPLE — private OBSERVATIONS.md 'Eval 8'",
            "do_not_use_when": "The page is text-extractable, or a per-row value is used without "
                               "a never-invent-a-value verify.",
        },
    },
    # Retired with the reason, never deleted — so a request for one is refused by name.
    "retired": {
        "free": "Routed to non-web models that hallucinate on current events.",
        "qwen": "Replaced by `extract-bulk` with explicit use-case constraints.",
        "deepseek": "Missed the highest-stakes finding (SQL injection) in Eval 4.",
        "chat": "Hallucinated entities in 30% of drafting outputs; drafting stays with the "
                "orchestrator.",
        "compare": "An evaluation harness, not a production routing mode.",
        "online": "Real-time web research stays with the orchestrator.",
    },
}

# --------------------------------------------------------------------------- the orchestrator tier
#
# A routing allowlist governs delegation *away from* the orchestrator, so it holds no entry for the
# orchestrator itself — yet that is the tier that actually carries a case's judgment. Declare it
# yourself, and point evidence_ref at a run you actually have.

ORCHESTRATOR_CLEARANCES: List[ModelClearance] = [
    ModelClearance(
        mode="case-recommendation",
        model="claude-opus-5",
        emits=(RECOMMENDATION,),
        evidence_ref="REPLACE ME — evals/case-recommendation.md, verdicts vs recorded human "
                     "decisions. This is the one clearance that carries a case; do not rely on "
                     "it until it cites a run you can open.",
        verified_date="2026-08-17",
        notes="Judgment on a consequential action, reviewed by a human before anything executes. "
              "Runs on a subscription credential via the Claude Code CLI, so it is flat-rate "
              "rather than metered. NOT for bulk extraction.",
    ),
]


# --------------------------------------------------------------------------- loading


def load_spec() -> Tuple[Optional[Dict[str, Any]], str]:
    """`(spec, source)`. `None` when nothing is configured — which is the default."""
    if ROUTES_PATH:
        try:
            raw = json.loads(ROUTES_PATH.read_text())
            if raw.get("modes"):
                return raw, str(ROUTES_PATH)
            return None, f"{ROUTES_PATH} has no 'modes'"
        except Exception as e:                      # noqa: BLE001
            return None, f"{ROUTES_PATH} unreadable ({e.__class__.__name__})"
    if USE_SAMPLE:
        return SAMPLE_ROUTES, "SAMPLE (someone else's evals — do not rely on these)"
    return None, "unconfigured"


PLACEHOLDER_MARKERS = ("REPLACE ME", "SAMPLE —")


def unbacked(reg: ClearanceRegistry) -> List[ModelClearance]:
    """Clearances whose evidence_ref is still a placeholder rather than a run you can open.

    These are live — they will pass `require()` — which is why they need naming. A clearance is a
    claim that a recorded eval exists; one citing "REPLACE ME" is that claim with nothing behind it.
    """
    return [c for c in reg.all()
            if any(m in c.evidence_ref for m in PLACEHOLDER_MARKERS)]


def build_clearances(include_orchestrator: Optional[bool] = None) -> ClearanceRegistry:
    """Every mapped mode in the configured allowlist, plus the orchestrator tier.

    Returns a genuinely EMPTY registry when nothing is configured — the orchestrator tier is a
    declaration too, and shipping it live by default would hand a newcomer a working clearance
    backed by a placeholder. `require()` then raises `NotCleared`, which is the correct outcome:
    no eval has been recorded, so nothing is cleared.

    Pass `include_orchestrator=True` explicitly to opt in regardless.
    """
    spec, _ = load_spec()
    if include_orchestrator is None:
        include_orchestrator = spec is not None
    reg = (from_allowlist(spec, KIND_FOR) if spec
           else ClearanceRegistry(retired=SAMPLE_ROUTES["retired"] if USE_SAMPLE else None))
    if include_orchestrator:
        for c in ORCHESTRATOR_CLEARANCES:
            reg.add(c)
    return reg


def check() -> Tuple[bool, str]:
    """Is the configured allowlist fully mapped? Wire this into a preflight, not a comment."""
    spec, source = load_spec()
    if spec is None:
        return True, f"no allowlist configured ({source}) — zero clearances, everything refused"
    missing = unmapped_modes(spec, KIND_FOR)
    if missing:
        return False, (f"{source}: modes with no contribution kind: {missing}. Add them to "
                       f"KIND_FOR with the reasoning, or they get no clearance at all.")
    return True, f"{source}: all {len(spec['modes'])} modes mapped to a kind"


CLEARANCES = build_clearances()


if __name__ == "__main__":
    spec, source = load_spec()
    ok, msg = check()
    print(f"source : {source}")
    print(f"check  : {'ok' if ok else 'FAIL'} — {msg}\n")

    if not CLEARANCES.modes():
        print("No clearances. Nothing is cleared to contribute, and require() will refuse.")
        print("  point at your own allowlist : export ABEYANCE_ROUTES_JSON=/path/routes.json")
        print("  or read the sample          : ABEYANCE_USE_SAMPLE_ALLOWLIST=1")
        sys.exit(0)

    print(f"{'mode':<22} {'kind':<15} {'model':<34} verified")
    for c in CLEARANCES.all():
        print(f"{c.mode:<22} {'/'.join(k.value for k in c.emits):<15} {c.model:<34} "
              f"{c.verified_date}")
    weak = unbacked(CLEARANCES)
    if weak:
        print(f"\n⚠ {len(weak)} clearance(s) cite a placeholder, not a run you can open:")
        for c in weak:
            print(f"    {c.mode:<22} {c.evidence_ref[:64]}")
        print("  These still pass require(). Record the eval or drop the clearance.")

    print("\nclearance_report() — what may form which kind of contribution:")
    print(json.dumps(CLEARANCES.clearance_report(), indent=2))
    if CLEARANCES.retired:
        print(f"\nretired (refused by name): {sorted(CLEARANCES.retired)}")

    today = os.environ.get("ABEYANCE_TODAY", "")
    if today:
        stale = CLEARANCES.stale(today)
        print(f"\nstale as of {today}: "
              f"{[f'{c.mode} ({c.age_days(today)}d)' for c in stale] or 'none'}")
